"""`W41`/`A41` -- with no `SHELLBOX_DATABASE_URL`, the reaper is INERT: `NullRegistry`
(`ADR-36`) makes it report zero candidates even with live, unattended sessions on the
server, and every tool still works.

This lane cannot age a session past a real timeout (`W46`'s floor is 60s and nothing here
may spend a minute), so the assertion is on the sweep's own INFO log line's candidate
count -- which is what makes this criterion able to FAIL if the has-a-row filter rule were
ever deleted, even though no session here is actually old enough to reap.

`T-P5-NULL-REGISTRY-INERT`.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio
from conftest import TmuxServer, requires_tmux
from harness import call, make_harness, run_script

pytestmark = requires_tmux

_CANDIDATE_RE = re.compile(r"(\d+) candidate\(s\)")


def test_the_reaper_reports_zero_candidates_with_no_database(
    tmux_server: TmuxServer, tmp_path: Path
) -> None:
    """A41/T-P5-NULL-REGISTRY-INERT.

    `SHELLBOX_REAP_INTERVAL_SECONDS=10` (`W46`'s floor) so a sweep lands inside this test's
    deadline; `SHELLBOX_IDLE_TIMEOUT_SECONDS=60` (the other floor) is injected too, but is
    not what makes this falsifiable -- the candidate count is. `SHELLBOX_DATABASE_URL` is
    absent from `make_harness`'s base environment already (module docstring rule 2), so the
    server runs with a `NullRegistry`.
    """
    harness = make_harness(tmux_server, tmp_path)
    env = harness.env_with(SHELLBOX_REAP_INTERVAL_SECONDS="10", SHELLBOX_IDLE_TIMEOUT_SECONDS="60")
    name = "unattended"

    async def script(client):  # type: ignore[no-untyped-def]
        created = await call(client, "shell_create", {"name": name, "cwd": str(tmp_path)})
        assert created.data["created"] is True

        # Real wall-clock wait for at least one sweep to log. `Reaper.start()` sweeps ONCE
        # immediately, so this should land quickly regardless of the injected interval --
        # polled, never a fixed sleep, so a slow CI box just polls longer.
        deadline = anyio.current_time() + 25.0
        while anyio.current_time() < deadline and "candidate(s)" not in harness.stderr():
            await anyio.sleep(0.1)

        return await call(client, "shell_list")

    listed = run_script(harness, script, env=env, timeout=40.0)

    stderr = harness.stderr()
    counts = [int(match) for match in _CANDIDATE_RE.findall(stderr)]
    assert counts, f"no reap sweep line observed in stderr:\n{stderr}"
    assert all(count == 0 for count in counts), (
        f"expected zero candidates with no database (ADR-36's laptop-safety invariant); "
        f"saw {counts} in:\n{stderr}"
    )

    assert not listed.is_error, listed.text
    tmux_names = {entry["tmux_name"] for entry in listed.data["sessions"]}
    assert name in tmux_names, "the live, unattended session must still exist -- nothing was reaped"
