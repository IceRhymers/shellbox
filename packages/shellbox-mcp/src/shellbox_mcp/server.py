"""The stdio MCP server: six tools over ``TmuxAdapter`` (plan §6).

Three properties this module is built to preserve. Each is asserted by
``tests/integration/``, because each is the kind of property that looks fine in review and
fails only in a client:

* **Nothing writes to stdout except the MCP protocol.** stdout *is* the transport. A stray
  ``print`` surfaces client-side as an unintelligible parse error with no hint of where it
  came from, so ruff's ``T20`` bans ``print`` over this package and
  ``tests/integration/test_stdout_protocol.py`` parses every line a full session writes.
  Logging goes to stderr, configured in ``cli.py`` before any other import.

* **Zero in-process session state.** No session cache, no dict of handles, no adapter held
  on an instance. 1-32 MCP processes run against one tmux server, so anything remembered
  here is wrong within a single turn -- a cached "session exists" survives another
  process's kill. ``TmuxAdapter`` is constructed **per call**: it is a frozen config plus a
  runner, so this costs nothing and removes the only place a cache could live.

* **The error taxonomy is closed** (§6). Every failure leaves this module as an MCP tool
  error whose text is ``{"error": {"code", "message", "session"}}`` with ``code`` from §6's
  table, so an agent can branch on ``not_found`` vs ``tmux_unavailable`` rather than parsing
  prose. ``no_server`` is internal and never appears: ``public_code`` maps it to
  ``tmux_unavailable``, and ``shell_list`` alone treats *only* the two measured no-server
  signatures as an empty inventory (that mapping lives in ``TmuxAdapter.list_sessions``).

Registry writes are a **projection, not a write path**: tmux is the authority (§9). A
registry failure yields a ``registry_warning`` on an otherwise successful call, because a
Lakebase outage must never stop an agent getting a shell.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from shellbox_registry import NullRegistry, Registry, create_registry
from shellbox_registry import SessionRecord as RegistrySessionRecord

from shellbox_mcp import identity, naming
from shellbox_mcp.config import Settings
from shellbox_mcp.errors import (
    InvalidName,
    ShellboxError,
    TmuxError,
    TmuxUnavailable,
    public_code,
)
from shellbox_mcp.tmux import SessionRecord, TmuxAdapter

logger = logging.getLogger(__name__)

__all__ = ["build_server", "error_payload", "serve"]

SERVER_NAME = "shellbox"

# A sentinel rather than `None`, because `SessionRecord.owner_email` is typed `str` and the
# column is NOT NULL. Making the unresolved case a distinct object means `project` can refuse
# it by identity, and no code path can smuggle it into the database by treating it as a name.
_OWNER_UNRESOLVED = "\x00unresolved"

INSTRUCTIONS = """\
tmux-backed shell sessions that outlive this MCP server process.

Sessions are addressed by name (`shell_create(name=...)`) and by the `session` id returned
by every tool. Output is returned with ANSI escapes preserved. `shell_send` submits input;
it cannot confirm the pane's process consumed it, which is why every send reports
`delivery: "unverified"` -- read the pane back to observe an effect.
"""


# --------------------------------------------------------------------------------------
# Tool payloads. TypedDicts rather than bare dicts so the MCP SDK derives an output schema
# from them: schema and implementation cannot then drift, which is the same reason §6
# chose a typed tool surface in the first place.
# --------------------------------------------------------------------------------------
class CreateResult(TypedDict):
    session: str
    tmux_name: str
    cwd: str
    cols: int
    rows: int
    created: bool
    incarnation: str
    host_id: str
    registry_warning: str | None


class SendResult(TypedDict):
    session: str
    tmux_name: str
    submitted_bytes: int
    keys_sent: list[str]
    # §9.1: the incarnation this send TARGETED. It makes misdelivery detectable after the
    # fact (T-CONC-3); it is not a delivery receipt -- see `delivery`.
    incarnation: str
    delivery: str


class ReadResult(TypedDict):
    session: str
    tmux_name: str
    content: str
    lines: int
    cols: int
    rows: int
    alive: bool
    scrollback_lines: int
    history_limit: int


class SessionEntry(TypedDict):
    session: str
    tmux_name: str
    cwd: str | None
    cols: int
    rows: int
    created_at: int
    last_activity_at: int
    alive: bool
    # `null` exactly when `foreign` is true. An empty `@shellbox_incarnation` is NEVER an
    # incarnation match (§9.1): it means a create is in flight, or the session was made by
    # something that is not shellbox. Both are "cannot confirm identity".
    incarnation: str | None
    foreign: bool


class ListResult(TypedDict):
    host_id: str
    sessions: list[SessionEntry]


class ResizeResult(TypedDict):
    session: str
    tmux_name: str
    cols: int
    rows: int


class KillResult(TypedDict):
    session: str
    tmux_name: str
    killed: bool
    registry_warning: str | None


def error_payload(exc: ShellboxError) -> dict[str, Any]:
    """§6's structured error body. The single shape every tool failure takes."""
    return {
        "error": {
            # `public_code` is what keeps `no_server` out of the public surface: it is an
            # internal N1 classification, and an agent branching on it would be branching
            # on a code §6 does not define.
            "code": public_code(exc.code),
            "message": exc.message,
            "session": exc.session,
        }
    }


def _tool_errors[R](fn: Callable[..., R]) -> Callable[..., R]:
    """Normalize every exception a tool body can raise into a ``ShellboxError``.

    Normalize, not render: the payload is built at the protocol boundary
    (``_install_error_boundary``). Two arms matter beyond the pass-through:

    * ``OSError`` is what ``subprocess`` raises when ``SHELLBOX_TMUX_BIN`` does not exist
      or is not executable. That is precisely "no usable tmux", i.e. ``tmux_unavailable``;
      leaving it uncaught would surface a Python traceback as the agent's answer.
    * Anything else becomes ``tmux_error`` -- the taxonomy's documented catch-all -- rather
      than escaping as an unclassified error. ``logger.exception`` keeps the traceback on
      stderr, where it is a diagnostic instead of part of the protocol stream.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> R:
        try:
            return fn(*args, **kwargs)
        except ShellboxError as exc:
            logger.info("%s -> %s: %s", fn.__name__, exc.code, exc.message)
            raise
        except OSError as exc:
            logger.warning("%s: tmux is unreachable: %s", fn.__name__, exc)
            raise TmuxUnavailable(f"tmux could not be invoked: {exc}") from exc
        except Exception as exc:
            logger.exception("%s failed unexpectedly", fn.__name__)
            raise TmuxError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper


def _shellbox_cause(exc: BaseException) -> ShellboxError | None:
    """Find the ``ShellboxError`` behind an exception, following ``__cause__``.

    Needed because ``FastMCP`` re-raises a failing tool as
    ``ToolError(f"Error executing tool {name}: {e}")`` -- the original is preserved only as
    the cause. Reading the code off the cause keeps the payload exact instead of leaving
    clients to locate a JSON document inside a prose sentence.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ShellboxError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _install_error_boundary(mcp: FastMCP[Any]) -> None:
    """Render tool failures as §6's envelope, exactly.

    ⚠️ This replaces the ``CallToolRequest`` handler ``FastMCP`` installed, reaching through
    ``_mcp_server`` to do it. Deliberate, and narrow: ``FastMCP`` is kept for everything it
    is good at (schemas derived from type hints, so schema and implementation cannot drift),
    while the error text stops being ``"Error executing tool shell_read: {...}"`` and becomes
    the ``{"error": {...}}`` document §6 specifies. The alternative was asking every client to
    find the JSON inside a sentence, which is the kind of contract that holds until the
    sentence changes. The SDK's own in-memory transport reaches for ``_mcp_server`` the same
    way.

    ``validate_input=False`` matches what ``FastMCP`` itself passes: the tool's pydantic
    model validates arguments, so a second jsonschema pass would only change the wording of
    the same rejection.
    """

    @mcp._mcp_server.call_tool(validate_input=False)  # noqa: SLF001 -- see the docstring
    async def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await mcp.call_tool(name, arguments)
        except Exception as exc:
            shellbox = _shellbox_cause(exc)
            if shellbox is None:
                # Not a shellbox condition: an unknown tool name, or arguments pydantic
                # rejected. §6's codes describe sessions and tmux, so inventing one here
                # would be worse than the SDK's own message -- passed through unchanged.
                logger.warning("tools/call %s failed outside the taxonomy: %s", name, exc)
                return types.CallToolResult(
                    isError=True, content=[types.TextContent(type="text", text=str(exc))]
                )
            return types.CallToolResult(
                isError=True,
                content=[
                    types.TextContent(
                        type="text", text=json.dumps(error_payload(shellbox), sort_keys=True)
                    )
                ],
                # Left unset on purpose: `structuredContent` is contracted to match the
                # tool's OUTPUT schema, and an error body does not. The text is the payload.
            )


@dataclass(frozen=True, slots=True)
class HostContext:
    """Who this host is and whose it is, resolved once per process.

    Separate from ``Settings`` on purpose: ``Settings`` is a pure function of the environment,
    while this is the result of reading (and possibly writing) the identity cache under a lock.
    See ``config.py``'s docstring for why that split is load-bearing.
    """

    host_id: str
    kind: str
    owner_email: str | None
    """``None`` means enrollment is DEFERRED (E2d): no credential, nothing cached, and
    ``SHELLBOX_OWNER_EMAIL`` unset. Shell tools still work -- only the inventory waits."""
    sandbox_id: str | None = None
    gateway_host: str | None = None


def resolve_host_context(settings: Settings) -> HostContext:
    """Resolve identity explicitly, at a point where writing a file is expected.

    ⚠️ ``credential_email=None``: resolving the *creating user* from the sandbox's ambient
    credential (D4, ``current_user.me()``) belongs to ``enroll.py`` and is not wired up yet. So
    ``owner_email`` currently comes from the cache, then ``SHELLBOX_OWNER_EMAIL``, then defers.
    That is E2b/E2c/E2d working as specified -- not a stub -- and E2a's reconciliation arrives
    with the credential.
    """
    host = identity.resolve_host_id(
        settings.state_dir,
        explicit=settings.host_id_override,
        # `recovered` (the @shellbox_host_id tmux stamp) is W7's: nothing stamps it yet, so
        # passing it would be passing None with extra steps.
    )
    owner = identity.resolve_owner_email(
        settings.state_dir,
        credential_email=None,
        env_email=settings.owner_email,
    )
    return HostContext(
        host_id=host.host_id,
        kind=host.kind,
        owner_email=owner.owner_email,
        sandbox_id=host.sandbox_id,
        gateway_host=host.gateway_host,
    )


def _open_registry(settings: Settings) -> Registry:
    """Pick a registry. NEVER fatal -- an unusable one degrades to ``NullRegistry``.

    Unset ``SHELLBOX_DATABASE_URL`` yielding ``NullRegistry`` is §5's design choice, not a
    fallback bug. This catch covers the other half: a DSN whose driver is missing or whose
    URL will not parse fails *here*, at engine construction, and letting that propagate
    would turn an inventory problem into "no agent on this host can get a shell."
    """
    try:
        return create_registry(settings.database_dsn)
    except Exception:
        logger.warning(
            "could not open the registry from SHELLBOX_DATABASE_URL; continuing with no "
            "inventory (shells still work, the inventory will be stale)",
            exc_info=True,
        )
        return NullRegistry()


def build_server(
    settings: Settings | None = None,
    registry: Registry | None = None,
    host: HostContext | None = None,
) -> FastMCP[Any]:
    """Construct a server. Called once per process by ``serve`` -- and twice by a test.

    Every tool is a closure over ``settings`` and ``registry``, and nothing else. Two
    independently constructed servers in one process therefore see identical truth,
    because the only thing either of them reads is tmux (asserted by
    ``tests/integration/test_no_session_state.py``).
    """
    resolved = Settings.from_env() if settings is None else settings
    # Before identity resolution, not after: the identity cache lives in this directory, and
    # `ensure_state_dir` is the only thing that creates it with mode 0700.
    try:
        resolved.ensure_state_dir()
    except OSError as exc:
        logger.warning("could not create SHELLBOX_STATE_DIR %r: %s", resolved.state_dir, exc)
    identified = resolve_host_context(resolved) if host is None else host
    store: Registry = _open_registry(resolved) if registry is None else registry
    mcp: FastMCP[Any] = FastMCP(
        SERVER_NAME, instructions=INSTRUCTIONS, log_level=resolved.log_level
    )

    def adapter() -> TmuxAdapter:
        """A NEW adapter per tool call. See the module docstring: this is the design.

        Construction validates the socket path against the platform's ``sun_path`` limit,
        so a too-long ``SHELLBOX_TMUX_SOCKET`` raises ``tmux_unavailable`` from the tool
        that hit it rather than killing the process at startup -- an agent can act on the
        first, and can only report the second as a failed handshake.
        """
        return TmuxAdapter(resolved.tmux_config())

    def session_id(tmux_name: str) -> str:
        return naming.session_id(identified.host_id, tmux_name)

    def tmux_name_of(session: str) -> str:
        """Accept either a bare tmux name or a full ``<host_id>:<tmux_name>`` session id.

        ``rpartition``, not ``partition``, and it still matters even though a resolved
        ``host_id`` is now a colon-free uuid4: ``$SHELLBOX_HOST_ID`` can be set to anything,
        and session names cannot contain a colon (``naming.SESSION_NAME_RE``), so the LAST
        colon is the only unambiguous separator. (`identity.py` rejects a colon in a cached or
        overridden id, so the two-colon case that originally motivated this -- the abandoned
        ``lakebox:<sandbox_id>`` derivation -- can no longer arise.)

        A session id carrying a DIFFERENT host is ``invalid_name`` rather than
        ``not_found``: the session may well exist, but not on this host, and this process
        can only address its own tmux server. Reporting ``not_found`` would invite a
        caller to retry it here forever.
        """
        if ":" not in session:
            return session
        host, _, name = session.rpartition(":")
        if host != identified.host_id:
            raise InvalidName(
                f"session id {session!r} belongs to host {host!r}, not this host "
                f"({identified.host_id!r}); this process can only address its own tmux server",
                session=session,
            )
        return name

    def project(record: RegistrySessionRecord) -> str | None:
        """Write one session row. Returns a warning for the payload, or ``None``.

        🔴 Never raises. Per §9, tmux wins: a registry failure is a stale inventory, not a
        failed tool call, and a Lakebase outage must never stop an agent getting a shell.

        The warning text deliberately carries only the exception TYPE. A SQLAlchemy error
        string can include the failing statement and its bound parameters, which for these
        rows means paths and an owner email; the full exception goes to stderr, where it is
        a diagnostic, and not into a payload an agent may echo anywhere.

        ⚠️ **Expected to warn on every call until ``enroll.py`` lands.** ``sessions.host_id`` is
        a foreign key to ``hosts``, and writing the ``hosts`` row is W7's (``enroll.py``,
        E1-E7). Against a reachable-but-unenrolled database this projection therefore fails and
        every create reports a ``registry_warning`` -- measured, not assumed. That is the
        correct behaviour (the shell still works), and it is why this is worth a comment: the
        next person to see the warning should look for the missing ``hosts`` row, not for a bug
        here.
        """
        if record.owner_email is _OWNER_UNRESOLVED:
            # 🔴 Never invent an owner. This column is what #7's ACL will filter on, so a
            # placeholder is not a harmless gap -- it is a row that a future `WHERE
            # owner_email = ...` either grants to nobody or, worse, matches for whoever ends up
            # owning the placeholder string. An earlier version wrote the literal "unknown"
            # here, which would have accumulated real sessions under a fake principal.
            #
            # Skipping is E2d working as designed: tools keep working, the inventory waits for a
            # credential. The warning says so, because a silently missing row is the one outcome
            # nobody can debug.
            logger.warning(
                "not writing session %s to the registry: owner_email is unresolved "
                "(enrollment deferred). Set SHELLBOX_OWNER_EMAIL, or wait for enroll.py to "
                "resolve the sandbox creator from its credential.",
                record.session_id,
            )
            return (
                "inventory deferred: this host has no resolved owner_email yet, so the session "
                "was not recorded. The shell itself is unaffected."
            )
        try:
            store.upsert_session(record)
        except Exception as exc:  # noqa: BLE001 -- see the docstring: this may not raise
            logger.warning(
                "registry projection failed for session %s (status=%s): %s",
                record.session_id,
                record.status,
                exc,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            return f"registry unavailable ({type(exc).__name__}); the inventory may be stale"
        return None

    def session_row(
        tmux_name: str,
        status: str,
        *,
        cwd: str | None = None,
        cols: int | None = None,
        rows: int | None = None,
    ) -> RegistrySessionRecord:
        now = datetime.now(UTC)
        return RegistrySessionRecord(
            session_id=session_id(tmux_name),
            host_id=identified.host_id,
            tmux_name=tmux_name,
            # W7 resolves this from `current_user.me()`. Until then an unset
            # SHELLBOX_OWNER_EMAIL is recorded as "unknown" rather than skipping the write:
            # #7's per-owner filtering is a WHERE clause over this column, and a missing
            # row is harder to notice later than an obviously unresolved one.
            owner_email=identified.owner_email or _OWNER_UNRESOLVED,
            last_activity_at=now,
            status=status,
            cwd=cwd,
            cols=cols,
            rows=rows,
            created_at=now,
        )

    @mcp.tool()
    @_tool_errors
    def shell_create(
        name: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        cols: int | None = None,
        rows: int | None = None,
    ) -> CreateResult:
        """Create a named tmux shell session, or adopt the existing one of that name.

        `created: false` with a successful call means a session of this name already
        existed with a matching working directory -- an idempotent re-create, which is what
        lets a restarted agent call this unconditionally. A CONFLICTING `cwd` is
        `already_exists` instead: silently handing back a shell pointed at another
        directory is the more dangerous of the two answers.

        `cwd` defaults to this server process's working directory, `cols`/`rows` to
        SHELLBOX_DEFAULT_COLS/_ROWS (80x24). `env` entries are set in the pane, not in this
        process.

        Errors: invalid_name | already_exists | bad_cwd | tmux_unavailable | tmux_error.
        """
        result = adapter().create(name, cwd=cwd, env=env, cols=cols, rows=rows)
        warning = project(
            session_row(
                result.tmux_name, "live", cwd=result.cwd, cols=result.cols, rows=result.rows
            )
        )
        return CreateResult(
            session=session_id(result.tmux_name),
            tmux_name=result.tmux_name,
            cwd=result.cwd,
            cols=result.cols,
            rows=result.rows,
            created=result.created,
            incarnation=result.incarnation,
            host_id=identified.host_id,
            registry_warning=warning,
        )

    @mcp.tool()
    @_tool_errors
    def shell_send(
        session: str, text: str | None = None, keys: list[str] | None = None
    ) -> SendResult:
        """Submit input to a session: `text` first, then `keys`. Ordering is guaranteed.

        At least one of `text`/`keys` is required. `text` is literal and needs no escaping
        or quoting -- it is delivered through a tmux buffer, so `;`, a leading `-`, control
        bytes and multi-byte UTF-8 all arrive as written. Newlines in `text` are submitted
        as newlines; to press Enter without text, use `keys: ["Enter"]`.

        `keys` are tmux key NAMES from a fixed allowlist (`Enter`, `Escape`, `Tab`, `Up`,
        `C-c`, `M-x`, `F1`...), lower-case after the modifier. Anything else is
        `invalid_key`.

        🔴 `delivery` is always "unverified" and `submitted_bytes` counts bytes handed to
        tmux, NOT bytes the pane's process read. Nothing here is a receipt: read the
        session back to observe an effect. A single line at or over
        SHELLBOX_MAX_SEND_LINE_BYTES (1000) is rejected as `line_too_long` before tmux is
        touched, because the pty line discipline would drop it on macOS and TRUNCATE it on
        Linux -- and a truncated command is a different, still-executable command.

        Errors: not_found | no_payload | invalid_key | too_large | line_too_long |
        invalid_name | tmux_unavailable | tmux_error.
        """
        name = tmux_name_of(session)
        result = adapter().send(name, text=text, keys=keys)
        return SendResult(
            session=session_id(result.tmux_name),
            tmux_name=result.tmux_name,
            submitted_bytes=result.submitted_bytes,
            keys_sent=list(result.keys_sent),
            incarnation=result.incarnation,
            delivery=result.delivery,
        )

    @mcp.tool()
    @_tool_errors
    def shell_read(session: str, lines: int = 0) -> ReadResult:
        """Capture a session's screen, with ANSI escapes PRESERVED.

        `lines: 0` (the default) is the visible pane. `lines: N` also includes up to N
        lines of scrollback above it.

        `alive: false` means the pane's process has exited -- its final output is still
        readable, which is why the session is kept rather than destroyed. `scrollback_lines`
        and `history_limit` are raw tmux facts: a caller that asked for `lines: N` and sees
        `scrollback_lines < N` received everything that exists. tmux never truncates a
        capture, so there is no `truncated` field to trust.

        Errors: not_found | invalid_name | invalid_dimensions | tmux_unavailable |
        tmux_error.
        """
        name = tmux_name_of(session)
        result = adapter().read(name, lines=lines)
        return ReadResult(
            session=session_id(result.tmux_name),
            tmux_name=result.tmux_name,
            content=result.content,
            lines=result.lines,
            cols=result.cols,
            rows=result.rows,
            alive=result.alive,
            scrollback_lines=result.scrollback_lines,
            history_limit=result.history_limit,
        )

    @mcp.tool()
    @_tool_errors
    def shell_list() -> ListResult:
        """List every session on this host's tmux server, read from tmux.

        Not from the registry: tmux is the authority, and other agents on this host share
        the same server. `cwd` is where each shell IS now, not where it was created.

        `foreign: true` with `incarnation: null` marks a session shellbox cannot prove it
        owns -- either created by something else, or observed in the window between its
        creation and its identity stamp. `shell_send`/`shell_kill` refuse such a session
        with `not_found`.

        An empty list means this host has no sessions. It never means "tmux is broken":
        any unrecognised tmux failure is reported as `tmux_error` instead, because
        reporting a broken tmux as a healthy empty inventory is what would let
        reconciliation mark every live session on the host dead.

        Errors: tmux_unavailable | tmux_error.
        """
        records = adapter().list_sessions()
        return ListResult(
            host_id=identified.host_id,
            sessions=[_entry(record, session_id(record.tmux_name)) for record in records],
        )

    @mcp.tool()
    @_tool_errors
    def shell_resize(session: str, cols: int, rows: int) -> ResizeResult:
        """Resize a session's window.

        Worth doing before running a TUI: the pane is created at 80x24, and a program that
        has already drawn itself at one size repaints incrementally after a resize.

        Errors: not_found | invalid_dimensions | invalid_name | tmux_unavailable |
        tmux_error.
        """
        name = tmux_name_of(session)
        new_cols, new_rows = adapter().resize(name, cols, rows)
        return ResizeResult(session=session_id(name), tmux_name=name, cols=new_cols, rows=new_rows)

    @mcp.tool()
    @_tool_errors
    def shell_kill(session: str) -> KillResult:
        """Kill a session. Idempotent.

        `killed: false` with a successful call means there was nothing to kill -- two
        agents racing a kill must not produce a spurious error for the loser.

        `not_found` is a different answer and a real one: it means the session EXISTS but
        carries no shellbox identity, so this server will not kill it. Killing a session
        shellbox cannot prove it owns must fail rather than succeed silently.

        Errors: not_found | invalid_name | tmux_unavailable | tmux_error.
        """
        name = tmux_name_of(session)
        killed = adapter().kill(name)
        warning = project(session_row(name, "reaped")) if killed else None
        return KillResult(
            session=session_id(name),
            tmux_name=name,
            killed=killed,
            registry_warning=warning,
        )

    _install_error_boundary(mcp)
    return mcp


def _entry(record: SessionRecord, session: str) -> SessionEntry:
    """One ``shell_list`` row.

    ``foreign`` and ``incarnation`` are derived from the SAME value, so they cannot
    disagree: ``record.foreign`` is true exactly when the incarnation is absent.
    """
    return SessionEntry(
        session=session,
        tmux_name=record.tmux_name,
        cwd=record.cwd,
        cols=record.cols,
        rows=record.rows,
        created_at=record.created_at,
        last_activity_at=record.last_activity_at,
        alive=record.alive,
        incarnation=record.incarnation,
        foreign=record.foreign,
    )


def serve(settings: Settings | None = None) -> None:
    """Run the stdio server until the client disconnects.

    The startup checks are all non-fatal on purpose. A too-long socket path or a missing
    tmux binary is reported per tool call as ``tmux_unavailable`` -- an answer an agent can
    act on -- whereas exiting here produces an opaque handshake failure in the harness, and
    the harness is where these misconfigurations actually happen.
    """
    resolved = Settings.from_env() if settings is None else settings
    try:
        resolved.ensure_state_dir()
    except OSError as exc:
        logger.warning("could not create SHELLBOX_STATE_DIR %r: %s", resolved.state_dir, exc)
    try:
        naming.validate_socket_path(resolved.socket_path)
    except ShellboxError as exc:
        logger.error("tmux socket path is unusable: %s", exc.message)

    # Resolved here and passed in, rather than letting `build_server` do it: identity assignment
    # writes a file and takes a lock, and doing it twice per process to satisfy a log line would
    # be one arbitrated transaction too many.
    identified = resolve_host_context(resolved)
    logger.info(
        "shellbox-mcp serving on stdio: tmux=%s socket=%s host_id=%s (%s, %s) owner=%s registry=%s",
        resolved.tmux_bin,
        resolved.socket_path,
        identified.host_id,
        identified.kind,
        f"sandbox {identified.sandbox_id}" if identified.sandbox_id else "no sandbox_id",
        identified.owner_email or "DEFERRED",
        "postgres" if resolved.database_dsn else "none",
    )
    build_server(resolved, host=identified).run()
