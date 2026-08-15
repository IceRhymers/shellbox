"""`W41`/`A28` -- two concurrent reaps of the same session both succeed safely, with no
election (`ADR-26`) and no lock (`ADR-37`'s "what absorbs the concurrent-reap race").

`T-P5-REAP-RACE`.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from conftest import PlantedRegistry, TmuxServer, await_output_timeout_elapsed, requires_tmux
from shellbox_mcp.reaper import Reaper

pytestmark = requires_tmux

TIMEOUT = 2
HOST_ID = "reap-race-host"


def _unique(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _old_row_time() -> datetime:
    return datetime.now(UTC) - timedelta(seconds=TIMEOUT * 30)


def test_two_concurrent_reapers_against_one_session_both_succeed_safely(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """A28/T-P5-REAP-RACE.

    Two `Reaper` instances against ONE real tmux server holding exactly one session (`tmux_server`
    is private and empty per test -- `tests/conftest.py`'s docstring), so the loser's read
    exercises the "no server running" classification (killing the last session exits the
    server, `tmux.py:1058-1062`) and not `can't find session`.

    Each `Reaper` gets its OWN `PlantedRegistry`, pre-populated with the same row -- mirroring
    two independent `shellbox-mcp` processes that each read the row from a SHARED database
    before racing to reap it. What makes this safe without coordination is `TmuxAdapter.kill`'s
    idempotence and `upsert_session`'s idempotent-by-value write (`ADR-28`, `ADR-37`), not
    registry sharing, so a fake registry per side is a faithful enough test double.
    """
    adapter = tmux_server.adapter()
    name = _unique("race")
    adapter.create(name, cwd=str(tmp_path), command=["sh"])

    row_time = _old_row_time()
    registry_a = PlantedRegistry(host_id=HOST_ID)
    registry_a.plant(name, last_activity_at=row_time)
    registry_b = PlantedRegistry(host_id=HOST_ID)
    registry_b.plant(name, last_activity_at=row_time)

    await_output_timeout_elapsed(adapter, name, TIMEOUT)

    reaper_a = Reaper(
        registry_a, tmux_server.adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999
    )
    reaper_b = Reaper(
        registry_b, tmux_server.adapter, host_id=HOST_ID, timeout=TIMEOUT, interval=9999
    )

    errors: list[BaseException] = []

    def run(reaper: Reaper) -> None:
        try:
            reaper.sweep()
        except BaseException as exc:  # noqa: BLE001 -- the assertion is that this never fires
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(reaper,)) for reaper in (reaper_a, reaper_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads), "a sweep hung during the race"
    assert not errors, f"a concurrent reap raised instead of resolving to killed=False: {errors}"
    assert name not in tmux_server.sessions(), "the session must be gone -- one side reaped it"

    # Constraint 1 (`W41`): the registry write happens ONLY when `kill` returned True. Exactly
    # one side's `kill` call could have been the one that actually removed the session; the
    # other saw it already gone (a `False`, never an exception) and wrote nothing at all.
    total_writes = len(registry_a.written) + len(registry_b.written)
    assert total_writes == 1, (
        f"expected exactly one side to have written the reap (the winner); got "
        f"{len(registry_a.written)} + {len(registry_b.written)} = {total_writes}"
    )
