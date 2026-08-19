"""E5b against a live Postgres: the FK the unit tests cannot exercise (issue #24).

`tests/unit/test_enroll.py` proves E5b's branch logic with a fake registry, but a fake has
no foreign key, so it cannot reproduce #24's actual failure: a `sessions` INSERT rejected
because its `hosts` row does not yet exist. This file does, on the real schema.

The shape mirrors `test_constraints.py::test_sessions_host_id_foreign_key_rejects_an_unknown_host`:
first prove the constraint bites (an INSERT with no host row raises), then prove E5b lands the
row once the host row is present.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from shellbox_mcp import identity
from shellbox_mcp.enroll import _recover_enrollment, reproject_live_sessions, session_id_for
from shellbox_registry import HostRecord, SessionRecord
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.registry

HOST_ID = "h-reproject"
TMUX_NAME = "build"
OWNER = "owner@example.com"
NOW = dt.datetime(2026, 7, 31, tzinfo=dt.UTC)

# tmux hands E5b unscaled epoch SECONDS; assert they land as the matching UTC datetimes.
CREATED_AT_EPOCH = 1_722_400_000  # 2024-07-31T04:26:40Z
ACTIVITY_AT_EPOCH = 1_722_400_500  # 2024-07-31T04:35:00Z


class _OneLiveSession:
    """A minimal tmux adapter that lists exactly one non-foreign live session."""

    def list_sessions(self) -> list[object]:
        return [
            type(
                "S",
                (),
                {
                    "tmux_name": TMUX_NAME,
                    "created_at": CREATED_AT_EPOCH,
                    "last_activity_at": ACTIVITY_AT_EPOCH,
                    "cols": 120,
                    "rows": 40,
                    "cwd": "/work",
                    "incarnation": "inc-1",
                    "foreign": False,
                },
            )()
        ]


def test_reprojection_lands_a_row_that_the_fk_first_rejects(registry) -> None:
    sid = session_id_for(HOST_ID, TMUX_NAME)

    # 1. The real `sessions_host_id_fkey` bites: no host row means the INSERT is rejected. This
    #    is exactly the swallowed failure behind #24 on a cold host.
    with pytest.raises(IntegrityError):
        registry.upsert_session(
            SessionRecord(
                session_id=sid,
                host_id=HOST_ID,
                tmux_name=TMUX_NAME,
                owner_email=OWNER,
                status="live",
                created_at=NOW,
                last_activity_at=NOW,
            )
        )

    # 2. Land the FK target host row (E4).
    registry.upsert_host(
        HostRecord(
            host_id=HOST_ID,
            kind="lakebox",
            owner_email=OWNER,
            last_seen_at=NOW,
            status="active",
            enrolled_at=NOW,
        )
    )

    # 3. E5b re-projects the live session that lost the race.
    count = reproject_live_sessions(
        registry,
        _OneLiveSession(),
        host_id=HOST_ID,
        owner_email=OWNER,
    )
    assert count == 1

    # 4. The row is present, live, correctly owned, with tmux's real epoch clocks in UTC.
    row = registry.get_session(sid)
    assert row is not None
    assert (row.status, row.owner_email, row.host_id) == ("live", OWNER, HOST_ID)
    assert row.created_at == dt.datetime.fromtimestamp(CREATED_AT_EPOCH, tz=dt.UTC)
    assert row.last_activity_at == dt.datetime.fromtimestamp(ACTIVITY_AT_EPOCH, tz=dt.UTC)
    assert [r.session_id for r in registry.list_sessions_for_host(HOST_ID)] == [sid]


class _ColdHostAdapter:
    """A tmux adapter with exactly one live, non-foreign, unstamped session -- enough to drive
    the full `enroll()` path (`stamp_sessions` + E5b), unlike `_OneLiveSession` which is only
    ever handed to `reproject_live_sessions` directly."""

    def list_sessions(self) -> list[object]:
        return [
            type(
                "S",
                (),
                {
                    "tmux_name": TMUX_NAME,
                    "created_at": CREATED_AT_EPOCH,
                    "last_activity_at": ACTIVITY_AT_EPOCH,
                    "cols": 120,
                    "rows": 40,
                    "cwd": "/work",
                    "incarnation": "inc-1",
                    "foreign": False,
                },
            )()
        ]

    def read_host_stamp(self, name: str) -> str | None:
        return None  # unstamped, so `stamp_sessions` stamps it

    def stamp_host_id(self, name: str, host_id: str) -> bool:
        return True


def test_recovery_reprojects_the_first_session_when_the_owner_warms_late(
    registry, tmp_path: Path
) -> None:
    """#26 case 1 end-to-end against the real FK: the owner is unresolvable at boot (so E2d
    writes nothing and the first live session has no row), then resolves on a later pass.
    `_recover_enrollment` re-runs `enroll()`, E4 lands the host row, and E5b re-projects the
    session that never had one -- with tmux's real epoch clocks and a satisfied FK."""
    host_id = "h-reproject-recover"
    sid = session_id_for(host_id, TMUX_NAME)
    identity.resolve_host_id(str(tmp_path))  # create host.json so the owner can be cached

    # No host row and no session row exist yet: this is a cold host mid-boot.
    assert registry.get_host(host_id) is None
    assert registry.get_session(sid) is None

    warming = iter([None, OWNER])  # deferred once, then the credential appears

    result = _recover_enrollment(
        registry,
        _ColdHostAdapter,
        state_dir=str(tmp_path),
        host_id=host_id,
        kind="lakebox",
        tmux_socket="/s.sock",
        tmux_bin="tmux",
        credential_email=lambda: next(warming, OWNER),
        now=lambda: NOW,
        sleep=lambda _interval: False,  # never wait, never self-stop
        sdk_present=True,
    )

    assert result.enrolled and result.owner_email == OWNER
    assert result.reprojected == 1

    row = registry.get_session(sid)
    assert row is not None
    assert (row.status, row.owner_email, row.host_id) == ("live", OWNER, host_id)
    assert row.created_at == dt.datetime.fromtimestamp(CREATED_AT_EPOCH, tz=dt.UTC)
    assert row.last_activity_at == dt.datetime.fromtimestamp(ACTIVITY_AT_EPOCH, tz=dt.UTC)
    # The FK target the cold-host race could not satisfy is now present.
    assert registry.get_host(host_id) is not None
