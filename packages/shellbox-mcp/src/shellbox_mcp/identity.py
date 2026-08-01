"""Host identity: who this host is, and whose it is (ADR-6, ADR-8; plan §10 E1-E3).

Two separately-resolved questions, deliberately kept apart because they fail differently:

* **`host_id`** — *which host is this?* Self-assigned and remembered. Never derived.
* **`owner_email`** — *whose is it?* Resolved from a credential and reconciled against
  the cache, because the credential is the more recent fact (E2).

## Why `host_id` is self-assigned rather than derived

A Databricks Sandbox **cannot learn which sandbox it is.** Measured, and not for lack of
looking (`docs/sandbox-environment.md` §1): not in the environment, not on disk, not in the
hostname, not even in PID 1's environment. The only source is the workspace API, which is
*caller-scoped* — it returns every sandbox the caller owns, with no locally-readable field
to match "which of these am I."

The obvious fallback is worse than no fallback. `/etc/machine-id` is baked into the image
(§2), so a `host_id` derived from it is **identical on every sandbox from that image**: not a
weak identity, but one `hosts` row shared by the entire fleet, each host overwriting the
others' `owner_email`. That is why this module assigns a uuid4 and why `_machine_id()` no
longer exists anywhere in the package.

So: `sandbox_id` is *injected* by the bootstrap path (which runs from outside and does know
it, ADR-8) and is a nullable **property**, never part of the identity.

## Concurrency: the thing to understand before changing anything here

1-32 MCP processes run against one tmux server and one state directory — an invariant issue #2
calls "mandatory, not stylistic". Every process must end up with the **same** `host_id`, and it
must equal what is in the file, because `session_id` is `f"{host_id}:{tmux_name}"`: a second id
means one sandbox split across N `hosts` rows, session rows for a shared tmux server filed
under different hosts, and each process rejecting its siblings' live session ids as
`invalid_name`.

Three mechanisms, in increasing cost, and the rule for which to use:

1. **Creating an identity where there is none — `os.link`.** `_create_or_adopt` writes the full
   content to a staging file and then links it into place. `link` is atomic and fails with
   ``EEXIST``, so exactly one process wins and the rest **adopt the winner**. Unlike ``O_EXCL``
   on the final path, the name appears only once the content behind it is complete, so a loser
   can never read a half-written identity.
2. **Reading — one `read_text`.** `_load_cache` returns a state *and* the parsed content from a
   single observation. Deciding anything by asking two questions about the same file is how a
   concurrent winner's file came to look corrupt to a loser.
3. **Mutating a file that already exists — `_exclusive`.** Quarantining corruption, overriding
   the id from a tmux stamp, replacing an empty file, recording a property: all of these are
   *multi-step transactions* (read, decide, write), and neither (1) nor (2) arbitrates them.
   Two processes quarantining concurrently destroyed each other's freshly-assigned identities
   and split one sandbox into six. So every mutation of an existing file takes the lock,
   **re-reads under it**, and re-derives its decision from that read.

The invariant all three exist to serve, and the one to assert when adding tests: **every
process's returned `host_id` equals the one in the file.**
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "HOST_JSON_NAME",
    "cached_owner_email",
    "HostIdentity",
    "IdentityError",
    "OwnerResolution",
    "lakebox_kind",
    "resolve_host_id",
    "resolve_owner_email",
]

HOST_JSON_NAME = "host.json"

# The two things that let a sandbox prove it IS a Lakebox, even though nothing lets it prove
# WHICH Lakebox (`docs/sandbox-environment.md` §1). `hosts.kind` is NOT NULL and both of its
# former producers were the `lakebox:`/`unknown:` host-id prefixes ADR-6 deleted, so this is
# now the only predicate behind that column.
_LAKEBOX_MARKERS = (Path("/etc/lakebox"),)
_LAKEBOX_PID1 = "sandbox-daemon"

KIND_LAKEBOX = "lakebox"
KIND_UNKNOWN = "unknown"

# How long a process that lost the exclusive create waits for the winner to finish writing.
# Under link-after-write the winner's content is complete before the name exists, so this is
# only reachable during a rolling upgrade from a build that used `O_EXCL` on the final path,
# where a genuinely empty file can be observed mid-write. 0.5s.
_ADOPT_ATTEMPTS = 25
_ADOPT_BACKOFF = 0.02

# Bounded outer loop: each pass either resolves or discovers the file changed underneath and
# re-reads. Bounded rather than `while True` so a pathological environment fails loudly instead
# of spinning inside an MCP server's startup.
_RESOLVE_ATTEMPTS = 8

_LOCK_SUFFIX = ".lock"
_LOCK_BACKOFF = 0.02
# The critical section is a couple of filesystem calls, so a lock older than this belonged to a
# process that died holding it. Breaking it is safe *because* the final write still goes through
# `_create_or_adopt`: two processes both believing they hold the lock still cannot both assign.
_LOCK_STALE_SECONDS = 10.0


class _CacheState(Enum):
    """Why a cache read produced no identity. Not cosmetic: only ``CORRUPT`` is preserved, and
    preserving the wrong thing re-keys every session on the host."""

    OK = "ok"
    ABSENT = "absent"
    """No file, or a file we could not read at all (permissions, I/O). Never touched."""
    EMPTY = "empty"
    """Present but blank. Never produced by this module, so it means a stray `touch` or a
    crashed writer from an older build. Nothing in it to preserve — so it is replaceable."""
    CORRUPT = "corrupt"
    """Complete content that is not a usable identity. Preserved, never overwritten in place."""


class IdentityError(Exception):
    """No identity could be established, and guessing would be worse than failing.

    Deliberately rare. A missing cache is the normal first-boot path and an unusable one is
    recoverable; this is for the cases where proceeding would have to invent a second identity
    for a host that may already have live sessions filed under its first one.
    """


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Resolved host identity. ``sandbox_id``/``gateway_host`` are properties, not identity."""

    host_id: str
    kind: str
    assigned: bool
    """True when this process minted the id; False when it adopted an existing one. Exposed
    because "who assigned it" is the one thing a concurrency test can assert on -- exactly one
    winner across N processes."""
    source: str
    """``env`` | ``cache`` | ``tmux`` | ``assigned`` -- for logging and `doctor`."""
    sandbox_id: str | None = None
    gateway_host: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerResolution:
    """Outcome of E2. ``owner_email`` is ``None`` only in the E2d "defer" case."""

    owner_email: str | None
    source: str
    """``credential`` | ``cache`` | ``env`` | ``deferred``."""
    reconciled: bool = False
    """True when a credential disagreed with the cache and the credential won (E2a)."""


# --------------------------------------------------------------------------------------
# What makes a host_id usable at all
# --------------------------------------------------------------------------------------
def _host_id_problem(value: object) -> str | None:
    """Why ``value`` cannot be a `host_id`, or ``None`` if it can.

    One predicate, applied to **every** source. It used to live inside the cache reader, so the
    two inputs that bypass the cache -- ``$SHELLBOX_HOST_ID`` and a `host_id` recovered from a
    tmux user option -- were never checked, and a stamp of `lakebox:abc` or a value with a TAB
    in it was accepted, persisted, and only detonated on the *next* boot with an error blaming
    the file.
    """
    if not isinstance(value, str):
        return f"is {type(value).__name__}, not a string"
    if not value.strip():
        return "is empty or whitespace"
    if ":" in value:
        # `session_id` is `<host_id>:<tmux_name>`; `server.py` splits on the last colon, so an
        # id containing one makes targeting ambiguous.
        return "contains ':', which would make session ids ambiguous"
    if any(char.isspace() for char in value):
        # `naming.py`'s list parser is TAB-delimited, so whitespace corrupts the record itself.
        return "contains whitespace"
    if value != value.strip():
        return "has leading or trailing whitespace"
    return None


def lakebox_kind(*, pid1_cmdline: str | None = None) -> str:
    """``hosts.kind``: can this host prove it is a Lakebox?

    Deliberately a *positive* test with an honest negative. A host that cannot prove it is a
    Lakebox is ``"unknown"``, which is a true statement about a laptop running the test suite.
    Unlike the old ``unknown:<machine-id>`` host id, a ``kind`` of ``"unknown"`` collides with
    nothing -- it is a label, not a key.
    """
    if any(marker.exists() for marker in _LAKEBOX_MARKERS):
        return KIND_LAKEBOX
    cmdline = pid1_cmdline if pid1_cmdline is not None else _read_pid1_cmdline()
    if cmdline and _LAKEBOX_PID1 in cmdline:
        return KIND_LAKEBOX
    return KIND_UNKNOWN


def _read_pid1_cmdline() -> str | None:
    """PID 1's argv. Readable unprivileged (unlike ``/proc/1/environ``), and on a Lakebox it
    is ``sandbox-daemon --enable-sshd --uid 10086``."""
    try:
        return Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------------------
# host_id
# --------------------------------------------------------------------------------------
def resolve_host_id(
    state_dir: str,
    *,
    explicit: str | None = None,
    sandbox_id: str | None = None,
    gateway_host: str | None = None,
    recovered: str | None = None,
) -> HostIdentity:
    """Resolve `host_id` by ADR-6's ladder. Never returns a derived or colliding id.

    ``explicit`` is ``$SHELLBOX_HOST_ID`` (step 1). ``recovered`` is a `host_id` read back from
    a live tmux server's ``@shellbox_host_id`` user option -- see `enroll.py` -- so a host whose
    cache was lost while sessions are still running re-adopts its real identity instead of
    re-keying them.

    Guarantees, under any interleaving of concurrent callers: the returned `host_id` is valid,
    and (except for ``explicit``, which is not persisted) it equals the one in the cache file.
    """
    # Step 1. An explicit id wins and is NOT written to the cache: it is a test and
    # non-Lakebox-host override, and persisting it would make a one-off run permanent.
    if explicit:
        problem = _host_id_problem(explicit)
        if problem:
            # Operator-set, so refusing is right: silently ignoring it would run under an
            # identity they did not choose, and silently accepting it corrupts session ids.
            raise IdentityError(f"SHELLBOX_HOST_ID={explicit!r} {problem}")
        return HostIdentity(
            host_id=explicit,
            kind=lakebox_kind(),
            assigned=False,
            source="env",
            sandbox_id=sandbox_id,
            gateway_host=gateway_host,
        )

    # A bad tmux stamp is NOT fatal -- it is data from a shared server that any agent can set,
    # so it is dropped with an ERROR and resolution continues. Refusing would let one bad
    # `set-option` deny every agent its shells.
    if recovered is not None:
        problem = _host_id_problem(recovered)
        if problem:
            logger.error(
                "ignoring the @shellbox_host_id stamp %r on the tmux server: it %s. "
                "Resolving identity from the cache instead.",
                recovered,
                problem,
            )
            recovered = None

    path = Path(state_dir) / HOST_JSON_NAME
    properties = {"sandbox_id": sandbox_id, "gateway_host": gateway_host}

    for _ in range(_RESOLVE_ATTEMPTS):
        state, cached, _ = _load_cache(path)

        # Step 2. The normal path from boot 2 onward.
        if cached is not None:
            if recovered and recovered != cached["host_id"]:
                # The cache describes a different host than the running tmux server does --
                # the fork RC-4 warns about. tmux wins: it is the session authority (ADR-5)
                # and its sessions are what a re-key would strand. Mutating an existing file,
                # so it goes through the lock and re-reads under it.
                if not _override_identity(path, cached["host_id"], recovered, properties):
                    time.sleep(_LOCK_BACKOFF)
                    continue
                return _identity(recovered, "tmux", assigned=False, **properties)

            _record_properties(path, cached, properties)
            return HostIdentity(
                host_id=cached["host_id"],
                kind=lakebox_kind(),
                assigned=False,
                source="cache",
                # A cached value is kept when the caller has none to offer, so a host stays
                # labelled across boots where the bootstrap has not re-run. A fresh value
                # always wins -- it came from someone who actually knows (ADR-8).
                sandbox_id=sandbox_id or cached.get("sandbox_id"),
                gateway_host=gateway_host or cached.get("gateway_host"),
            )

        # Step 3. No usable identity. When there is no file at all and no stamp to honour,
        # `os.link` is sufficient arbitration on its own -- no lock, so the common cold-boot
        # path stays lock-free for all 32 processes.
        if state is _CacheState.ABSENT and not recovered:
            host_id, assigned = _create_or_adopt(path, str(uuid.uuid4()), properties)
            logger.info(
                "%s host_id %r%s",
                "assigned" if assigned else "adopted concurrently-assigned",
                host_id,
                f" and cached it at {path}" if assigned else f" from {path}",
            )
            return _identity(host_id, "assigned", assigned=assigned, **properties)

        # EMPTY, CORRUPT, or ABSENT-with-a-stamp: all mutate or replace what is there, so all
        # take the lock and re-decide under it.
        resolved = _replace_unusable(path, recovered, properties)
        if resolved is None:
            time.sleep(_LOCK_BACKOFF)
            continue
        return resolved

    raise IdentityError(
        f"could not settle a host identity at {path} after {_RESOLVE_ATTEMPTS} attempts: it is "
        "being changed continuously by other processes, or a lock is being held and released "
        "repeatedly. Inspect the state directory."
    )


def _identity(
    host_id: str, source: str, *, assigned: bool, **properties: str | None
) -> HostIdentity:
    return HostIdentity(
        host_id=host_id,
        kind=lakebox_kind(),
        assigned=assigned,
        source=source,
        sandbox_id=properties.get("sandbox_id"),
        gateway_host=properties.get("gateway_host"),
    )


def _override_identity(
    path: Path, expected: str, recovered: str, properties: dict[str, str | None]
) -> bool:
    """Replace the cached `host_id` with one recovered from tmux. False ⇒ retry the resolution.

    Under the lock throughout, and re-reads under it: if the cache no longer says ``expected``,
    someone else already changed it and this process's decision was made on stale information.
    """
    with _exclusive(path) as acquired:
        if not acquired:
            return False
        _, cached, raw = _load_cache(path)
        if cached is None or cached["host_id"] != expected:
            return False
        logger.warning(
            "host identity mismatch: cache %s says %r but the live tmux server is stamped %r; "
            "adopting the tmux value, because its sessions are what a re-key would strand.",
            path,
            expected,
            recovered,
        )
        payload = dict(raw or {})
        # `host_id` only. NOT `version`: forcing it here would let an older build stamp a
        # version-2 file back to 1 while leaving the version-2 *content* in place, so the
        # field would assert a schema the file does not have -- worse than having no field,
        # since the point of the scaffolding is that a later reader can trust it. `raw`
        # already carries whatever wrote the file; `_payload` sets it when creating one.
        payload["host_id"] = recovered
        payload.setdefault("version", 1)
        for key, value in properties.items():
            if value:
                payload[key] = value
        _atomic_write(path, payload)
        return True


def _replace_unusable(
    path: Path, recovered: str | None, properties: dict[str, str | None]
) -> HostIdentity | None:
    """Handle EMPTY / CORRUPT / ABSENT-with-a-stamp. ``None`` ⇒ retry the resolution.

    CRITICAL: **This is where six identities came from.** Quarantine-then-assign is two steps, and
    with no arbitration two processes each quarantined what the other had just assigned: 32
    processes produced 6 distinct ids, 5 quarantine files of which 4 held *live valid identities*,
    and the ERROR log named a file it had not moved. The fix is not a better quarantine — it is that
    the whole transaction is serialized and re-decided from a read taken **under** the lock.
    """
    with _exclusive(path) as acquired:
        if not acquired:
            return None

        state, cached, _ = _load_cache(path)
        if cached is not None:
            # Someone else fixed it while we waited. Retry so the normal cache path runs and we
            # return *their* id -- never a second one.
            return None

        if state is _CacheState.CORRUPT:
            quarantined = _quarantine(path)
            logger.error(
                "identity cache %s held unusable content and has been moved aside to %s; "
                "assigning a NEW host_id. Every session_id on this host is now re-keyed, so a "
                "session id already handed to an agent will report invalid_name while its tmux "
                "session keeps running. Inspect the quarantined file.",
                path,
                quarantined,
            )
        elif state is _CacheState.EMPTY:
            # An empty file has two possible authors, and they want opposite treatment:
            #
            #   * a writer that is mid-write RIGHT NOW -- only possible from a build that used
            #     `O_EXCL` on the final path, i.e. during a rolling upgrade. Replacing it steals
            #     the name from a live writer, which then writes its own id: two identities.
            #   * a writer that died, or a stray `touch`. Nothing will ever finish it.
            #
            # They are indistinguishable by inspection but trivially separated by *waiting*: the
            # first resolves within microseconds. So wait out the same window `_adopt_winner`
            # uses, then treat it as abandoned. That keeps the mid-write adoption AND stops an
            # empty file being a permanent brick -- an earlier version refused to start on one,
            # every process, every boot, until a human deleted it, which protected nothing
            # (there is no `host_id` in an empty file to strand) and cost every shell.
            for _ in range(_ADOPT_ATTEMPTS):
                time.sleep(_ADOPT_BACKOFF)
                if _load_cache(path)[1] is not None:
                    return None  # it landed; retry so the cache path returns THEIR id
            logger.error(
                "identity cache %s has been empty for %.1fs (no host_id to preserve); replacing "
                "it. A crashed writer or a stray `touch` is the usual cause.",
                path,
                _ADOPT_ATTEMPTS * _ADOPT_BACKOFF,
            )
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("could not remove the empty cache %s: %s", path, exc)
                return None

        candidate = recovered or str(uuid.uuid4())
        if recovered:
            logger.warning(
                "identity cache %s held no usable id, but the live tmux server is stamped %r; "
                "re-adopting it rather than assigning a new one, which would re-key every "
                "session_id on this host.",
                path,
                recovered,
            )
        # Still through `_create_or_adopt`: holding the lock stops another *lock-taking* process
        # from racing, but a process on the lock-free cold-boot path above can legitimately link
        # a file between our unlink and our write. `link` makes that safe -- we adopt theirs.
        host_id, assigned = _create_or_adopt(path, candidate, properties)
        source = "tmux" if (recovered and host_id == recovered) else "assigned"
        return _identity(host_id, source, assigned=assigned, **properties)


# --------------------------------------------------------------------------------------
# The arbitrated create
# --------------------------------------------------------------------------------------
def _create_or_adopt(
    path: Path, candidate: str, properties: dict[str, str | None]
) -> tuple[str, bool]:
    """Claim the identity, or adopt the winner's. Returns ``(host_id, assigned)``.

    **Write the content first, then claim the name — never the other way round.** The obvious
    implementation opens the final path with ``O_CREAT|O_EXCL`` and writes into it, which does
    elect exactly one winner but publishes the file *before* it has content. A loser arriving in
    that window sees a file that exists and is either empty or **truncated mid-JSON**, and
    truncated JSON is indistinguishable from corruption by inspection. An earlier version
    therefore quarantined the winner's file out from under it and assigned a second id.

    ``os.link`` fixes that structurally rather than by heuristic: it is atomic and fails with
    ``EEXIST`` if the name is taken, so it arbitrates the race exactly as ``O_EXCL`` did — but
    the directory entry appears only once the content behind it is complete. **A loser can never
    observe a partial identity file, so no code needs to guess whether one is corrupt.**

    (Found by the concurrency test, not by review: it is unreachable without a barrier, because
    serialized process startup lets the winner finish long before a loser looks.)
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {"version": 1, "host_id": candidate}
    # Absent rather than null when unknown, so a reader cannot mistake "never bootstrapped" for
    # "the bootstrap told us it is null".
    for key, value in properties.items():
        if value:
            payload[key] = value
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    # Unique per attempt, not merely per pid: two processes can share a pid across a container
    # boundary, and a stale staging file from a crashed run must never be written into by a live
    # one.
    staging = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    # 0600 at open() rather than a later chmod, so the file is never briefly world-readable --
    # it names the host whose owner_email is a workspace admin.
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            # Written once per sandbox lifetime, so the cost is irrelevant and losing it to a
            # crash is not.
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            # Losing is the COMMON case, not an error: with 32 processes starting together, 31
            # arrive here. Thanks to link-after-write, whatever is at `path` is complete.
            return _adopt_winner(path), False
        except OSError as exc:
            # Hardlinks are unavailable on a few exotic filesystems. Refusing beats silently
            # falling back to a racy write: on the one filesystem where that mattered, hosts
            # would split invisibly.
            raise IdentityError(
                f"could not atomically claim {path} ({exc}). Identity assignment needs hardlink "
                "support in the state directory; set SHELLBOX_STATE_DIR to a normal filesystem, "
                "or SHELLBOX_HOST_ID to assign identity explicitly."
            ) from exc
        # The fsync above protects the file's *contents*; only fsyncing the directory protects
        # the name that now points at them.
        _fsync_dir(path.parent)
        return candidate, True
    finally:
        # Drops only the second link. `path` keeps the inode.
        try:
            os.unlink(staging)
        except OSError:
            pass


def _adopt_winner(path: Path) -> str:
    """Read the `host_id` of whoever won the race.

    Normally one read: `os.link` guarantees the content is complete before the name exists. The
    retry covers one narrow case -- a **rolling upgrade** from a build that used ``O_EXCL`` on
    the final path, where a genuinely empty file can be observed between open and write. Nothing
    in the current module can produce that state.
    """
    for _ in range(_ADOPT_ATTEMPTS):
        state, adopted, _ = _load_cache(path)
        if adopted is not None:
            return adopted["host_id"]
        if state is not _CacheState.EMPTY:
            # ABSENT (the winner's file vanished) or CORRUPT: waiting cannot help.
            break
        time.sleep(_ADOPT_BACKOFF)
    # Assigning a second id here would split the host, so refusing is the least-bad option.
    raise IdentityError(
        f"identity cache {path} exists but did not become readable as an identity within "
        f"{_ADOPT_ATTEMPTS * _ADOPT_BACKOFF:.1f}s. The process that created it may have died "
        "mid-write, it may be corrupt, or it may be unreadable to this user -- earlier log "
        "lines say which. Inspect it by hand; deleting it re-keys every session_id on this host."
    ) from None


# --------------------------------------------------------------------------------------
# Arbitration for multi-step mutations
# --------------------------------------------------------------------------------------
@contextmanager
def _exclusive(path: Path, *, attempts: int = 1) -> Iterator[bool]:
    """Serialize multi-step mutations of ``path``. Yields True to exactly one process at a time.

    A lock file rather than `flock`, for the same reason `_create_or_adopt` uses `link`: it is
    one syscall whose semantics do not vary across the filesystems a `$HOME` can be on.

    ``attempts`` is the caller's answer to "what if I do not get it?", and the two answers are
    genuinely different:

    * ``attempts=1`` (default) — **do not wait.** For callers whose enclosing loop re-reads and
      re-decides, where waiting would mean holding a decision made against a file that is
      actively changing. Not getting the lock is *information*: someone else is fixing this.
    * ``attempts>1`` — **wait, briefly.** For callers whose work is unconditional and cannot be
      deferred to anyone else.

    CRITICAL: The second mode exists because the first was applied to a caller that needed it.
    Property writes gave up on contention, and the justification — "the next start records them" —
    is false when every contender is in the **same boot**: with 16 processes released together,
    ``sandbox_id`` was absent from the file in 3 of 12 rounds and ``owner_email`` in 4 of 12,
    which is exactly the "`doctor` reports a bootstrapped host as never bootstrapped" symptom
    `_record_properties` exists to prevent. The critical section is two filesystem calls, so
    waiting out a handful of them is free.
    """
    lock = path.with_name(path.name + _LOCK_SUFFIX)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd: int | None = None

    for attempt in range(attempts):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            # A lock older than the critical section belonged to a process that died holding it.
            # Breaking it is safe because every write behind it still goes through
            # `_create_or_adopt` or a re-read under the lock: two processes both believing they
            # hold this still cannot both assign an identity.
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_SECONDS:
                logger.warning(
                    "breaking a stale identity lock %s (%.1fs old); the process holding it "
                    "appears to have died",
                    lock,
                    age,
                )
                try:
                    lock.unlink()
                except OSError:
                    pass
            if attempt + 1 < attempts:
                # Jittered, because a fixed interval makes 32 processes released together
                # retry in lockstep every 20ms. That cannot corrupt anything -- the lock
                # still serializes -- but it can starve one waiter past its bounded window,
                # and the consequence is the dropped-field symptom `attempts` exists to
                # prevent, recurring under sustained load.
                time.sleep(_LOCK_BACKOFF * (1.0 + random.random()))
        except OSError as exc:
            logger.warning("could not take the identity lock %s: %s", lock, exc)
            break

    if fd is None:
        yield False
        return

    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    try:
        yield True
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _quarantine(path: Path) -> Path:
    """Move a corrupt cache aside, preserving it. Returns its new path.

    WARNING: **Callers must hold `_exclusive`.** The `exists()`-then-`replace` below is only safe
    because one process at a time runs it; unguarded, two quarantines moved each other's
    freshly-assigned identities aside and split one sandbox six ways.

    Never deletes: the file names a host whose sessions may still be running, and its contents
    are the only evidence of what that host's identity used to be.
    """
    for suffix in range(1, 1000):
        candidate = path.with_name(f"{path.name}.corrupt.{suffix}")
        if not candidate.exists():
            try:
                os.replace(path, candidate)
            except OSError as exc:  # pragma: no cover - defensive
                logger.error("could not quarantine %s: %s", path, exc)
                return path
            return candidate
    return path  # pragma: no cover - 999 corrupt caches is someone else's problem


# --------------------------------------------------------------------------------------
# Reading and property writes
# --------------------------------------------------------------------------------------
def _load_cache(path: Path) -> tuple[_CacheState, dict[str, Any] | None, dict[str, Any] | None]:
    """Read and shape-check the cache in ONE observation.

    Returns ``(state, checked, raw)``. ``checked`` holds only the keys this module models and is
    ``None`` unless the state is ``OK``; ``raw`` is the file as parsed, so a merge can preserve
    keys a future build adds without a second read. Handing back both from one `read_text` is
    the point -- a caller that reads twice to answer one question is the bug this signature
    exists to prevent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _CacheState.ABSENT, None, None
    except OSError as exc:
        # Unreadable is NOT corrupt: a permissions or I/O problem must never cause the file to
        # be moved aside, because moving it aside re-keys every session on the host.
        logger.warning("identity cache %s is unreadable (%s); treating as absent", path, exc)
        return _CacheState.ABSENT, None, None

    if not text.strip():
        return _CacheState.EMPTY, None, None

    try:
        parsed = json.loads(text)
    except ValueError:
        logger.error(
            "identity cache %s is not valid JSON; it will be preserved rather than overwritten, "
            "because a new host_id re-keys every session_id on this host.",
            path,
        )
        return _CacheState.CORRUPT, None, None
    if not isinstance(parsed, dict):
        logger.error("identity cache %s holds %s, not an object", path, type(parsed).__name__)
        return _CacheState.CORRUPT, None, None

    problem = _host_id_problem(parsed.get("host_id"))
    if problem:
        logger.error("identity cache %s has an unusable host_id: it %s", path, problem)
        return _CacheState.CORRUPT, None, parsed

    checked: dict[str, Any] = {"host_id": parsed["host_id"]}
    for optional in ("sandbox_id", "gateway_host"):
        value = parsed.get(optional)
        if isinstance(value, str) and value.strip():
            checked[optional] = value
    return _CacheState.OK, checked, parsed


def _record_properties(
    path: Path, cached: dict[str, Any], properties: dict[str, str | None]
) -> None:
    """Persist a `sandbox_id`/`gateway_host` the caller brought that the cache lacks.

    CRITICAL: Without this, ADR-8 does not work past the first boot. The bootstrap path runs **every
    boot** and is the only actor that knows the sandbox id, but from boot 2 onward it takes the
    cache-hit branch -- so the id was handed back to that one caller and never written, leaving
    the other 1-31 processes in the boot with ``None``, `doctor` reporting a bootstrapped host as
    "never bootstrapped", and the `hosts` row losing a column it had. Invisible to any assertion
    on the *returned* value, which was correct throughout.

    WARNING: Not best-effort. An earlier version skipped the write when another process held the
    lock, reasoning that "the next start records them" -- which is false, because **every contender
    is in the same boot**. If all the processes that were told the `sandbox_id` lose the lock to an
    owner-email writer, nobody records it, and "the next start" means a sandbox restart. Measured
    at 16 processes: `sandbox_id` absent from the file in 3 of 12 rounds. So this waits.
    """
    incoming = {
        key: value for key, value in properties.items() if value and value != cached.get(key)
    }
    if not incoming:
        return
    logger.info("recording %s on identity cache %s", ", ".join(sorted(incoming)), path)
    _merge_properties(path, incoming, expected_host_id=cached["host_id"])


def _merge_properties(
    path: Path, updates: dict[str, Any], *, expected_host_id: str | None = None
) -> None:
    """Merge non-identity fields into the cache, preserving everything else.

    **Never writes `host_id`.** That is not a stylistic split, it is what makes a lost update
    harmless: this is a read-modify-write, so a concurrent writer's change can be overwritten by
    a stale snapshot -- and when the field in that snapshot was `host_id`, the effect was to
    silently *revert* an identity, undoing the tmux-wins reconciliation and sending the next boot
    back to the previous id. Properties are idempotent and re-derived every start, so losing one
    costs an API call; losing an identity change costs every session on the host.

    Identity therefore only ever changes through `_create_or_adopt` or `_override_identity`,
    both arbitrated. This function also never *creates* the cache: if a merge could, two
    processes could each merge a `host_id` into a missing file and split the host through the
    back door.
    """
    # An assertion rather than a filter, deliberately. `host_id` is stripped defensively
    # below too, but a silent strip lets a future caller believe it changed an identity
    # through this path -- which is last-writer-wins and would revert a concurrent
    # reconciliation. No test can catch a caller that does not exist yet; this trips over it
    # on the first run.
    assert "host_id" not in updates, (
        f"identity must not change through a property write ({sorted(updates)}); use "
        "_create_or_adopt or _override_identity, which are arbitrated"
    )

    # Waits, rather than skipping -- see this function's docstring for what skipping cost.
    with _exclusive(path, attempts=_ADOPT_ATTEMPTS) as acquired:
        if not acquired:
            # At WARNING, not debug: after a restart with no credential the cache is E2b's ONLY
            # source of `owner_email`, so a dropped write here is not "one extra API call", it is
            # the next boot deferring enrollment entirely.
            logger.warning(
                "could not record %s on %s within %.1fs: another process held the identity lock "
                "throughout. The value will be recorded on a later start.",
                ", ".join(sorted(updates)),
                path,
                _ADOPT_ATTEMPTS * _LOCK_BACKOFF,
            )
            return
        _, cached, raw = _load_cache(path)
        if cached is None or raw is None:
            logger.warning(
                "not updating identity cache %s: it is missing or unusable, and rewriting it "
                "would risk the host_id it carries",
                path,
            )
            return
        if expected_host_id is not None and cached["host_id"] != expected_host_id:
            # The identity changed while we were deciding; our update was computed against a
            # host that is no longer this one.
            logger.info("skipping a property write to %s: the host_id changed underneath", path)
            return
        payload = dict(raw)
        payload.update({key: value for key, value in updates.items() if value})
        payload["host_id"] = cached["host_id"]  # belt and braces: never let `updates` move it
        _atomic_write(path, payload)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """`tmp` + `os.replace`, mode 0600. **Callers must hold `_exclusive`.**

    `os.replace` is last-writer-wins, which is why this is never the arbiter of an identity --
    see `_create_or_adopt`. It is correct for a caller that has already been serialized and has
    re-read the file under the lock.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _fsync_dir(directory: Path) -> None:
    """Persist a directory entry, so a crash cannot lose the name a written file now has."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - not all filesystems allow it
        pass
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------
# owner_email
# --------------------------------------------------------------------------------------
def resolve_owner_email(
    state_dir: str,
    *,
    credential_email: str | None,
    env_email: str | None = None,
) -> OwnerResolution:
    """E2: resolve `owner_email`, reconciling the cache against the credential.

    The credential wins on disagreement (E2a) and the cache is corrected. r1 of the inherited
    plan read the cache first and stopped, which let a stale `owner_email` win **forever, with
    no TTL** -- a sandbox that changed hands would keep attributing its shells to the previous
    owner.

    WARNING: **E2b is weaker than the inherited plan assumed.** Its "no credential, cache present"
    case was documented as *the* normal path after the PAT reset, on the strength of the CLI's
    OAuth token cache serving in-sandbox API calls. That holds **within one boot only**:
    `~/.databricks/token-cache.json` is boot-templated into wiped `/run`
    (`docs/sandbox-environment.md` §3), so after a restart a PAT-reset sandbox has no credential
    at all until the login is re-run. The cache is then the *only* source, which is exactly why
    it is written before the `hosts` row and never expired.
    """
    path = Path(state_dir) / HOST_JSON_NAME
    cached_email = _read_cached_owner(path)

    if credential_email:
        if cached_email and cached_email != credential_email:
            logger.warning(
                "owner_email mismatch: cache says %r, the live credential says %r. The "
                "credential wins (it is the more recent fact -- the sandbox may have changed "
                "hands) and the cache is being corrected.",
                cached_email,
                credential_email,
            )
            _merge_properties(path, {"owner_email": credential_email})
            return OwnerResolution(credential_email, source="credential", reconciled=True)
        if not cached_email:
            _merge_properties(path, {"owner_email": credential_email})
        return OwnerResolution(credential_email, source="credential")

    if cached_email:
        return OwnerResolution(cached_email, source="cache")
    if env_email:
        return OwnerResolution(env_email, source="env")
    # E2d. Enrollment defers; the tool surface keeps working with NullRegistry semantics, because
    # a shell an agent cannot get is a worse outcome than an inventory row nobody reads.
    # `doctor` is where this becomes visible.
    logger.warning(
        "no credential, no cached owner_email, and SHELLBOX_OWNER_EMAIL is unset: enrollment is "
        "DEFERRED and will be retried. Shell tools still work. Run `shellbox-mcp doctor` to see "
        "why no credential was available."
    )
    return OwnerResolution(None, source="deferred")


def cached_owner_email(state_dir: str) -> str | None:
    """The cached `owner_email`, or ``None``. A cheap re-read, for callers that started
    before enrollment finished resolving it.

    `enroll.py` resolves the owner from the ambient credential on a background thread --
    measured at ~1.4s against a live workspace -- and caches it here. A process that started
    in that window must be able to notice, or it spends its whole life believing the host has
    no owner. See `server.py`'s `owner_email()`.
    """
    return _read_cached_owner(Path(state_dir) / HOST_JSON_NAME)


def _read_cached_owner(path: Path) -> str | None:
    _, _, raw = _load_cache(path)
    if raw is None:
        return None
    email = raw.get("owner_email")
    return email if isinstance(email, str) and email.strip() else None
