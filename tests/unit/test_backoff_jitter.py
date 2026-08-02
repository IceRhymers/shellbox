"""T-BACKOFF-JITTER -- full jitter, because the failure input is SYNCHRONIZED.

The measurement that shapes this: **the kill is a global wall-clock event at the edge.** Three
holds opened three minutes apart died in the same second (`probe/FINDINGS.md`). The event is at
the edge, so it does not respect sandbox boundaries -- every publisher talking to one App loses
its socket simultaneously.

That inverts the usual reasoning about backoff. Ordinary exponential backoff assumes failures
arrive independently and the server is struggling; here the failures are perfectly correlated
and the server is fine. A fixed delay would preserve the correlation exactly, so 32 publishers
would re-dial in the same millisecond, back off by the same amount, and re-dial together again.
The delay must therefore be *drawn*, not computed: ``uniform(floor, cap)``, re-drawn per
attempt. That is full jitter.

Three properties follow, and each has a test here because each is easy to lose in a refactor:

1. **The floor is nonzero** -- constraint 2. A socket can die seconds after opening, so a
   zero-delay retry can hot-loop straight into an imminent kill.
2. **The cap does not widen per attempt.** There is nothing to back off from: the App will
   accept the very next dial, so a widening cap only lengthens the blank-terminal window.
3. **Two publishers do not draw the same sequence.** The generator is seeded from the OS, not
   from the clock -- and the clock is the tempting default precisely when every instance is
   constructed in the same synchronized instant.
"""

from __future__ import annotations

import random
import statistics
import uuid

import anyio
from shellbox_mcp.transport import WSTransport, WSTransportConfig
from wsfakes import SESSION_ID, FakeConnection, RecordingSleep, ScriptedDial, hello_bytes

EPOCH = str(uuid.uuid4())

_FLOOR = 0.5
_CAP = 5.0
_DRAWS = 2000


def config(**overrides: object) -> WSTransportConfig:
    base: dict[str, object] = {
        "url": "wss://app.example/publish",
        "session_id": SESSION_ID,
        "epoch": EPOCH,
    }
    base.update(overrides)
    return WSTransportConfig(**base)  # type: ignore[arg-type]


def transport(**overrides: object) -> WSTransport:
    return WSTransport(config(**overrides), dial=ScriptedDial([]))


# --------------------------------------------------------------------------------------
# The shape of one draw
# --------------------------------------------------------------------------------------


def test_the_defaults_put_a_nonzero_floor_under_every_delay() -> None:
    """T-BACKOFF-JITTER. Constraint 2: never assume a minimum socket lifetime.

    A socket can die seconds after opening. With a zero floor a publisher that hits that case
    re-dials instantly, gets killed again, and spins -- turning a bounded reconnect into a hot
    loop against the edge.
    """
    defaults = config()

    assert defaults.backoff_floor > 0
    assert defaults.backoff_cap > defaults.backoff_floor
    assert defaults.backoff_cap <= 10.0, (
        "the cap IS the gap the publisher's ring has to cover; a large one guarantees a resync"
    )


def test_every_draw_lands_inside_the_configured_band() -> None:
    """T-BACKOFF-JITTER. The bound, over enough draws to catch an off-by-one at either end."""
    subject = transport(backoff_floor=_FLOOR, backoff_cap=_CAP)

    draws = [subject.next_delay() for _ in range(_DRAWS)]

    assert all(_FLOOR <= delay <= _CAP for delay in draws)


def test_the_draws_spread_across_the_band_rather_than_clustering() -> None:
    """T-BACKOFF-JITTER. The property that actually breaks the storm.

    A delay inside the band is not enough: ``floor + 0.001`` every time satisfies the bound and
    keeps 32 publishers in lockstep, which is the exact failure this exists to prevent. So the
    assertion is on the SPREAD -- the draws must reach both ends of the band and sit near its
    middle on average.

    The thresholds are loose on purpose. This is a randomized test in a suite that must not
    flake, so it is written to fail on a constant, a truncated range, or an exponential ramp,
    and not to police the quality of the PRNG.
    """
    subject = transport(backoff_floor=_FLOOR, backoff_cap=_CAP)

    draws = [subject.next_delay() for _ in range(_DRAWS)]
    span = _CAP - _FLOOR

    assert min(draws) < _FLOOR + span * 0.1, "the draws never approach the floor"
    assert max(draws) > _CAP - span * 0.1, "the draws never approach the cap"
    assert abs(statistics.fmean(draws) - (_FLOOR + _CAP) / 2) < span * 0.1
    assert len(set(draws)) > _DRAWS * 0.9, "a re-drawn delay must not repeat"


def test_a_degenerate_band_is_allowed_so_tests_can_pin_the_delay() -> None:
    """T-BACKOFF-JITTER. ``floor == cap`` is a fixed delay, which every other test in the
    transport lane relies on to keep its arithmetic legible."""
    assert transport(backoff_floor=0.0, backoff_cap=0.0).next_delay() == 0.0


# --------------------------------------------------------------------------------------
# What must NOT happen between attempts
# --------------------------------------------------------------------------------------


def test_the_delay_does_not_widen_with_the_attempt_count() -> None:
    """T-BACKOFF-JITTER. No exponential ramp, and the reason is not efficiency.

    The App is not a failing server. It will accept the very next dial -- the socket was killed
    by a healthy edge on a timer -- so backing off further buys nothing and costs exactly the
    thing the user is looking at: a blank terminal. A genuinely broken server is stopped by
    ``classify_failure`` making it terminal, not by waiting longer.

    Ten consecutive failures, and the tenth delay must obey the same cap as the first.
    """
    sleep = RecordingSleep()
    failures: list[FakeConnection | BaseException] = [OSError("refused") for _ in range(10)]
    failures.append(FakeConnection([hello_bytes(SESSION_ID, EPOCH)]))
    subject = WSTransport(
        config(backoff_floor=_FLOOR, backoff_cap=_CAP),
        dial=ScriptedDial(failures),
        sleep=sleep,
    )

    async def scenario() -> None:
        stream = subject.connect_forever()
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    anyio.run(scenario)

    assert len(sleep.delays) == 10
    assert all(_FLOOR <= delay <= _CAP for delay in sleep.delays), (
        f"a delay escaped the band, so the cap widens per attempt: {sleep.delays}"
    )
    assert sleep.delays != sorted(sleep.delays), (
        "ten draws arriving in ascending order is a ramp, not a jitter"
    )


def test_two_publishers_constructed_in_the_same_instant_draw_different_delays() -> None:
    """T-BACKOFF-JITTER. The seeding claim, and the one that is genuinely load-bearing.

    Constraint 1 makes construction itself synchronized: every publisher in every sandbox is
    killed in the same second and re-dials at once. A generator seeded from a coarse clock
    would hand two of them the identical sequence, and full jitter would then be decorative --
    the delays would vary over time and match each other exactly, which is the storm.

    ``random.Random()`` with no argument seeds from ``os.urandom``, so this holds. The test is
    here because "seed it from the time" is a one-character change that no other test notices.
    """
    first = [transport().next_delay() for _ in range(20)]
    second = [transport().next_delay() for _ in range(20)]

    assert first != second


def test_an_injected_generator_makes_the_sequence_reproducible() -> None:
    """T-BACKOFF-JITTER. The seam the rest of the lane uses.

    Injectable so a test can pin a delay, and per-instance rather than the module-level
    ``random`` functions -- which share global state, so one test seeding it would change
    another's draws.
    """
    expected = random.Random(1234).uniform(_FLOOR, _CAP)
    subject = WSTransport(
        config(backoff_floor=_FLOOR, backoff_cap=_CAP),
        dial=ScriptedDial([]),
        rng=random.Random(1234),
    )

    assert subject.next_delay() == expected


def test_the_loop_sleeps_before_every_retry_and_not_before_the_first_dial() -> None:
    """T-BACKOFF-JITTER. A delay in front of the first attempt would add latency to the
    common case -- a publisher starting up, with nothing to back off from yet."""
    sleep = RecordingSleep()
    subject = WSTransport(
        config(backoff_floor=0.0, backoff_cap=0.0),
        dial=ScriptedDial([FakeConnection([hello_bytes(SESSION_ID, EPOCH)])]),
        sleep=sleep,
    )

    async def scenario() -> None:
        stream = subject.connect_forever()
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    anyio.run(scenario)

    assert sleep.delays == [], "a first-try success must not have waited"
