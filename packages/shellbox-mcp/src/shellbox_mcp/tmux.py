"""``TmuxAdapter`` -- the ONLY place tmux argv is built (plan §7).

Every command form here was transcribed from ``spike/tmux_spike.py``, which measured it in
both lanes (tmux 3.6b/macOS and tmux 3.4/Ubuntu 24.04 -- the sandbox's version). **The
spike is the oracle**: if this module and the plan's prose ever disagree, the spike wins,
and a new form goes into the spike first and into this module second.

Three properties this module is built to preserve, each of which cost a review round:

* **Zero in-process session state.** No caches, no session dicts. tmux is the authority and
  several MCP processes run concurrently against one tmux server, so anything remembered
  here is wrong the moment another process acts (T-CONC-2 asserts it).
* **Every ``-t`` comes from ``target()``.** See ``target.py``; enforced over this module's
  AST by ``tests/unit/test_target.py``.
* **No ``window-size manual`` at global scope, anywhere.** A GLOBAL ``window-size manual``
  kills the tmux server on the NEXT ``new-session`` (15/15 in both lanes, SIGSEGV in
  ``clients_calculate_size``), taking every other pooled agent's sessions with it. The
  per-window form ``-w -t '=<name>:'`` is safe, and Phase 2 needs neither.
  ``tests/unit/test_no_global_window_size.py`` greps for it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol

from shellbox_mcp import naming
from shellbox_mcp.errors import (
    NO_SERVER,
    AlreadyExists,
    InvalidDimensions,
    LineTooLong,
    NoPayload,
    NotFound,
    TmuxError,
    TooLarge,
    classify_stderr,
    tmux_failure,
)
from shellbox_mcp.keys import validate_keys
from shellbox_mcp.target import new_session_name, target

logger = logging.getLogger(__name__)

__all__ = [
    "FIELD_COUNT",
    "LIST_FIELDS",
    "LIST_FORMAT",
    "CommandResult",
    "CreateResult",
    "ReadResult",
    "SendResult",
    "SessionRecord",
    "SubprocessRunner",
    "TmuxAdapter",
    "TmuxConfig",
]

# --------------------------------------------------------------------------------------
# The `list-sessions -F` format. NORMATIVE FIELD ORDER, transcribed from the spike's
# FIELDS_8 -- not the other way round.
#
# The ordering rule: **the two fields that may legitimately be EMPTY go LAST.** An earlier
# plan revision put `incarnation` in position 2, which is unsafe in a specific and silent
# way: a parser written to it reads `session_created` -- always non-empty -- as the
# incarnation, so the "empty is never a match" rule below is satisfied for every session
# and every session is misidentified as shellbox-owned carrying a bogus incarnation.
#
# `pane_current_path` is deliberately NOT here: it is unbounded and user-controlled, and a
# path containing a TAB or LF would corrupt the record. It is fetched per session instead
# (§7.4), where the output is a whole stream needing no delimiter.
# --------------------------------------------------------------------------------------
LIST_FIELDS: tuple[str, ...] = (
    "#{session_name}",
    "#{session_created}",
    "#{session_activity}",
    "#{window_width}",
    "#{window_height}",
    "#{pane_dead}",
    "#{@shellbox_incarnation}",
    "#{@shellbox_cwd}",
)
LIST_FORMAT = "\t".join(LIST_FIELDS)
FIELD_COUNT = len(LIST_FIELDS)

# maxsplit is FIELD_COUNT, i.e. one MORE than the 7 splits a well-formed record needs.
# That is the whole point: with maxsplit=7 a record carrying an extra TAB would silently
# collapse into 8 fields and pass the count check with a corrupted tail, so a TAB injected
# into `@shellbox_incarnation` would parse as a valid-looking record. With maxsplit=8 a
# 9-field record still yields 9 and is dropped. The maxsplit bounds the work; the count
# check does the detecting.
_LIST_MAXSPLIT = FIELD_COUNT

# Numeric/bounded formats, safe to read as a TAB group because none of them can contain a
# TAB. Anything user-controlled (a path, a user option) is read one value per invocation.
_READ_FIELDS = (
    "#{window_width}",
    "#{window_height}",
    "#{pane_dead}",
    "#{history_size}",
    "#{history_limit}",
)

INCARNATION_OPTION = "@shellbox_incarnation"
CWD_OPTION = "@shellbox_cwd"


class CommandResult(NamedTuple):
    """One tmux invocation's outcome.

    ``stdout_raw`` is deliberately NOT stripped. Measured (spike S10, both lanes): an
    unstamped session's record ends in TABs because its last two fields are empty, and
    ``.strip()`` eats them -- turning 8 fields into 6 and dropping exactly the sessions
    orphan reconciliation exists to find. Trailing empty fields are significant data.
    """

    argv: tuple[str, ...]
    rc: int
    stdout_raw: str
    stderr: str


class Runner(Protocol):
    """How ``TmuxAdapter`` reaches tmux. Injectable so tests can record and fault-inject."""

    def __call__(self, argv: Sequence[str], stdin: bytes | None = None) -> CommandResult: ...


@dataclass(frozen=True)
class TmuxConfig:
    """Adapter configuration.

    Defaults mirror §5's table. Env-var resolution is NOT done here -- ``config.py`` (W4)
    owns that and constructs this -- so that the adapter is a pure function of its
    configuration and tests never need to mutate the environment.
    """

    socket_path: str
    tmux_bin: str = "tmux"
    # 20000, not 50000: measured ~445 B/line, so 50000 is ~22 MB per session and ~700 MB
    # across a 32-agent pool in the one process every agent shares (§7.3).
    history_limit: int = 20_000
    default_cols: int = 80
    default_rows: int = 24
    # Small on purpose: first attach GROWS the window, which is lossless. A large default
    # means first attach shrinks it, and a TUI's cursor-up repaint then stitches frames
    # into rewrapped debris (a Phase 4 rendering bug prevented by a Phase 2 default).
    default_terminal: str = "screen-256color"
    term: str = "xterm-256color"
    # tmux-server memory guard ONLY. It does not protect delivery -- see max_send_line_bytes.
    max_send_bytes: int = 1 << 20
    # THE correctness boundary (§8, H4): bytes since the last newline. 1000 is below both
    # platforms' line-discipline thresholds (macOS drops at >1023, Linux truncates at 4096),
    # so a dev Mac and the sandbox behave identically and neither failure mode can fire.
    max_send_line_bytes: int = 1000
    # Extra env for the tmux CLIENT process (not the pane). TERM and LC_CTYPE are forced
    # separately. LC_ALL is deliberately ABSENT and must stay absent -- see below.
    passthrough_env: tuple[str, ...] = ("HOME", "PATH", "USER", "LOGNAME", "LANG")
    # 🔴 FORCED, and the whole 8-field record depends on it. Measured in BOTH lanes: when the
    # invoking client's ctype locale is not UTF-8, tmux visually encodes the TAB in format
    # output as `_` -- in `list-sessions -F` as well as `display-message`. The entire record
    # then collapses to ONE field (`build_1785…_80_24_0_<inc>_/tmp`), every record is dropped
    # as malformed, `shell_list` reports an empty inventory, and orphan reconciliation marks
    # every live session on the host `orphaned`.
    #
    # This is not hypothetical: a locale is normally absent in a container, a systemd unit and
    # a sandbox, and PASSING `LANG` THROUGH IS NOT ENOUGH -- if the parent has no locale there
    # is nothing to pass. It must be forced. `LC_CTYPE` rather than `LC_ALL` because forcing
    # `LC_ALL` would also override collation and messages inside the user's shell.
    locale_ctype: str = "C.UTF-8"


@dataclass(frozen=True)
class SessionRecord:
    """One parsed ``list-sessions -F`` record, enriched with its per-session cwd."""

    tmux_name: str
    created_at: int
    last_activity_at: int
    cols: int
    rows: int
    alive: bool
    incarnation: str | None
    cwd: str | None
    stamped_cwd: str | None

    @property
    def foreign(self) -> bool:
        """True when ``@shellbox_incarnation`` is empty -- shellbox cannot prove it owns it.

        An empty incarnation means EITHER a create is in flight (the window between
        ``new-session`` and ``set-option`` inside the create chain) OR the session was made
        by something that is not shellbox. Both are "cannot confirm identity", which is why
        an empty incarnation is never a match on either side of a comparison.
        """
        return self.incarnation is None


@dataclass(frozen=True)
class CreateResult:
    tmux_name: str
    cwd: str
    cols: int
    rows: int
    created: bool
    incarnation: str


@dataclass(frozen=True)
class ReadResult:
    tmux_name: str
    content: str
    lines: int
    cols: int
    rows: int
    alive: bool
    scrollback_lines: int
    history_limit: int


@dataclass(frozen=True)
class SendResult:
    tmux_name: str
    submitted_bytes: int
    keys_sent: tuple[str, ...]
    incarnation: str
    # Stated in the type, not only in prose: per H4 the bytes reaching the pane PROCESS are
    # not knowable to shellbox, so nothing here may be read as a delivery receipt.
    delivery: str = "unverified"


@dataclass
class SubprocessRunner:
    """The real runner: ``subprocess.run`` with an argv LIST and ``shell=False``.

    ``TERM`` is forced for every invocation and the inherited environment is reduced to a
    handful of variables: the MCP process's environment is where the harness injects
    credentials, and a tmux server started by this client passes its environment on to
    every pane it later spawns.
    """

    config: TmuxConfig
    env: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.env = {"TERM": self.config.term, "LC_CTYPE": self.config.locale_ctype}
        for key in self.config.passthrough_env:
            value = os.environ.get(key)
            if value is not None:
                self.env[key] = value
        # LC_ALL would override LC_CTYPE, so a parent's `LC_ALL=C` would silently reinstate
        # the TAB-mangling described on TmuxConfig.locale_ctype. It is not in the
        # pass-through allowlist; this asserts that it never sneaks back in.
        assert "LC_ALL" not in self.env, (
            "LC_ALL must never be passed through: it overrides LC_CTYPE"
        )

    def __call__(self, argv: Sequence[str], stdin: bytes | None = None) -> CommandResult:
        # argv LIST, shell=False, never a string. Shell metacharacters are thus a non-issue
        # at the process boundary; H1/H2 are *tmux argv* semantics, a distinct layer.
        proc = subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            env=self.env,
            shell=False,
        )
        return CommandResult(
            argv=tuple(argv),
            rc=proc.returncode,
            stdout_raw=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )


class TmuxAdapter:
    """Stateless facade over one tmux server.

    Stateless is load-bearing, not stylistic: T-RESTART SIGKILLs the MCP process and
    expects a second process to see identical truth, and T-CONC-2 catches any cache by
    having one process observe another's kill.
    """

    def __init__(self, config: TmuxConfig, runner: Runner | None = None) -> None:
        naming.validate_socket_path(config.socket_path)
        self.config = config
        self._run_command: Runner = runner if runner is not None else SubprocessRunner(config)

    # -- plumbing ----------------------------------------------------------------------

    def _base_argv(self) -> list[str]:
        # `-f /dev/null`: do not inherit ~/.tmux.conf. A user conf could rebind keys or
        # change `default-terminal` and silently break the Phase 4 renderer.
        return [self.config.tmux_bin, "-S", self.config.socket_path, "-f", "/dev/null"]

    def _run(self, *args: str, stdin: bytes | None = None) -> CommandResult:
        return self._run_command(self._base_argv() + list(args), stdin)

    def _display_tail(self, name: str, format_field: str) -> str | None:
        """Read ONE user-controlled format field. ``None`` means the target did not resolve.

        ⚠️ ``display-message`` returns **rc=0 for every nonexistent target** (spike F6) --
        it is the only one of the seven verbs measured that does, so an rc check on it is
        worthless. The rule is: **empty output is ``not_found``**.

        And the naive form of that rule is not enough. Measured while building this
        adapter: for a MULTI-field format the placeholders expand empty but the literal
        separators SURVIVE, so ``display-message -p -t '=nope:' '#{a}\\t#{b}'`` prints
        ``"\\t\\n"`` -- non-empty stdout for a session that does not exist. Every format
        here therefore leads with ``#{session_name}``, which is non-empty for any real
        session, and resolution is decided by that field alone.

        ``maxsplit=1`` keeps the value whole: it is the one field allowed to contain a TAB.
        """
        result = self._run(
            "display-message", "-p", "-t", target(name), f"#{{session_name}}\t{format_field}"
        )
        if result.rc != 0:
            if classify_stderr(result.stderr) == NO_SERVER:
                # No server (or no socket file at all) means this session does not exist --
                # which is N1's "not_found elsewhere", expressed as an unresolved target so
                # every caller handles it the same way it handles a missing session.
                return None
            raise tmux_failure(result.stderr, session=name, context="display-message failed")
        # A value containing an LF is truncated at the first line -- unavoidable in a
        # line-oriented format, and the reason naming.py rejects LF in the cwd shellbox
        # sets itself. A foreign session's path may still trip it; that is a foreign
        # session's inventory row being approximate, not shellbox corrupting its own.
        line = result.stdout_raw.split("\n", 1)[0]
        resolved, _, tail = line.partition("\t")
        if not resolved:
            return None
        return tail

    def _display_numeric(self, name: str, format_fields: Sequence[str]) -> list[str] | None:
        """Read a group of TAB-safe (numeric/bounded) fields in ONE invocation."""
        fmt = "\t".join(("#{session_name}", *format_fields))
        result = self._run("display-message", "-p", "-t", target(name), fmt)
        if result.rc != 0:
            if classify_stderr(result.stderr) == NO_SERVER:
                return None
            raise tmux_failure(result.stderr, session=name, context="display-message failed")
        parts = result.stdout_raw.split("\n", 1)[0].split("\t")
        if not parts[0]:
            return None
        if len(parts) != len(format_fields) + 1:
            raise TmuxError(
                f"display-message returned {len(parts)} fields, expected "
                f"{len(format_fields) + 1}: {parts!r}",
                session=name,
            )
        return parts[1:]

    def _read_incarnation(self, name: str) -> str | None:
        """The session's ``@shellbox_incarnation``, with the three states kept distinct.

        ``None`` -- the session does not exist (including "no server at all").
        ``""``   -- it exists but is unstamped: mid-create, or foreign.
        Otherwise -- the incarnation.

        The three are returned rather than collapsed because ``kill`` must treat the first as
        an idempotent no-op and the second as ``not_found``, and telling them apart by
        inspecting an exception message is the kind of thing that breaks silently.
        """
        return self._display_tail(name, f"#{{{INCARNATION_OPTION}}}")

    def _resolve_owned(self, name: str) -> str:
        """Return the session's incarnation, or raise ``not_found``.

        This is the resolution step for every mutating non-create operation, and it
        replaces a pre-flight ``has-session``: it resolves the session AND reads its
        incarnation in one round-trip, which is what collapses the check-then-act window
        rather than merely moving it.

        It is detection, not prevention. No sequence of tmux commands can make "check
        incarnation" and "paste" atomic -- tmux offers no compare-and-swap -- so the
        residual race (a kill-and-recreate landing between resolution and paste) is
        accepted and documented (R12). What this buys is that misdelivery becomes
        *detectable*: the incarnation targeted is returned to the caller.
        """
        incarnation = self._read_incarnation(name)
        if incarnation is None:
            raise NotFound(f"session {name!r} does not exist", session=name)
        if not incarnation:
            raise NotFound(
                f"session {name!r} exists but carries no {INCARNATION_OPTION}: it is either "
                "mid-create or foreign, and shellbox will not act on a session it cannot "
                "prove it owns",
                session=name,
            )
        return incarnation

    def _delete_buffer(self, buffer_name: str) -> None:
        """Best-effort buffer cleanup for the ``paste-buffer`` failure path.

        ``paste-buffer -d`` only deletes on success, so a failed paste leaks the buffer.
        That is not hygiene: ``buffer-limit`` defaults to 50, server-wide across all pooled
        agents, so leaked buffers evict other agents' buffers *and* retain arbitrary agent
        input -- the exact confidentiality concern ``-d`` exists to prevent. Never raises:
        masking the original failure with a cleanup failure would hide the real cause.
        """
        result = self._run("delete-buffer", "-b", buffer_name)
        if result.rc != 0:
            logger.warning(
                "could not delete tmux buffer %r after a failed paste: %s",
                buffer_name,
                result.stderr.strip(),
            )

    # -- operations --------------------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        cols: int | None = None,
        rows: int | None = None,
        command: Sequence[str] | None = None,
    ) -> CreateResult:
        """The §7.2 create chain, as ONE tmux invocation.

        Ordering inside the chain is load-bearing, and each element cost a review round:

        1. ``start-server`` then the globals then ``new-session`` -- **in that order, in one
           invocation**. A pane fixes its history limit at creation, so setting the global
           afterwards leaves the pane at tmux's 2000 default while ``show-options -g``
           cheerfully reports 20000. The pane's ``#{history_limit}`` is the only valid
           oracle for this, which is why the earlier "verified" form was not.
        2. ``-s`` takes a **bare** name. ``-s '=build'`` creates a session literally named
           ``=build``, unreachable through ``target()`` afterwards.
        3. Both ``set-option`` calls use the anchored ``=<name>:``. The half-anchored
           ``=<name>`` returns rc=1 and stores NOTHING, which is how an earlier revision
           shipped an inert incarnation whose own tests compared ``"" == ""`` and passed.
        """
        naming.validate_session_name(name)
        cols, rows = naming.validate_dimensions(
            self.config.default_cols if cols is None else cols,
            self.config.default_rows if rows is None else rows,
            session=name,
        )
        resolved_cwd = naming.validate_cwd(os.getcwd() if cwd is None else cwd, session=name)
        env_args = naming.validate_env(env, session=name)
        incarnation = str(uuid.uuid4())

        # One tmux command per line, so this block can be read against §7.2 verbatim. The
        # formatter's one-argument-per-line expansion turns it into a 60-line list in which
        # a misplaced `;` or a `-g` that should not be there is genuinely hard to see, and
        # this is the composition four review rounds were spent getting right.
        # fmt: off
        chain: list[str] = [
            "start-server",
            ";",
            "set-option", "-g", "history-limit", str(self.config.history_limit),
            ";",
            "set-option", "-g", "status", "off",
            ";",
            "set-option", "-g", "default-terminal", self.config.default_terminal,
            ";",
            # Without this, the pane's process exiting destroys the session, the server
            # exits, every later command fails `no server running`, and the process's final
            # output is lost. Cost: `has-session` no longer implies "alive", so liveness
            # reads `#{pane_dead}` (R9).
            "set-option", "-g", "remain-on-exit", "on",
            ";",
            "new-session", "-d", "-s", new_session_name(name),
            "-x", str(cols), "-y", str(rows), "-c", resolved_cwd,
            *env_args,
            *(command or ()),
            ";",
            "set-option", "-t", target(name), INCARNATION_OPTION, incarnation,
            ";",
            "set-option", "-t", target(name), CWD_OPTION, resolved_cwd,
        ]
        # fmt: on
        result = self._run(*chain)
        if result.rc != 0:
            return self._handle_create_failure(result, name, resolved_cwd, cols, rows)
        return CreateResult(
            tmux_name=name,
            cwd=resolved_cwd,
            cols=cols,
            rows=rows,
            created=True,
            incarnation=incarnation,
        )

    def _handle_create_failure(
        self, result: CommandResult, name: str, cwd: str, cols: int, rows: int
    ) -> CreateResult:
        """Turn a failed create into an idempotent success, or a precise error.

        tmux resolves the create race atomically -- a second ``new-session -d -s build``
        returns ``duplicate session: build`` with the original intact -- so there is no
        lock and no check-then-act here. The only question left is whether the existing
        session is the one the caller asked for.
        """
        if classify_stderr(result.stderr) != "already_exists":
            raise tmux_failure(result.stderr, session=name, context="create failed")

        existing_incarnation = self._read_incarnation(name)
        existing_cwd = self._display_tail(name, f"#{{{CWD_OPTION}}}")
        if not existing_incarnation:
            # Name taken by a session shellbox cannot prove it owns (foreign, or a
            # concurrent create still inside its own chain). Reusing it would hand the
            # caller someone else's shell, so this is an error rather than created=False.
            raise AlreadyExists(
                f"session {name!r} already exists but carries no {INCARNATION_OPTION}",
                session=name,
            )
        # realpath BOTH sides: measured (M20) that on macOS `/tmp` resolves to
        # `/private/tmp` for pane_current_path but not for session_path, so a naive string
        # compare reports a spurious conflict in CI.
        if os.path.realpath(existing_cwd or "") != os.path.realpath(cwd):
            raise AlreadyExists(
                f"session {name!r} already exists with cwd {existing_cwd!r}, not {cwd!r}",
                session=name,
            )
        logger.info("session %r already exists with a matching cwd; reusing it", name)
        return CreateResult(
            tmux_name=name,
            cwd=os.path.realpath(existing_cwd or cwd),
            cols=cols,
            rows=rows,
            created=False,
            incarnation=existing_incarnation,
        )

    def send(
        self,
        name: str,
        *,
        text: str | None = None,
        keys: Sequence[str] | None = None,
    ) -> SendResult:
        """Deliver ``text`` through a tmux buffer, then ``keys``. Ordering is guaranteed (M18).

        Text goes through ``load-buffer``/``paste-buffer`` rather than ``send-keys -l``
        because ``send-keys -l ';'`` returns rc=0 and the character NEVER ARRIVES (tmux
        consumes a standalone ``;`` as its command separator, and ``--`` does not help),
        while text starting with ``-`` parses as a flag.

        Every guard runs before tmux is invoked at all, so ``line_too_long`` is returned
        without touching the server.
        """
        naming.validate_session_name(name)
        validated_keys = validate_keys(keys)
        payload = b"" if text is None else text.encode("utf-8")
        if not payload and not validated_keys:
            raise NoPayload("shell_send requires at least one of text or keys", session=name)
        if payload:
            self._check_size(payload, name)

        incarnation = self._resolve_owned(name)
        submitted = self._paste(name, payload) if payload else 0
        if validated_keys:
            result = self._run("send-keys", "-t", target(name), "--", *validated_keys)
            if result.rc != 0:
                raise tmux_failure(result.stderr, session=name, context="send-keys failed")
        return SendResult(
            tmux_name=name,
            submitted_bytes=submitted,
            keys_sent=tuple(validated_keys),
            incarnation=incarnation,
        )

    def _check_size(self, payload: bytes, name: str) -> None:
        if len(payload) > self.config.max_send_bytes:
            raise TooLarge(
                f"payload is {len(payload)} bytes, over SHELLBOX_MAX_SEND_BYTES "
                f"({self.config.max_send_bytes})",
                session=name,
            )
        # Bytes since the last newline, NOT total bytes: this is the quantity the pty line
        # discipline destroys. Over the limit, macOS discards the whole line and Linux
        # TRUNCATES it -- and a truncated command is a different, still-executable command,
        # which is the worse of the two failures and the sandbox's behaviour. The comparison
        # is `>=` because the limit is specified as the first REJECTED length (§11.3).
        longest_line = max((len(segment) for segment in payload.split(b"\n")), default=0)
        if longest_line >= self.config.max_send_line_bytes:
            raise LineTooLong(
                f"a line of {longest_line} bytes reaches SHELLBOX_MAX_SEND_LINE_BYTES "
                f"({self.config.max_send_line_bytes}); the pty line discipline would drop it "
                "on macOS and truncate it on Linux, and a truncated command is a different, "
                "still-executable command",
                session=name,
            )

    def _paste(self, name: str, payload: bytes) -> int:
        # Per-call unique buffer name -- required, not stylistic: a shared name would let
        # concurrent pooled agents paste each other's input into each other's panes.
        buffer_name = f"shellbox-{uuid.uuid4()}"
        # `load-buffer -b <name> -` reads STDIN, so agent input never touches disk.
        load = self._run("load-buffer", "-b", buffer_name, "-", stdin=payload)
        if load.rc != 0:
            raise tmux_failure(load.stderr, session=name, context="load-buffer failed")
        # `-d` drops the buffer after pasting, so payloads do not linger in the server's
        # buffer stack where any other pane could paste them back.
        paste = self._run("paste-buffer", "-d", "-b", buffer_name, "-t", target(name))
        if paste.rc != 0:
            self._delete_buffer(buffer_name)
            raise tmux_failure(paste.stderr, session=name, context="paste-buffer failed")
        return len(payload)

    def read(self, name: str, *, lines: int = 0) -> ReadResult:
        """``capture-pane -p -e``. ``lines=0`` is the visible pane.

        ``-e`` preserves ANSI (without it a red line captures as ``RED``, with it as
        ``\\033[31mRED\\033[39m``) -- a deliberate divergence from omnigent, which strips
        it, because xterm.js needs the escapes and the omission looks fine until Phase 4.
        ``-J`` is deliberately NOT used: it destroys the wrapping the terminal needs.
        """
        naming.validate_session_name(name)
        if lines < 0:
            raise InvalidDimensions(f"lines must be >= 0, got {lines}", session=name)
        metrics = self._display_numeric(name, _READ_FIELDS)
        if metrics is None:
            raise NotFound(f"session {name!r} does not exist", session=name)
        width, height, pane_dead, history_size, history_limit = metrics

        capture_args = ["capture-pane", "-p", "-e", "-t", target(name)]
        if lines:
            capture_args += ["-S", f"-{lines}"]
        result = self._run(*capture_args)
        if result.rc != 0:
            raise tmux_failure(result.stderr, session=name, context="capture-pane failed")
        return ReadResult(
            tmux_name=name,
            content=result.stdout_raw,
            lines=lines,
            cols=_as_int(width, 0),
            rows=_as_int(height, 0),
            # `alive` is the single source of truth for liveness -- there is no separate
            # `dead_pane` field. With `remain-on-exit on`, has-session no longer implies
            # alive, so this reads the pane, not the session (R9).
            alive=pane_dead == "0",
            scrollback_lines=_as_int(history_size, 0),
            history_limit=_as_int(history_limit, 0),
        )

    def list_sessions(self) -> list[SessionRecord]:
        """Every session on the server, parsed from the 8-field format.

        🔴 Only the two exact ``no_server`` signatures yield an empty list -- ``no server
        running`` (the socket exists, nothing is listening) and ``error connecting to … (No
        such file or directory)`` (the cold start, no socket file yet). Every other non-zero
        exit raises, including ``error connecting to … (File name too long)``, which shares a
        prefix with the second but means a misconfigured path.

        A permissive "probably no sessions" fallback would report a broken tmux as a healthy
        empty inventory, and orphan reconciliation would mark every live session on the host
        ``orphaned`` on the strength of it.
        """
        result = self._run("list-sessions", "-F", LIST_FORMAT)
        if result.rc != 0:
            if classify_stderr(result.stderr) == NO_SERVER:
                return []
            raise tmux_failure(result.stderr, context="list-sessions failed")
        records = []
        for parsed in self._parse_list(result.stdout_raw):
            enriched = self._enrich(parsed)
            if enriched is not None:
                records.append(enriched)
        return records

    def _parse_list(self, stdout_raw: str) -> list[SessionRecord]:
        """Parse the raw ``-F`` output. NEVER strips before splitting."""
        records: list[SessionRecord] = []
        # Split lines off the RAW stdout and drop only the single trailing newline tmux
        # ends its output with. `.strip()` (or `.splitlines()` on stripped text) would eat
        # the trailing TABs of an unstamped session and cost it four of its eight fields.
        lines = stdout_raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        malformed = 0
        for line in lines:
            fields = line.split("\t", _LIST_MAXSPLIT)
            if len(fields) != FIELD_COUNT:
                malformed += 1
                logger.warning(
                    "dropping malformed list-sessions record with %d fields (expected %d): %r",
                    len(fields),
                    FIELD_COUNT,
                    line,
                )
                continue
            name, created, activity, width, height, pane_dead, incarnation, stamped_cwd = fields
            if not name:
                malformed += 1
                logger.warning("dropping list-sessions record with an empty session name: %r", line)
                continue
            records.append(
                SessionRecord(
                    tmux_name=name,
                    created_at=_as_int(created, 0),
                    last_activity_at=_as_int(activity, 0),
                    cols=_as_int(width, 0),
                    rows=_as_int(height, 0),
                    alive=pane_dead == "0",
                    # Empty is None, never "": an equality test two empty strings can
                    # satisfy is not an identity check, and this is the type-level version
                    # of that rule -- `None == None` is a comparison a caller has to mean.
                    incarnation=incarnation or None,
                    cwd=None,
                    stamped_cwd=stamped_cwd or None,
                )
            )
        if lines and not records:
            # 🔴 tmux listed sessions and NONE of them parsed. Never return that as an empty
            # inventory: an empty list is indistinguishable from "this host has no sessions",
            # and orphan reconciliation would mark every live session on the host `orphaned`
            # on the strength of it.
            #
            # The known cause is a non-UTF-8 ctype locale, which makes tmux encode the record's
            # TABs as `_` and collapses all eight fields into one (see TmuxConfig.locale_ctype).
            # The adapter forces `LC_CTYPE`, so this should be unreachable -- which is exactly
            # why it must be loud if it happens.
            raise TmuxError(
                f"list-sessions returned {len(lines)} record(s) and none parsed into "
                f"{FIELD_COUNT} TAB-separated fields ({malformed} malformed). If the fields "
                "look joined by '_', the tmux client's ctype locale is not UTF-8 and tmux has "
                f"encoded the TAB separators. First record: {lines[0]!r}"
            )
        return records

    def _enrich(self, record: SessionRecord) -> SessionRecord | None:
        """Attach the authoritative cwd, read per session.

        ``cwd`` is ``#{pane_current_path}`` -- where the shell IS -- not ``#{session_path}``
        where it was created; measured (M20) they diverge after any ``cd``, so the latter
        would make the inventory show stale directories.

        Two invocations rather than one: both values are path-shaped, so a single TAB-joined
        format would be ambiguous for exactly the inputs that motivate reading them
        separately. The per-session read is authoritative and the in-format copy is used
        only for the presence/field-count check; a disagreement is logged and the
        per-session value wins.
        """
        name = record.tmux_name
        if not naming.SESSION_NAME_RE.match(name):
            # A foreign session whose name shellbox could never address through target().
            # It still belongs in the inventory, unenriched, rather than silently vanishing.
            logger.info("session %r has a name shellbox cannot target; listing it unenriched", name)
            return record
        current_path = self._display_tail(name, "#{pane_current_path}")
        if current_path is None:
            logger.info("session %r vanished between list-sessions and its cwd read", name)
            return None
        stamped = self._display_tail(name, f"#{{{CWD_OPTION}}}")
        stamped_value = stamped or None
        if stamped_value != record.stamped_cwd:
            logger.warning(
                "session %r: %s reads %r per session but %r in the list format; "
                "the per-session value wins",
                name,
                CWD_OPTION,
                stamped_value,
                record.stamped_cwd,
            )
        return SessionRecord(
            tmux_name=name,
            created_at=record.created_at,
            last_activity_at=record.last_activity_at,
            cols=record.cols,
            rows=record.rows,
            alive=record.alive,
            incarnation=record.incarnation,
            cwd=current_path,
            stamped_cwd=stamped_value,
        )

    def resize(self, name: str, cols: int, rows: int) -> tuple[int, int]:
        """``resize-window -t '=<name>:' -x -y``.

        ``=<name>`` is NOT safe here: this is the one verb the half-anchored form fails to
        protect -- ``resize-window -t '=bui'`` returns rc=0 and resizes ``build``.
        """
        naming.validate_session_name(name)
        cols, rows = naming.validate_dimensions(cols, rows, session=name)
        self._resolve_owned(name)
        result = self._run("resize-window", "-t", target(name), "-x", str(cols), "-y", str(rows))
        if result.rc != 0:
            raise tmux_failure(result.stderr, session=name, context="resize-window failed")
        return cols, rows

    def kill(self, name: str) -> bool:
        """Kill a session. Returns ``False`` (and succeeds) when there was nothing to kill.

        Idempotent by design: two pooled agents racing a kill must not produce a spurious
        error for the loser. ``not_found`` is still reachable and distinct -- it is raised
        when the session EXISTS but carries no incarnation, because killing a session
        shellbox cannot prove it owns must fail rather than succeed silently.
        """
        naming.validate_session_name(name)
        incarnation = self._read_incarnation(name)
        if incarnation is None:
            return False
        if not incarnation:
            raise NotFound(
                f"session {name!r} exists but carries no {INCARNATION_OPTION}: shellbox will "
                "not kill a session it cannot prove it owns",
                session=name,
            )
        result = self._run("kill-session", "-t", target(name))
        if result.rc != 0:
            # Killing the last session exits the server, so a concurrent kill's loser sees
            # `no server running` rather than `can't find session` (M12). Both mean "gone".
            if classify_stderr(result.stderr) in {"not_found", NO_SERVER}:
                return False
            raise tmux_failure(result.stderr, session=name, context="kill-session failed")
        return True

    def exists(self, name: str) -> bool:
        """``has-session -t '=<name>:'``. Existence only -- NOT liveness (see ``read``)."""
        naming.validate_session_name(name)
        result = self._run("has-session", "-t", target(name))
        if result.rc == 0:
            return True
        if classify_stderr(result.stderr) in {"not_found", NO_SERVER}:
            return False
        raise tmux_failure(result.stderr, session=name, context="has-session failed")


def _as_int(value: str, default: int) -> int:
    """Parse a tmux numeric format field, defaulting rather than raising.

    A malformed numeric field is an inventory row that is slightly wrong; raising here
    would make one odd session take down a whole ``shell_list``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
