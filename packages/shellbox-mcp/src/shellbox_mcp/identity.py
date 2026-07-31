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

## Why the exclusive create matters

1-32 MCP processes run concurrently against one tmux server -- an invariant issue #2 calls
"mandatory, not stylistic". Writing the cache with the usual `tmp` + `os.replace` idiom would
be **last-writer-wins**: on a cold first boot every process mints its own uuid4, one write
survives, and the losers each serve a full lifetime under an id absent from the cache. That
is one sandbox split across N `hosts` rows, session rows for a *shared* tmux server filed
under different hosts, and `server.py`'s cross-host check rejecting a sibling's session id as
`invalid_name` while that tmux session is alive and usable.

`O_CREAT | O_EXCL` makes exactly one process the assigner; every other **adopts the winner**.
The file is therefore written once per sandbox lifetime and read forever after, which is also
why an atomic-replace idiom would be solving a problem that does not arise.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "HOST_JSON_NAME",
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
# The real window is a single small `json.dump` -- microseconds -- so this is generous by
# three orders of magnitude and exists only so a *crashed* winner surfaces as an error
# instead of a hang. 0.5s total.
_ADOPT_ATTEMPTS = 25
_ADOPT_BACKOFF = 0.02


class _CacheState(Enum):
    """Why a cache read produced no identity. The distinction is not cosmetic: only ``CORRUPT``
    may be quarantined, and quarantining anything else re-keys every session on the host."""

    OK = "ok"
    ABSENT = "absent"
    """No file, or a file we could not read at all (permissions, I/O). Never quarantined."""
    EMPTY = "empty"
    """Present but blank. Not produced by this module -- see `_create_or_adopt` -- so it means
    a stray `touch` or a writer from an older build that died. Nothing to preserve."""
    CORRUPT = "corrupt"
    """Complete content that is not a usable identity. The only quarantinable state."""


class IdentityError(Exception):
    """The identity cache exists but cannot be used, and overwriting it would be worse.

    Distinct from a *missing* cache, which is the normal first-boot path. A corrupt cache is
    not silently replaced: a new `host_id` re-keys every `session_id` (they are
    ``f"{host_id}:{tmux_name}"``), stranding live sessions as unaddressable while their tmux
    sessions still run. An operator deleting the file is a decision; this module making that
    decision for them is not.
    """


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Resolved host identity. ``sandbox_id``/``gateway_host`` are properties, not identity."""

    host_id: str
    kind: str
    assigned: bool
    """True when this process performed the assignment; False when it adopted an existing id.
    Exposed because "who assigned it" is the one thing the concurrency test can assert on --
    exactly one winner across N processes."""
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


def lakebox_kind(*, pid1_cmdline: str | None = None) -> str:
    """``hosts.kind``: can this host prove it is a Lakebox?

    Deliberately a *positive* test with an honest negative. A host that cannot prove it is a
    Lakebox is ``"unknown"``, which is a true statement about a laptop running the test suite
    and does not pretend otherwise. Unlike the old ``unknown:<machine-id>`` host id, a
    ``kind`` of ``"unknown"`` collides with nothing -- it is a label, not a key.
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


def resolve_host_id(
    state_dir: str,
    *,
    explicit: str | None = None,
    sandbox_id: str | None = None,
    gateway_host: str | None = None,
    recovered: str | None = None,
) -> HostIdentity:
    """Resolve `host_id` by ADR-6's ladder. Never returns a derived or colliding id.

    ``explicit`` is ``$SHELLBOX_HOST_ID`` (step 1). ``recovered`` is a `host_id` read back
    from a live tmux server's ``@shellbox_host_id`` user option -- see `enroll.py`, which
    supplies it so that a host whose cache was deleted while sessions are still running
    re-adopts its real identity instead of re-keying them.
    """
    # Step 1. An explicit id wins and is NOT written to the cache: it is a test and
    # non-Lakebox-host override, and persisting it would make a one-off run permanent.
    if explicit:
        return HostIdentity(
            host_id=explicit,
            kind=lakebox_kind(),
            assigned=False,
            source="env",
            sandbox_id=sandbox_id,
            gateway_host=gateway_host,
        )

    path = Path(state_dir) / HOST_JSON_NAME
    state, cached = _load_cache(path)

    # Step 2. The normal path from boot 2 onward.
    if cached is not None:
        host_id = cached["host_id"]
        # A recovered id that disagrees with the cache means the cache is describing a
        # different host than the running tmux server is -- which is the fork RC-4 warns
        # about, caught rather than propagated. tmux wins: it is the session authority
        # (ADR-5), and its sessions are the things that would be stranded.
        if recovered and recovered != host_id:
            logger.warning(
                "host identity mismatch: cache %s says %r but the live tmux server is "
                "stamped %r; adopting the tmux value, because its sessions are what a "
                "re-key would strand. Rewriting the cache.",
                path,
                host_id,
                recovered,
            )
            host_id = recovered
            _write_cache(path, host_id, overwrite=True)
        return HostIdentity(
            host_id=host_id,
            kind=lakebox_kind(),
            assigned=False,
            source="cache",
            # A cached sandbox_id is kept when the caller has none to offer, so a host stays
            # labelled across boots where the bootstrap has not re-run yet. A fresh value
            # always wins -- it came from someone who actually knows (ADR-8).
            sandbox_id=sandbox_id or cached.get("sandbox_id"),
            gateway_host=gateway_host or cached.get("gateway_host"),
        )

    # Genuine corruption: content that is present, complete, and not an identity.
    #
    # Neither obvious response is acceptable on its own -- silently assigning a new id re-keys
    # every `session_id` on the host, and refusing to start denies an agent its shells over an
    # inventory problem. So the file is QUARANTINED (preserved under a new name, so nothing is
    # destroyed and an operator can still see what it held), a fresh id is assigned, and the
    # consequence is logged at ERROR. Nothing here is silent.
    #
    # ⚠️ This decision rides on the SINGLE read above and must never re-stat the file. An
    # earlier version asked two questions -- "did the read fail?" then "does the file have
    # content?" -- and a concurrent winner's `link` landing between them made a perfectly good
    # identity file look corrupt, so a loser quarantined it and minted a second id. The bug was
    # in the gap between the two observations, not in either one.
    if state is _CacheState.CORRUPT:
        quarantined = _quarantine(path)
        logger.error(
            "identity cache %s held unusable content and has been moved aside to %s; "
            "assigning a NEW host_id. Every session_id on this host is now re-keyed, so any "
            "session id already handed to an agent will report invalid_name while its tmux "
            "session keeps running. Inspect the quarantined file.",
            path,
            quarantined,
        )

    # No cache. Prefer an id recovered from the live tmux server over minting a new one:
    # this is the "cache deleted while sessions are live" case, the only one where a new id
    # does real damage.
    if recovered:
        logger.warning(
            "identity cache %s is missing, but the live tmux server is stamped %r; "
            "re-adopting it rather than assigning a new id, which would re-key every "
            "session_id on this host.",
            path,
            recovered,
        )
        _write_cache(path, recovered, sandbox_id=sandbox_id, gateway_host=gateway_host)
        return HostIdentity(
            host_id=recovered,
            kind=lakebox_kind(),
            assigned=False,
            source="tmux",
            sandbox_id=sandbox_id,
            gateway_host=gateway_host,
        )

    # Step 3. Assign. The race is resolved by the filesystem, not by locking: whoever wins
    # `O_EXCL` is the assigner and everyone else adopts what the winner wrote.
    candidate = str(uuid.uuid4())
    host_id, assigned = _create_or_adopt(
        path, candidate, sandbox_id=sandbox_id, gateway_host=gateway_host
    )
    if assigned:
        logger.info("assigned host_id %r and cached it at %s", host_id, path)
    else:
        logger.info("adopted concurrently-assigned host_id %r from %s", host_id, path)
    return HostIdentity(
        host_id=host_id,
        kind=lakebox_kind(),
        assigned=assigned,
        source="assigned",
        sandbox_id=sandbox_id,
        gateway_host=gateway_host,
    )


def _create_or_adopt(
    path: Path,
    candidate: str,
    *,
    sandbox_id: str | None,
    gateway_host: str | None,
) -> tuple[str, bool]:
    """Claim the identity, or adopt the winner's. Returns ``(host_id, assigned)``.

    **Write the content first, then claim the name — never the other way round.** The obvious
    implementation opens the final path with ``O_CREAT|O_EXCL`` and writes into it, which does
    elect exactly one winner but publishes the file *before* it has content. A loser arriving
    in that window sees a file that exists and is either empty or **truncated mid-JSON**, and
    truncated JSON is indistinguishable from corruption by inspection. The first version of
    this function therefore quarantined the winner's file out from under it and assigned a
    second id — one sandbox, two hosts, which is the precise failure the exclusive create is
    here to prevent.

    ``os.link`` fixes it structurally rather than by heuristic: it is atomic and it fails with
    ``EEXIST`` if the name is taken, so it arbitrates the race exactly as ``O_EXCL`` did — but
    the directory entry appears only once the content behind it is complete. **A loser can
    never observe a partial identity file, so no code needs to guess whether one is corrupt.**

    (Discovered by the concurrency test, not by review. It is unreachable without a barrier:
    serialized process startup lets the winner finish long before a loser looks.)
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = _payload(candidate, sandbox_id=sandbox_id, gateway_host=gateway_host)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    # Unique per attempt, not just per pid: two processes can share a pid across a container
    # boundary, and a stale tmp from a crashed run must never be written into by a live one.
    staging = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    # 0600 at open() rather than a later chmod, so the file is never briefly world-readable --
    # it names the host whose owner_email is a workspace admin.
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            # This file is the sandbox's only durable identity and is written once per sandbox
            # lifetime, so the cost of an fsync is irrelevant and losing it to a crash is not.
            os.fsync(handle.fileno())
        try:
            os.link(staging, path)
        except FileExistsError:
            # Losing is the COMMON case, not an error: with 32 processes starting together, 31
            # arrive here. Thanks to link-after-write, whatever is at `path` is complete.
            return _adopt_winner(path), False
        except OSError as exc:
            # Hardlinks are unavailable on a few exotic filesystems. Refusing beats silently
            # falling back to a racy write: on the one filesystem where that mattered, it would
            # split hosts invisibly.
            raise IdentityError(
                f"could not atomically claim {path} ({exc}). Identity assignment needs "
                "hardlink support in the state directory; set SHELLBOX_STATE_DIR to a normal "
                "filesystem, or SHELLBOX_HOST_ID to assign identity explicitly."
            ) from exc
        return candidate, True
    finally:
        try:
            os.unlink(staging)
        except OSError:
            pass


def _adopt_winner(path: Path) -> str:
    """Read the `host_id` of whoever won the race.

    Normally one read: `os.link` guarantees the content is complete before the name exists. The
    retry exists for an identity file left **empty** by something else — a crashed writer from
    an older build, or an operator's stray `touch` — where waiting briefly is free and assuming
    corruption would be wrong.
    """
    for _ in range(_ADOPT_ATTEMPTS):
        state, adopted = _load_cache(path)
        if adopted is not None:
            return adopted["host_id"]
        if state is _CacheState.CORRUPT:
            break  # complete content that is not an identity: waiting cannot help
        time.sleep(_ADOPT_BACKOFF)
    # Assigning a second id here would split the host, so this is one of the few places where
    # refusing to start is the least-bad option.
    raise IdentityError(
        f"identity cache {path} exists but did not become readable as an identity within "
        f"{_ADOPT_ATTEMPTS * _ADOPT_BACKOFF:.1f}s. Either the process that created it died "
        "mid-write, or it is corrupt. Inspect it by hand -- deleting it re-keys every "
        "session_id on this host."
    ) from None


def _write_cache(
    path: Path,
    host_id: str,
    *,
    sandbox_id: str | None = None,
    gateway_host: str | None = None,
    overwrite: bool = False,
) -> None:
    """Write the cache outside the assignment race.

    Used for the recovery and reconciliation paths, where the `host_id` is already decided by
    something more authoritative than this process. `tmp` + `os.replace` is correct *here* --
    the value is not being chosen, so a last-writer-wins race between two processes writing
    the *same* id is harmless.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = _payload(host_id, sandbox_id=sandbox_id, gateway_host=gateway_host)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _payload(host_id: str, *, sandbox_id: str | None, gateway_host: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"version": 1, "host_id": host_id}
    # Absent rather than null when unknown, so a reader cannot mistake "we have never been
    # bootstrapped" for "the bootstrap told us it is null".
    if sandbox_id:
        payload["sandbox_id"] = sandbox_id
    if gateway_host:
        payload["gateway_host"] = gateway_host
    return payload


def _quarantine(path: Path) -> Path:
    """Move a corrupt cache aside, preserving it. Returns the new path.

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


def _load_cache(path: Path) -> tuple[_CacheState, dict[str, Any] | None]:
    """Read and **shape-check** the cache in ONE observation.

    Returns a state as well as the data, because callers must distinguish *why* they got
    nothing, and the difference decides whether a file gets quarantined. Splitting that
    judgement across two filesystem calls is what made a live winner's file look corrupt to a
    concurrent loser -- see `resolve_host_id`.

    Shape-checked rather than trusted, following the idiom #11's review established for the
    tmux incarnation: a file that parses as JSON but holds the wrong shape is a likelier
    failure than a missing one, and a `host_id` of ``None`` or ``[]`` would propagate into
    every `session_id` on the host before anything noticed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _CacheState.ABSENT, None
    except OSError as exc:
        # Unreadable is NOT corrupt: a permissions or I/O problem must never cause the file to
        # be moved aside, because moving it aside re-keys every session on the host.
        logger.warning("identity cache %s is unreadable (%s); treating as absent", path, exc)
        return _CacheState.ABSENT, None

    if not raw.strip():
        # Empty. With link-after-write we never publish an empty identity file, so this is a
        # stray `touch` or a crashed writer from an older build. Not corrupt -- there is nothing
        # in it to preserve or to have been damaged -- and not usable either.
        return _CacheState.EMPTY, None

    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.error(
            "identity cache %s is not valid JSON; it will be preserved rather than "
            "overwritten, because a new host_id re-keys every session_id on this host.",
            path,
        )
        return _CacheState.CORRUPT, None
    if not isinstance(parsed, dict):
        logger.error("identity cache %s holds %s, not an object", path, type(parsed).__name__)
        return _CacheState.CORRUPT, None
    host_id = parsed.get("host_id")
    if not isinstance(host_id, str) or not host_id.strip():
        logger.error("identity cache %s has no usable host_id (%r)", path, host_id)
        return _CacheState.CORRUPT, None
    # A colon would split `f"{host_id}:{tmux_name}"` ambiguously. `server.py` uses
    # `rpartition` so the LAST colon separates, which is safe for a uuid4 but not for an
    # arbitrary hand-edited value -- so reject it here rather than let it corrupt targeting.
    if ":" in host_id:
        logger.error(
            "identity cache %s has a host_id containing ':' (%r); session ids are "
            "'<host_id>:<tmux_name>', so this would make them ambiguous",
            path,
            host_id,
        )
        return _CacheState.CORRUPT, None

    result: dict[str, Any] = {"host_id": host_id}
    for optional in ("sandbox_id", "gateway_host"):
        value = parsed.get(optional)
        if isinstance(value, str) and value.strip():
            result[optional] = value
    return _CacheState.OK, result


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

    ⚠️ **E2b is weaker than the inherited plan assumed.** Its "no credential, cache present"
    case was documented as *the* normal path after the PAT reset, on the strength of the CLI's
    OAuth token cache serving in-sandbox API calls. That holds **within one boot only**:
    `~/.databricks/token-cache.json` is boot-templated into wiped `/run`
    (`docs/sandbox-environment.md` §3), so after a restart a PAT-reset sandbox has no
    credential at all until the login is re-run. The cache is then the *only* source, which is
    exactly why it is written before the `hosts` row and never expired.
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
            _write_cached_owner(path, credential_email)
            return OwnerResolution(credential_email, source="credential", reconciled=True)
        if not cached_email:
            _write_cached_owner(path, credential_email)
        return OwnerResolution(credential_email, source="credential")

    if cached_email:
        return OwnerResolution(cached_email, source="cache")
    if env_email:
        return OwnerResolution(env_email, source="env")
    # E2d. Enrollment defers; the tool surface keeps working with NullRegistry semantics,
    # because a shell an agent cannot get is a worse outcome than an inventory row nobody
    # reads. `doctor` is where this becomes visible.
    logger.warning(
        "no credential, no cached owner_email, and SHELLBOX_OWNER_EMAIL is unset: "
        "enrollment is DEFERRED and will be retried. Shell tools still work. "
        "Run `shellbox-mcp doctor` to see why no credential was available."
    )
    return OwnerResolution(None, source="deferred")


def _read_cached_owner(path: Path) -> str | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    email = parsed.get("owner_email")
    return email if isinstance(email, str) and email.strip() else None


def _write_cached_owner(path: Path, owner_email: str) -> None:
    """Merge `owner_email` into the cache, preserving `host_id`.

    Read-modify-write rather than a rewrite: `host_id` is the irreplaceable field in this file
    and must survive an owner correction. If the file is unreadable the merge is skipped
    entirely -- losing a cached email costs one API call next start, while clobbering
    `host_id` re-keys every session on the host.
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or not isinstance(existing.get("host_id"), str):
            raise ValueError("no host_id")
    except (OSError, ValueError):
        logger.warning(
            "not caching owner_email: %s is missing or unusable, and rewriting it would "
            "risk the host_id it carries",
            path,
        )
        return
    existing["owner_email"] = owner_email
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
