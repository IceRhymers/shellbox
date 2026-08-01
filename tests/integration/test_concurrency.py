"""T-CONC-1/2/3 (§9, §11.4) -- what happens when pooled agents collide on one tmux server.

shellbox runs 1-32 MCP processes against a single tmux server with no lock, no leader
election and no cross-process serialisation (§9.2's explicit non-goal). Each test here pins
one of the three claims that makes that design defensible.

**T-CONC-1 -- create races resolve in tmux, atomically.** ``new-session`` is the arbiter, so
**exactly one** of N concurrent creates reports ``created: true``. "At most one" is the
assertion an earlier revision made, and it is satisfied by *zero* -- i.e. by every caller
failing -- which is the opposite of the property.

**T-CONC-2 -- concurrent sends do not interleave.** ``paste-buffer`` was measured atomic per
call (M29), so N concurrent payloads arrive as N unbroken runs.

WARNING: **A label collision worth knowing about before you go looking.** §11.4 uses the name
"T-CONC-2" for a different scenario -- A creates, B lists, B kills, A reads → ``not_found``,
the stale-cache test -- which is §9.2's *"``shell_read`` races another agent's kill"* row.
That one is **already covered**, by ``test_no_session_state.py`` at both the two-process and
the two-servers-in-one-process level. The test in *this* module is §9.2's *"concurrent
``shell_send`` to the SAME session"* row, i.e. the M29 atomicity claim, which nothing else
asserted. Both properties are tested; only the numbering is ambiguous.

**T-CONC-3 -- an incarnation mismatch is DETECTED.** Not prevented: tmux offers no
compare-and-swap, so the check-then-act window between resolving a session and pasting into
it is real, accepted and documented (§9.1, R12). What ``@shellbox_incarnation`` buys is that
misdelivery stops being silent -- ``shell_send`` reports the incarnation it targeted, and a
caller holding the old one can tell. **No test here claims prevention.**

WARNING: Two traps this module is written to stay out of, both of which have already cost this
project a revision:

* **``#{session_id}`` cannot discriminate incarnations.** It resets with the tmux *server*
  (M27), so after a kill-and-recreate the new session is ``$0`` again -- which is precisely
  why ``@shellbox_incarnation`` exists. Asserted, not assumed.
* **An EMPTY incarnation is never a match.** ``"" == ""`` passes green while validating
  nothing, and that defect actually shipped: r2's ``set-option -t '=name'`` always returned
  rc=1, so the incarnation was never stored and T-CONC-3's ancestor compared two empty
  strings. Every comparison below asserts non-emptiness first, and
  ``test_two_unstamped_sessions_are_not_each_others_incarnation_match`` makes the guard
  itself executable.

Synchronization is sentinel-and-poll throughout (§11.1). There is no ``sleep`` in this file.
"""

from __future__ import annotations

import itertools
import re
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conftest import TmuxServer, await_file, await_file_bytes, raw_reader, requires_tmux
from harness import Harness, Outcome, call, make_harness, run_calls, run_script
from mcp import ClientSession

pytestmark = requires_tmux

# Generous: it is not a synchronization mechanism, it is the deadline that turns a thread
# that died before the barrier into a failure instead of a hung suite.
BARRIER_TIMEOUT = 90.0


def in_parallel[T](count: int, body: Callable[[int, threading.Barrier], T]) -> list[T]:
    """Run ``body(index, barrier)`` in ``count`` threads and return the results in order.

    The barrier is the point. Each ``body`` here spawns an MCP process and completes a
    handshake before touching tmux, and process startup dominates a tool call by orders of
    magnitude -- so without a rendezvous *after* the handshake the "concurrent" calls would
    be comfortably sequential and every race assertion below would be vacuous.
    """
    barrier = threading.Barrier(count)
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(body, index, barrier) for index in range(count)]
        return [future.result() for future in futures]


def concurrent_calls(harness: Harness, calls: list[tuple[str, dict[str, object]]]) -> list[Outcome]:
    """One tool call per PROCESS, all released together.

    Separate processes rather than threads in one server: §6's "zero in-process session
    state" means a single process would be a weaker subject, and the pool this simulates is
    made of processes.
    """

    def body(index: int, barrier: threading.Barrier) -> Outcome:
        name, arguments = calls[index]

        async def script(client: ClientSession) -> Outcome:
            # Blocking on purpose, and safe: this thread's event loop has nothing else to
            # run, and the barrier's own deadline is shorter than the session timeout.
            barrier.wait(timeout=BARRIER_TIMEOUT)
            return await call(client, name, arguments)

        return run_script(harness, script)

    return in_parallel(len(calls), body)


def tmux_format(tmux: TmuxServer, name: str, format_field: str) -> str:
    """Read one tmux format field for ``name`` through raw tmux, anchored.

    Raw tmux because these are facts *about* tmux that shellbox deliberately does not
    expose: ``#{session_id}`` is the value §9.1 rejects as an identity, and a test that
    could only see it through shellbox could not show why it was rejected.

    CRITICAL: ``display-message`` returns **rc=0 with empty output for any nonexistent target**
    (spike F6), so an rc check on it is worthless -- and an unresolved target here would hand
    two callers ``""`` and ``""``, which compare equal. That is the same shape of defect this
    module exists to guard against, so the empty case is rejected rather than returned.
    """
    result = tmux.raw("display-message", "-p", "-t", f"={name}:", format_field)
    assert result.rc == 0, f"could not read {format_field} for {name!r}: {result.stderr!r}"
    value = result.stdout_raw.split("\n", 1)[0]
    assert value, (
        f"{format_field} expanded EMPTY for {name!r}: display-message is rc=0 for a target "
        "that does not resolve, so this is an absent session, not a value to compare"
    )
    return value


# --------------------------------------------------------------------------------------
# T-CONC-1
# --------------------------------------------------------------------------------------

CREATE_RACERS = 8


def test_exactly_one_of_eight_concurrent_creates_reports_created_true(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """T-CONC-1. Eight processes, one name, one winner -- and the winner's session is usable.

    ``exactly one``, not ``at most one``: the second is satisfied by zero, and zero means
    every agent in the pool failed to get a shell. The losers are allowed either answer the
    design documents -- an idempotent ``created: false`` (the existing session matched), or
    ``already_exists`` (the winner was still inside its own create chain, so its session was
    not yet stamped and reusing it would have handed out a shell shellbox could not prove it
    owned). What they are NOT allowed is a second ``created: true``, a ``tmux_error``, or a
    duplicate session.
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "contended"
    arguments: dict[str, object] = {"name": name, "cwd": str(tmp_path)}

    outcomes = concurrent_calls(harness, [("shell_create", dict(arguments))] * CREATE_RACERS)

    winners = [out for out in outcomes if not out.is_error and out.data["created"] is True]
    assert len(winners) == 1, (
        f"expected EXACTLY one created:true out of {CREATE_RACERS}, got {len(winners)}. "
        f"Zero means every caller failed, which 'at most one' would have accepted. "
        f"Outcomes: {[out.text for out in outcomes]}"
    )
    incarnation = str(winners[0].data["incarnation"])
    assert incarnation, "the winning create returned an empty incarnation"

    adopters = [out for out in outcomes if not out.is_error and out.data["created"] is False]
    already = [out for out in outcomes if out.is_error]
    assert len(adopters) + len(already) == CREATE_RACERS - 1
    for out in adopters:
        # An adopter must hand back the WINNER's session, not merely report success.
        assert out.data["incarnation"] == incarnation
        assert Path(str(out.data["cwd"])).resolve() == tmp_path.resolve()
    for out in already:
        assert out.code == "already_exists", (
            f"a losing create failed with {out.code!r}; the only documented loss is "
            f"already_exists: {out.text}"
        )

    # One session, not eight, and the survivor is the winner's.
    assert tmux_server.sessions() == [name]

    # ...and it is intact and USABLE. The oracle is the file the pane's process wrote:
    # `shell_send` returns as soon as tmux accepts the paste, and the pane echoes the
    # command line whether or not the shell ever ran it.
    token = f"CONC1-{uuid.uuid4().hex[:12]}"
    marker = tmp_path / "conc1"
    listed, sent = run_calls(
        harness,
        [
            ("shell_list", {}),
            ("shell_send", {"session": name, "text": f"echo {token} > {marker}\n"}),
        ],
    )
    entries = {entry["tmux_name"]: entry for entry in listed.data["sessions"]}
    assert list(entries) == [name]
    assert entries[name]["foreign"] is False
    assert entries[name]["incarnation"] == incarnation
    assert sent.data["incarnation"] == incarnation
    assert token.encode() in await_file(
        str(marker), lambda data: token.encode() in data, timeout=15.0, what="the T-CONC-1 token"
    )


# --------------------------------------------------------------------------------------
# T-CONC-2
# --------------------------------------------------------------------------------------

# Four senders rather than M29's two: same total bytes, four times the opportunity to
# interleave. The total stays well under the 4096-byte tty input queue, so a full queue
# cannot break a run and be misread as an interleaving defect.
SEND_RACERS = "ABCD"
PAYLOAD_BYTES = 300
READY = b"READY\n"


def test_concurrent_sends_to_one_session_arrive_as_unbroken_runs(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """T-CONC-2. Four processes paste into one pane at once; each payload arrives whole.

    WARNING: **The reader pane is RAW mode (``stty -icanon``) and that is not optional.** A
    canonical-mode pane buffers a line until the newline and destroys anything past the
    line-discipline limit -- dropped on macOS, silently TRUNCATED on Linux -- so a
    canonical-mode version of this test would show mangled payloads that look exactly like
    an interleaving bug and are not one. See ``tests/conftest.py``'s warning: the two reader
    panes test different things and must never be unified.

    The assertion is against **the file the pane process wrote**, never ``capture-pane``:
    the pane renders, wraps and normalises, so a screen scrape is not an oracle for bytes.
    """
    harness = make_harness(tmux_server, tmp_path)
    delivered = tmp_path / "delivered"
    name = "shared"
    # Created through the adapter, not `shell_create`: the raw-mode reader needs a pane
    # COMMAND, and the MCP tool surface deliberately does not expose one (a session is a
    # shell). The adapter stamps the incarnation exactly as `shell_create` does, so the
    # sends below still go through the real tool over stdio -- which is the path under test.
    tmux_server.adapter().create(name, cwd=str(tmp_path), command=raw_reader(str(delivered)))

    # Sentinel-and-poll handshake, so the race starts against a pane that is already
    # READING. Without it a payload could be measured against a `cat` that had not yet
    # replaced the shell, and the shell's line discipline is a different test.
    run_calls(harness, [("shell_send", {"session": name, "text": READY.decode()})])
    await_file_bytes(str(delivered), len(READY), timeout=15.0)

    payloads = {char: char * PAYLOAD_BYTES for char in SEND_RACERS}
    outcomes = concurrent_calls(
        harness,
        [("shell_send", {"session": name, "text": payloads[char]}) for char in SEND_RACERS],
    )
    for char, out in zip(SEND_RACERS, outcomes, strict=True):
        assert out.data["submitted_bytes"] == PAYLOAD_BYTES, f"sender {char} was refused"

    total = len(READY) + PAYLOAD_BYTES * len(SEND_RACERS)
    data = await_file_bytes(str(delivered), total, timeout=30.0)

    assert data[: len(READY)] == READY, "the handshake bytes were not delivered intact"
    assert len(data) == total, f"delivered {len(data)} bytes, expected exactly {total}"
    tail = data[len(READY) :]
    runs = [bytes(group) for _, group in itertools.groupby(tail)]
    # Four runs of 300 identical bytes, in ANY order: the ORDER is genuinely unspecified
    # (there is no cross-process serialisation and none is claimed), the ATOMICITY is not.
    # Interleaving would split a payload and show up here as more than four runs.
    assert [len(run) for run in runs] == [PAYLOAD_BYTES] * len(SEND_RACERS), (
        f"payloads interleaved: expected {len(SEND_RACERS)} unbroken runs of {PAYLOAD_BYTES} "
        f"bytes, got run lengths {[len(run) for run in runs]}"
    )
    assert {run[:1].decode() for run in runs} == set(SEND_RACERS)


# --------------------------------------------------------------------------------------
# T-CONC-3
# --------------------------------------------------------------------------------------


def test_an_incarnation_mismatch_is_detectable_after_a_kill_and_recreate(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """T-CONC-3. B replaces the session under A's feet; A can TELL. It is not prevented.

    Three claims, in the order that makes the third meaningful:

    1. ``#{session_id}`` is useless for this. Killing the last session exits the tmux
       server, so the replacement is ``$0`` all over again -- identical to the session it
       replaced (M27). This is the measurement ``@shellbox_incarnation`` exists because of.
    2. The two incarnations are non-empty and DISTINCT. Asserted before anything is compared:
       an equality test two empty strings can satisfy is not an identity check.
    3. A's ``shell_send`` **succeeds** and reports the NEW incarnation. That is detection --
       the caller compares it against the value it was holding and knows it was misdelivered.
       shellbox does not and cannot prevent this: tmux has no compare-and-swap, so the window
       between resolving a session and pasting into it stays open (§9.1, R12).
    """
    harness = make_harness(tmux_server, tmp_path)
    name = "rotating"

    # A creates.
    (a_created,) = run_calls(harness, [("shell_create", {"name": name, "cwd": str(tmp_path)})])
    first = str(a_created.data["incarnation"])
    first_session_id = tmux_format(tmux_server, name, "#{session_id}")

    # B -- a different process -- kills it and recreates it under the same name.
    b_killed, b_created = run_calls(
        harness,
        [("shell_kill", {"session": name}), ("shell_create", {"name": name, "cwd": str(tmp_path)})],
    )
    assert b_killed.data["killed"] is True
    second = str(b_created.data["incarnation"])
    second_session_id = tmux_format(tmux_server, name, "#{session_id}")

    # Claim 1. The `$N` shape is asserted too: it is what makes "the counter reset" the
    # explanation rather than "both reads returned nothing".
    assert re.fullmatch(r"\$\d+", first_session_id), first_session_id
    assert re.fullmatch(r"\$\d+", second_session_id), second_session_id
    assert second_session_id == first_session_id, (
        f"#{{session_id}} changed ({first_session_id} -> {second_session_id}), so this lane "
        "did not reproduce M27's server-scoped reset and the rest of this test would be "
        "asserting against a weaker premise than §9.1 describes"
    )
    # Claim 2. Non-empty FIRST, on both sides, then distinct.
    assert first and second, f"an empty incarnation is never a match: {first!r} / {second!r}"
    assert first != second, (
        "the recreated session reused the incarnation of the session it replaced; nothing "
        "downstream could then detect a misdelivery"
    )

    # Claim 3. A is still holding `first`.
    (a_sent,) = run_calls(harness, [("shell_send", {"session": name, "text": "echo late\n"})])
    # It SUCCEEDED. This is the honest part: the send landed in B's session. shellbox detects,
    # it does not prevent, and a test asserting `not_found` here would be asserting a
    # guarantee the design explicitly disclaims.
    assert a_sent.is_error is False, a_sent.text
    targeted = str(a_sent.data["incarnation"])
    assert targeted == second
    assert targeted != first, (
        "shell_send reported the incarnation the caller was already holding, so a "
        "misdelivery into a recreated session would be invisible to it"
    )


def test_two_unstamped_sessions_are_not_each_others_incarnation_match(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """The r2 defect, made executable: ``"" == ""`` must never read as an identity match.

    Two sessions created with raw tmux, so neither carries ``@shellbox_incarnation`` -- the
    same state as a session mid-create (§9.2's stamp window) or one shellbox never made. If
    an empty incarnation counted as a value, these two *distinct* sessions would compare
    equal to each other and to every other unstamped session on the host.

    So the property is asserted at the surface: **no tool ever hands a caller an empty
    incarnation to compare.** ``shell_list`` reports ``null`` plus ``foreign: true``, and
    ``shell_send``/``shell_kill`` refuse with ``not_found`` rather than acting on a session
    shellbox cannot prove it owns.
    """
    harness = make_harness(tmux_server, tmp_path)
    for ghost in ("ghosta", "ghostb"):
        result = tmux_server.raw(
            "new-session", "-d", "-s", ghost, "-x", "80", "-y", "24", "-c", str(tmp_path), "sh"
        )
        assert result.rc == 0, result.stderr

    mine, listed, sent, killed = run_calls(
        harness,
        [
            # A positive control in the same inventory: without it, "everything is foreign"
            # would pass these assertions too.
            ("shell_create", {"name": "mine", "cwd": str(tmp_path)}),
            ("shell_list", {}),
            ("shell_send", {"session": "ghosta", "text": "echo pwned\n"}),
            ("shell_kill", {"session": "ghostb"}),
        ],
    )
    entries = {entry["tmux_name"]: entry for entry in listed.data["sessions"]}
    assert set(entries) == {"ghosta", "ghostb", "mine"}
    for ghost in ("ghosta", "ghostb"):
        # `is None`, not falsy: `""` would satisfy a falsy check and is exactly the value
        # that made the r2 comparison pass.
        assert entries[ghost]["incarnation"] is None, entries[ghost]
        assert entries[ghost]["foreign"] is True
    assert entries["mine"]["incarnation"] == mine.data["incarnation"]
    assert entries["mine"]["foreign"] is False

    assert sent.code == "not_found"
    assert killed.code == "not_found"
    # And the refusals were refusals, not silent successes: both ghosts are untouched.
    assert sorted(tmux_server.sessions()) == ["ghosta", "ghostb", "mine"]
