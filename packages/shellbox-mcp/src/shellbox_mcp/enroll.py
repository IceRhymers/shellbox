"""Enrollment: the E1-E7 sequence that puts this host in the inventory (plan §10).

`identity.py` answers *who this host is*. This module tells the registry, and keeps telling
it. The two are separate because they fail differently and at different times: identity is
resolved synchronously (nothing can serve a tool call without it), while everything here is
**best-effort and backgrounded**.

## The rule that shapes every function in this file

**Enrollment may never fail a tool call, and may never block the handshake.** An agent that
cannot get a shell because Lakebase is unreachable is a worse outcome than an inventory row
nobody reads (§9, R7). So every function here either returns a result object describing what
happened or logs and continues; none of them raise into a caller on the tool path, and
`start_enrollment` runs the whole sequence on a daemon thread.

That is also why the sequence is ordered the way it is. Each step's failure must leave the
steps before it intact:

```
E1  resolve host_id                       identity.py, already done by the caller
E2  resolve owner_email + reconcile       credential -> cache -> env -> defer
E3  cache it                              identity.py; happens inside E2
E4  upsert the hosts row                  first enrollment wins (enrolled_at preserved)
E5  reconcile orphans                     guarded: see `reconcile_orphans`
E6  record what we know about the host    tmux version/path, and ADR-8's sandbox_id story
E7  heartbeat                             last_seen_at advances while the process lives
```

E4 before E5 is not cosmetic: `sessions.host_id` is a foreign key to `hosts`, so orphaning a
session row before its host row exists cannot work. E2 before E4 is likewise forced --
`hosts.owner_email` is `NOT NULL`, and the whole point of D4 is that the row carries a real
principal rather than a placeholder.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from shellbox_registry import HostRecord, Registry, SessionRecord

from shellbox_mcp import identity, naming
from shellbox_mcp.tmux import TmuxAdapter

logger = logging.getLogger(__name__)

__all__ = [
    "EnrollmentResult",
    "Heartbeat",
    "enroll",
    "recover_host_id",
    "reconcile_orphans",
    "resolve_credential_email",
    "stamp_sessions",
    "start_enrollment",
]

STATUS_ACTIVE = "active"
STATUS_ORPHANED = "orphaned"

# E7. Short enough that Phase 5's staleness sweep sees a live host as live, long enough that
# 32 processes heartbeating do not become a write load of their own.
HEARTBEAT_SECONDS = 60.0

# E5's debounce. Orphan reconciliation is a bulk status write over every session row for this
# host; running it on every enrollment pass in a 32-process pool would be 32 identical sweeps.
RECONCILE_DEBOUNCE_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    """What one enrollment pass actually managed to do.

    Returned rather than raised, and every field is a fact rather than a promise, because the
    caller is a background thread whose only other option is a log line. `doctor` reads this
    shape too.
    """

    host_id: str
    owner_email: str | None
    """``None`` ⇒ E2d: enrollment deferred, nothing was written. Not an error."""
    enrolled: bool
    """True when the `hosts` row was written."""
    owner_source: str = "deferred"
    reconciled_owner: bool = False
    """True when a credential disagreed with the cache and the credential won (E2a)."""
    orphaned: int = 0
    stamped: int = 0
    recovered_from_tmux: bool = False
    error: str | None = None
    """Why enrollment did not complete. Present *with* partial results, not instead of them."""


# --------------------------------------------------------------------------------------
# D4 -- who created this sandbox
# --------------------------------------------------------------------------------------
def resolve_credential_email(*, timeout: float = 30.0) -> str | None:
    """The workspace user the sandbox's ambient credential authenticates as, or ``None``.

    This is D4, and it is the only way to answer "whose sandbox is this": the Lakebox API
    exposes **no owner field**, while the credential baked into `~/.databrickscfg` at boot
    authenticates as the sandbox's *creator* (measured -- `docs/sandbox-environment.md` §5).

    ⚠️ **This credential is a confused deputy, and on the measured sandbox it belongs to a
    workspace admin.** Any agent in the sandbox can act as that user, so a hostile agent can
    forge `owner_email`. That is R6, accepted only while access is default-open (D6), and it
    is a hard blocker for #7's ACL -- which must replace this with a per-host enrollment token
    rather than trusting a host-side stamp.

    Never raises, and never logs the credential. The SDK is imported lazily and its absence is
    an ordinary outcome: shellbox runs on developer laptops and in CI, where there is no
    Databricks credential and enrollment correctly defers.
    """
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.core import Config
    except ImportError:
        logger.debug("databricks-sdk is not installed; no credential-based owner_email")
        return None

    try:
        # A bound matters more here than anywhere else in this module: this is the one step that
        # talks to the network, and it runs at startup. Without it, an unreachable control plane
        # turns "enrollment is backgrounded" into a thread that never finishes and a `hosts` row
        # that never lands -- which looks exactly like a bug in the registry.
        #
        # Through `Config`, not as a `WorkspaceClient` kwarg -- the client does not accept it,
        # and an earlier version of this line passed it there with a `TypeError` fallback. That
        # "fallback" would have fired on every single call, so the bound this comment promises
        # would never once have applied. mypy caught it; a runtime test never would have, because
        # the degraded path is silent and correct-looking.
        client = WorkspaceClient(config=Config(http_timeout_seconds=timeout))
        user = client.current_user.me()
    except Exception as exc:  # noqa: BLE001 -- see the docstring: this may not raise
        # `str(exc)` from the SDK can carry a host and a request id but not the token itself;
        # the type alone would hide which of "no config", "expired", "no network" happened,
        # and that distinction is exactly what an operator needs. Never the credential.
        logger.info(
            "no owner_email from the ambient credential (%s: %s); enrollment will fall back "
            "to the identity cache, then SHELLBOX_OWNER_EMAIL, then defer",
            type(exc).__name__,
            exc,
        )
        return None

    email = (user.user_name or "").strip()
    if not email:
        logger.warning(
            "current_user.me() succeeded but returned no user_name; treating as no credential"
        )
        return None
    return email


# --------------------------------------------------------------------------------------
# W7f -- the tmux host stamp
# --------------------------------------------------------------------------------------
def recover_host_id(adapter: TmuxAdapter) -> str | None:
    """A `host_id` read back from the live tmux server, or ``None``.

    The point of this is narrow and worth stating: it covers **exactly** the case where losing
    `$HOME/.shellbox/host.json` would do real damage — the file is gone while tmux sessions are
    still running. Assigning a fresh id there re-keys every `session_id` (they are
    ``f"{host_id}:{tmux_name}"``), so live, addressable sessions become permanently
    unaddressable while their processes keep running. Recovering the stamp instead makes that
    self-healing.

    The complementary loss (a sandbox restart) destroys the tmux server but *keeps* `$HOME`, so
    between the two caches every case with live sessions is covered — and the case where both
    are gone has no live sessions to strand.

    **Disagreement is resolved deterministically rather than arbitrarily.** Sessions can
    legitimately carry different stamps: a host that re-keyed once, sessions created by a build
    that predates the stamp, or a genuine fork. Picking "the first one" would let two concurrent
    processes adopt different ids from the same disagreeing set — reintroducing the split this
    whole mechanism exists to prevent, through its own mitigation. So: exactly one distinct
    value wins; more than one logs CRITICAL and takes the lexicographically smallest, which is
    a choice every process makes identically even when the host is already broken.
    """
    try:
        sessions = adapter.list_sessions()
    except Exception as exc:  # noqa: BLE001 -- enrollment may not raise
        logger.debug("cannot list sessions to recover a host stamp (%s)", type(exc).__name__)
        return None

    stamps = {
        stamp for session in sessions if (stamp := adapter.read_host_stamp(session.tmux_name))
    }
    if not stamps:
        return None
    if len(stamps) == 1:
        return next(iter(stamps))

    chosen = min(stamps)
    logger.critical(
        "sessions on this tmux server carry %d DIFFERENT %s values (%s): this host has been "
        "re-keyed at least once, so some session rows are filed under an id no longer in use. "
        "Adopting %r (the lexicographically smallest, so every process agrees). Inspect the "
        "hosts table for duplicate rows.",
        len(stamps),
        "@shellbox_host_id",
        ", ".join(sorted(stamps)),
        chosen,
    )
    return chosen


def stamp_sessions(adapter: TmuxAdapter, host_id: str) -> int:
    """Stamp ``@shellbox_host_id`` on any session missing it. Returns how many were stamped.

    Existing sessions need this because sessions created before this code shipped carry no
    stamp at all, so on the first upgraded host recovery would find nothing. Stamping them
    during enrollment closes that window without touching the create path — where a fourth
    `set-option` would lengthen the chain §7.2 was transcribed from a spike to get right.
    """
    stamped = 0
    try:
        sessions = adapter.list_sessions()
    except Exception as exc:  # noqa: BLE001 -- enrollment may not raise
        logger.debug("cannot list sessions to stamp them (%s)", type(exc).__name__)
        return 0
    for session in sessions:
        if adapter.read_host_stamp(session.tmux_name) == host_id:
            continue
        if adapter.stamp_host_id(session.tmux_name, host_id):
            stamped += 1
    return stamped


# --------------------------------------------------------------------------------------
# E5 -- orphan reconciliation
# --------------------------------------------------------------------------------------
def reconcile_orphans(
    registry: Registry,
    adapter: TmuxAdapter,
    *,
    host_id: str,
    owner_email: str,
    expected_socket: str,
) -> int:
    """Mark rows `orphaned` when their tmux session is gone. Returns how many changed.

    This is the step that tells the truth after a sandbox restart: tmux's socket lives outside
    `$HOME`, so the server and every session die, and the rows that describe them are the only
    thing left claiming otherwise (R3).

    🔴 **The guard that makes this safe to run at all.** "No sessions on the server" and "I am
    looking at the wrong server" produce identical evidence — an empty list — and the two
    demand opposite responses. If *this* process resolved a different `socket_path` than the
    one the `hosts` row records (a different `$HOME`, an operator's `SHELLBOX_STATE_DIR`, a
    `sudo` invocation), then the live sessions are all on the *other* socket and orphaning every
    row would be a mass falsification of a healthy host's inventory. So a socket mismatch
    refuses to reconcile and logs CRITICAL: the mismatch is the bug, and reconciliation would
    bury it under a plausible-looking result.

    Takes no clock, deliberately: this function writes **no new timestamps**. Noticing that a
    session is gone is not activity on it, and stamping `last_activity_at` here would make a
    long-dead session look freshly used to #5's reaper -- which reads exactly that column.
    """
    stored = None
    try:
        stored = registry.get_host(host_id)
    except Exception as exc:  # noqa: BLE001 -- enrollment may not raise
        logger.warning("cannot read the hosts row to reconcile orphans: %s", exc)
        return 0

    if stored is not None and stored.tmux_socket and stored.tmux_socket != expected_socket:
        logger.critical(
            "REFUSING to reconcile orphans: host %s records tmux_socket %r but this process "
            "resolved %r. The sessions this host owns are on the other socket, so marking rows "
            "orphaned from here would falsify a healthy inventory. Fix SHELLBOX_TMUX_SOCKET / "
            "SHELLBOX_STATE_DIR (or $HOME) so both agree.",
            host_id,
            stored.tmux_socket,
            expected_socket,
        )
        return 0

    try:
        rows = registry.list_sessions_for_host(host_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cannot list session rows to reconcile orphans: %s", exc)
        return 0
    if not rows:
        return 0

    try:
        live = {session.tmux_name for session in adapter.list_sessions()}
    except Exception as exc:  # noqa: BLE001
        # NOT an empty set. `list_sessions` raises for everything except the two measured
        # no-server signatures, and treating a broken tmux as "no sessions" would orphan every
        # live session on the host on the strength of a parse error.
        logger.warning(
            "cannot enumerate live tmux sessions (%s: %s); leaving session rows untouched "
            "rather than orphaning them on the strength of a failed read",
            type(exc).__name__,
            exc,
        )
        return 0

    orphaned = 0
    for row in rows:
        if row.status == STATUS_ORPHANED or row.tmux_name in live:
            continue
        try:
            registry.upsert_session(
                SessionRecord(
                    session_id=row.session_id,
                    host_id=host_id,
                    tmux_name=row.tmux_name,
                    owner_email=row.owner_email or owner_email,
                    # NOT advanced: nothing happened to this session, we merely noticed. Moving
                    # it would make a long-dead session look freshly used to #5's reaper.
                    last_activity_at=row.last_activity_at,
                    last_read_at=row.last_read_at,
                    status=STATUS_ORPHANED,
                    cwd=row.cwd,
                    cols=row.cols,
                    rows=row.rows,
                    created_at=row.created_at,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not orphan session row %s: %s", row.session_id, exc)
            continue
        orphaned += 1

    if orphaned:
        logger.info(
            "marked %d session row(s) orphaned for host %s: their tmux sessions are gone "
            "(expected after a sandbox restart -- the tmux server does not survive one)",
            orphaned,
            host_id,
        )
    return orphaned


# --------------------------------------------------------------------------------------
# E6 -- what the host can say about itself
# --------------------------------------------------------------------------------------
def tmux_description(tmux_bin: str, *, timeout: float = 5.0) -> str:
    """``"<tmux -V output> at <path>"``, for the `hosts` row (E6, ADR-1).

    Recorded because these measurements are version-sensitive: §7 is transcribed from a spike
    run on tmux 3.6b and gated in CI on 3.4, and the sandbox image ships 3.4. When a host
    behaves oddly, "which tmux" is the first question and the row should already answer it.
    """
    try:
        result = subprocess.run(
            [tmux_bin, "-V"], capture_output=True, text=True, timeout=timeout, shell=False
        )
        version = result.stdout.strip() or result.stderr.strip() or "unknown"
    except (OSError, subprocess.SubprocessError) as exc:
        version = f"unavailable ({type(exc).__name__})"
    return f"{version} at {tmux_bin}"


def warn_about_autostop(sandbox_id: str | None) -> None:
    """E6's R8 warning -- and it says why it cannot check, rather than staying silent.

    Phase 5 owns keepalive; Phase 2 only warns. The honest version of that warning depends on
    ADR-8: picking this sandbox's row out of `databricks sandbox list` **requires** the
    `sandbox_id`, because the API is caller-scoped and returns every sandbox the caller owns
    with no locally-readable field to match on (`docs/sandbox-environment.md` §1). So on
    exactly the un-bootstrapped hosts ADR-6 exists to support, the check is impossible.

    An earlier plan revision had this warning simply not fire in that case, which would have
    made the failure mode that hurts users most -- autostop killing a session mid-build --
    silent on the hosts most likely to hit it.
    """
    if sandbox_id:
        logger.info(
            "sandbox_id is %r; idle-autostop settings are #5's to manage (Phase 2 records and "
            "warns only). If `noAutostop` is false this sandbox can stop mid-session.",
            sandbox_id,
        )
        return
    logger.warning(
        "cannot evaluate this sandbox's idle-autostop settings: sandbox_id is unknown, and a "
        "sandbox cannot learn its own id (the workspace API is caller-scoped with no local "
        "field to match on). Run `shellbox-mcp bootstrap` from outside to stamp it. Until "
        "then, an idle autostop can stop this sandbox mid-session and kill every tmux session."
    )


# --------------------------------------------------------------------------------------
# The sequence
# --------------------------------------------------------------------------------------
def enroll(
    registry: Registry,
    adapter: TmuxAdapter,
    *,
    state_dir: str,
    host_id: str,
    kind: str,
    tmux_socket: str,
    tmux_bin: str,
    sandbox_id: str | None = None,
    gateway_host: str | None = None,
    env_email: str | None = None,
    credential_email: str | None | Callable[[], str | None] = resolve_credential_email,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> EnrollmentResult:
    """Run E2-E6 once. Never raises.

    ``credential_email`` is a callable by default so the network call happens *inside* this
    function, on the background thread, rather than in the caller that decides whether to spawn
    one. Tests pass a value or ``None`` directly.
    """
    credential = credential_email() if callable(credential_email) else credential_email

    # E2/E3. The credential wins over the cache and corrects it (E2a); a cached value serves
    # when no credential is available (E2b), which after a PAT reset plus a reboot is the ONLY
    # source there is, because the CLI's OAuth token cache is boot-templated into wiped /run.
    owner = identity.resolve_owner_email(
        state_dir, credential_email=credential, env_email=env_email
    )
    if owner.owner_email is None:
        # E2d. Nothing is written -- not even a placeholder. `hosts.owner_email` is NOT NULL and
        # is what #7's ACL will filter on, so a fake principal here accumulates real rows under
        # a name that either grants to nobody or matches whoever ends up owning the string.
        return EnrollmentResult(
            host_id=host_id,
            owner_email=None,
            enrolled=False,
            owner_source=owner.source,
            error="no resolvable owner_email; enrollment deferred (E2d)",
        )

    stamped = stamp_sessions(adapter, host_id)
    warn_about_autostop(sandbox_id)
    timestamp = now()

    # E4. `enrolled_at` is passed but preserved on conflict by the registry, so the first
    # enrollment's timestamp survives every later pass ("first enrollment wins").
    try:
        registry.upsert_host(
            HostRecord(
                host_id=host_id,
                kind=kind,
                owner_email=owner.owner_email,
                last_seen_at=timestamp,
                status=STATUS_ACTIVE,
                sandbox_id=sandbox_id,
                gateway_host=gateway_host,
                # E6, and E5's guard depends on it being recorded: a later pass compares this
                # against its own resolved socket and refuses to orphan on a mismatch.
                tmux_socket=tmux_socket,
                enrolled_at=timestamp,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- enrollment may not raise
        logger.warning(
            "could not write the hosts row for %s (%s: %s); shell tools are unaffected and the "
            "next enrollment pass will retry",
            host_id,
            type(exc).__name__,
            exc,
        )
        return EnrollmentResult(
            host_id=host_id,
            owner_email=owner.owner_email,
            enrolled=False,
            owner_source=owner.source,
            reconciled_owner=owner.reconciled,
            stamped=stamped,
            error=f"registry unavailable ({type(exc).__name__})",
        )

    logger.info(
        "enrolled host %s (%s) as %s [%s]; tmux %s",
        host_id,
        kind,
        owner.owner_email,
        f"sandbox {sandbox_id}" if sandbox_id else "no sandbox_id",
        tmux_description(tmux_bin),
    )

    # E5, after E4 because `sessions.host_id` references `hosts`.
    orphaned = reconcile_orphans(
        registry,
        adapter,
        host_id=host_id,
        owner_email=owner.owner_email,
        expected_socket=tmux_socket,
    )
    return EnrollmentResult(
        host_id=host_id,
        owner_email=owner.owner_email,
        enrolled=True,
        owner_source=owner.source,
        reconciled_owner=owner.reconciled,
        orphaned=orphaned,
        stamped=stamped,
    )


@dataclass
class Heartbeat:
    """E7: advance `last_seen_at` while this process lives, so #5 can spot a dead host.

    A **daemon** thread with an event-based sleep, and both halves matter for a stdio server: a
    non-daemon thread would hang process exit after the client closes stdin (invisible to the
    client except as a child that never reaps), and `Event.wait` means `stop()` returns
    promptly instead of after a full interval.
    """

    registry: Registry
    host_id: str
    kind: str
    owner_email: str
    tmux_socket: str
    sandbox_id: str | None = None
    gateway_host: str | None = None
    interval: float = HEARTBEAT_SECONDS
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    beats: int = field(default=0, init=False)

    def beat(self) -> bool:
        """One heartbeat. Returns whether it landed. Never raises.

        `GREATEST(excluded.last_seen_at, hosts.last_seen_at)` in the registry means a delayed
        beat can never move the timestamp backwards, so several processes beating at different
        offsets is safe by construction rather than by coordination.
        """
        try:
            self.registry.upsert_host(
                HostRecord(
                    host_id=self.host_id,
                    kind=self.kind,
                    owner_email=self.owner_email,
                    last_seen_at=self.now(),
                    status=STATUS_ACTIVE,
                    sandbox_id=self.sandbox_id,
                    gateway_host=self.gateway_host,
                    tmux_socket=self.tmux_socket,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- a heartbeat may not raise
            logger.debug("heartbeat for %s failed (%s)", self.host_id, type(exc).__name__)
            return False
        self.beats += 1
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.beat()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"shellbox-heartbeat-{self.host_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


def start_enrollment(
    registry: Registry,
    adapter_factory: Callable[[], TmuxAdapter],
    *,
    state_dir: str,
    host_id: str,
    kind: str,
    tmux_socket: str,
    tmux_bin: str,
    sandbox_id: str | None = None,
    gateway_host: str | None = None,
    env_email: str | None = None,
    heartbeat: bool = True,
) -> threading.Thread:
    """Run enrollment on a daemon thread and start the heartbeat if it succeeded.

    🔴 **This is the function that keeps the promise "enrollment never blocks the handshake".**
    Everything in E2-E7 can be slow or can hang: `current_user.me()` is a network call, and a
    registry pointed at an unreachable DSN waits out a connect timeout. Doing any of it before
    `FastMCP.run()` would delay `initialize`/`tools/list` — which a client reports as a failed
    handshake, with no indication that the cause was an inventory write.

    Returns the thread so a test can join it. Nothing on the tool path ever does.
    """

    def run() -> None:
        # A fresh adapter, because this thread runs concurrently with tool calls and
        # `TmuxAdapter` is constructed per use everywhere else for the same reason.
        adapter = adapter_factory()
        result = enroll(
            registry,
            adapter,
            state_dir=state_dir,
            host_id=host_id,
            kind=kind,
            tmux_socket=tmux_socket,
            tmux_bin=tmux_bin,
            sandbox_id=sandbox_id,
            gateway_host=gateway_host,
            env_email=env_email,
        )
        if not (heartbeat and result.enrolled and result.owner_email):
            return
        Heartbeat(
            registry=registry,
            host_id=host_id,
            kind=kind,
            owner_email=result.owner_email,
            tmux_socket=tmux_socket,
            sandbox_id=sandbox_id,
            gateway_host=gateway_host,
        ).start()

    thread = threading.Thread(target=run, name="shellbox-enroll", daemon=True)
    thread.start()
    return thread


def session_id_for(host_id: str, tmux_name: str) -> str:
    """``naming.session_id``, re-exported so `enroll.py` and `server.py` cannot diverge."""
    return naming.session_id(host_id, tmux_name)
