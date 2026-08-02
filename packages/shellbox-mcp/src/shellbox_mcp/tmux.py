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
import re
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
    UnencodableText,
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
    "client_env",
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

# Numeric/bounded formats, safe to read as a TAB group. Paths stay OUT of this group because
# they are user-controlled and can contain a TAB or LF (§7.4).
#
# @shellbox_incarnation is the one user option read here, which the previous version of this
# comment said should never happen. It is admissible only because the value is shape-checked on
# read-back by `_own_incarnation`: a value carrying a TAB or LF cannot masquerade as valid, it
# reads as ABSENT. Without that check this group would be exactly the hazard the comment warned
# about, so the two changes belong together and neither is safe alone.
_READ_FIELDS = (
    "#{window_width}",
    "#{window_height}",
    "#{pane_dead}",
    "#{history_size}",
    "#{history_limit}",
    # LAST, deliberately: it is the only field here that can legitimately be EMPTY (an
    # unstamped or mid-create session), and `_display_numeric` counts fields from the raw
    # line, so a trailing empty field still counts. Putting it earlier would be fine for the
    # count but would place a possibly-empty value between two always-present ones, which is
    # the shape that made `.strip()` destructive in the first place (F10).
    "#{@shellbox_incarnation}",
)

INCARNATION_OPTION = "@shellbox_incarnation"

# shellbox only ever writes a uuid4 here, so the read-back is shape-checked rather than merely
# tested for emptiness. That is strictly stronger, and it closes an edge the emptiness test
# could not:
#
# Both display read-backs take `stdout_raw.split("\n", 1)[0]`, so an incarnation containing an
# LF truncates to its FIRST LINE. A value of "aaa\nbbb" therefore read back as "aaa" --
# non-empty -- so `if not incarnation` was False and send/read/resize/kill all proceeded on a
# session shellbox could not prove it owned. A TAB gave a related mess.
#
# This is not a privilege escalation: writing the option needs `tmux set-option` on the shared
# server, and anyone with that can `capture-pane`/`send-keys` directly. What it damages is the
# thing §9.1 says the incarnation actually buys -- post-hoc misdelivery DETECTION. A caller
# comparing a truncated prefix will find two distinct incarnations equal, which is the
# detection mechanism failing silently.
_INCARNATION_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def _own_incarnation(raw: str) -> str:
    """A malformed incarnation is reported as ABSENT, i.e. ``""``.

    Returning "" rather than raising is deliberate: every caller already distinguishes
    "missing" from "present", and "shellbox cannot prove it owns this session" is exactly what
    absent means. So this strengthens the existing checks without moving any of them.
    """
    return raw if _INCARNATION_RE.match(raw) else ""


CWD_OPTION = "@shellbox_cwd"

# The host identity, stamped on every session shellbox creates so that a host which loses
# `$HOME/.shellbox/host.json` while sessions are still running can re-adopt its real identity
# instead of re-keying every `session_id` (see `identity.py`).
#
# WARNING: Deliberately NOT a ninth `LIST_FIELDS` entry, and this is a decision, not an oversight.
# `LIST_FIELDS` is exactly 8 with `_LIST_MAXSPLIT = FIELD_COUNT` and a long comment on why
# maxsplit must be one MORE than a well-formed record needs; `_READ_FIELDS` separately
# documents why the single possibly-empty field is last; and shipped tests assert "`-F` yields
# 8 raw fields". A ninth option would break a thrice-reviewed invariant, and would put a
# SECOND possibly-empty field into a format whose whole safety argument is that there is one.
# It is read through `read_host_stamp` instead: one extra subprocess against ADR-5's "zero
# extra subprocesses", paid only on the rare cold path where the identity cache is missing.
HOST_ID_OPTION = "@shellbox_host_id"


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
    # CRITICAL: FORCED, and the whole 8-field record depends on it. Measured in BOTH lanes: when the
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


def client_env(config: TmuxConfig) -> dict[str, str]:
    """The environment every tmux CLIENT this package spawns runs under.

    One construction, two callers: ``SubprocessRunner`` for the short-lived CLI invocations,
    and ``TmuxAdapter.attach_env`` for the long-lived attach child. An attach is a tmux client
    like any other, and two independent spellings of "the environment a tmux client needs"
    would drift -- which here means drifting on ``LC_CTYPE``, whose absence silently collapses
    every ``-F`` record into one field.

    ``TERM`` is forced because a headless host has no tty, so bash substitutes ``TERM=dumb`` --
    and ``tmux attach`` refuses a dumb terminal outright (spike F17). The inherited environment
    is reduced to an allowlist because the MCP process's environment is where the harness
    injects credentials, and a tmux server started by a client passes its environment on to
    every pane it later spawns.
    """
    env = {"TERM": config.term, "LC_CTYPE": config.locale_ctype}
    for key in config.passthrough_env:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    # LC_ALL would override LC_CTYPE, so a parent's `LC_ALL=C` would silently reinstate the
    # TAB-mangling described on TmuxConfig.locale_ctype. It is not in the pass-through
    # allowlist; this asserts that it never sneaks back in.
    assert "LC_ALL" not in env, "LC_ALL must never be passed through: it overrides LC_CTYPE"
    return env


@dataclass
class SubprocessRunner:
    """The real runner: ``subprocess.run`` with an argv LIST and ``shell=False``.

    ``TERM`` is forced for every invocation and the inherited environment is reduced to a
    handful of variables: the MCP process's environment is where the harness injects
    credentials, and a tmux server started by this client passes its environment on to
    every pane it later spawns.

    The environment itself is built by ``client_env`` so the attach client of ``attach_env``
    gets the identical one. An attach IS a tmux client, and two constructions of "the
    environment a tmux client runs under" would drift.
    """

    config: TmuxConfig
    env: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.env = client_env(self.config)

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

        WARNING: ``display-message`` returns **rc=0 for every nonexistent target** (spike F6) --
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

        A present-but-MALFORMED value collapses into the second state -- see
        ``_own_incarnation`` for why a truncated prefix would otherwise read as valid.
        """
        raw = self._display_tail(name, f"#{{{INCARNATION_OPTION}}}")
        return raw if raw is None else _own_incarnation(raw)

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
        payload = b"" if text is None else _encode_text(text, name)
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
        width, height, pane_dead, history_size, history_limit, raw_incarnation = metrics
        incarnation = _own_incarnation(raw_incarnation)

        # §6 lists not_found for send, read, resize AND kill when a session is present but
        # carries no incarnation. read was the one that did not enforce it, so the code was
        # looser than its own documented contract: a foreign session could be read while the
        # other three refused it. Enforced rather than relaxing the spec -- a read-only
        # carve-out is arguable under D6, but silently differing from the table is not.
        #
        # Checked from the field above rather than by calling _resolve_owned, which would add
        # a second tmux round-trip to every read. Same guarantee, one invocation.
        if not incarnation:
            raise NotFound(
                f"session {name!r} exists but carries no {INCARNATION_OPTION}: it is either "
                "mid-create or foreign, and shellbox will not act on a session it cannot "
                "prove it owns",
                session=name,
            )

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

        CRITICAL: Only the two exact ``no_server`` signatures yield an empty list -- ``no server
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
            # CRITICAL: tmux listed sessions and NONE of them parsed. Never return that as an empty
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

    # -- attach support (W15) ------------------------------------------------------------
    #
    # Every tmux form the transport needs lives HERE, in the package that owns `target()` and
    # `Settings.tmux_bin`, and not in `shellbox-transport`. That is decision B3 and it is
    # mechanical rather than aesthetic: the two shipped AST guards
    # (`tests/unit/test_target.py`, `tests/unit/test_no_global_window_size.py`) both reach this
    # module, so an attach argv built here is covered by them, while the same argv built in the
    # transport package would have to import `shellbox_mcp.target` -- the import cycle the
    # separate package exists to prevent.
    #
    # omnigent's bridge is the counter-example and the reason the guard scope was widened:
    # `ws_bridge.py:492` passes `-t tmux_target` UNANCHORED, and its `_tmux_session_alive`
    # helper does it a second time while spawning a bare `"tmux"` rather than a resolved path.
    # Both are exactly what `target.py` and `ADR-1` exist to forbid.

    def attach_argv(self, name: str) -> list[str]:
        """The argv for an attach client. A pure builder: no I/O, no ownership check.

        Pure so that the shipped AST guard in ``tests/unit/test_target.py`` can prove the ``-t``
        comes from ``target()`` on every branch, including the ones no test reaches.

        WARNING: This does NOT resolve ownership and does NOT freeze the window size. Call
        ``prepare_attach`` unless you have a reason not to -- attaching without the freeze
        reflows the agent's window to the viewer's size, measured (spike F16, the control row).
        """
        naming.validate_session_name(name)
        return [*self._base_argv(), "attach", "-t", target(name)]

    def attach_env(self) -> dict[str, str]:
        """The environment for an attach child. ``TERM`` describes the FAR end, not this process.

        MEASURED (spike F17): ``tmux attach`` under ``TERM=dumb`` is refused outright with
        ``open terminal failed: terminal does not support clear``, the child exits, and zero
        clients attach -- in both lanes. A headless host has no tty, so bash substitutes
        ``TERM=dumb``, which means *inheriting* the environment is exactly the failing case, and
        the renderer would then display that error message instead of the pane.

        So the forced value is load-bearing rather than hygiene, and it describes the terminal
        at the far end of the socket -- a browser running xterm.js -- not the Python process
        that forked the client.

        It is ``client_env`` unmodified, which is the point: an attach is a tmux client, and it
        needs the same reduced environment and the same forced ``LC_CTYPE`` as every other one.
        """
        return client_env(self.config)

    def freeze_window_size(self, name: str) -> None:
        """Pin this window's size so an attaching client cannot reflow the agent's pane.

        CRITICAL: **The scope is the variable, and the global form kills the server.** With
        ``set-option -g window-size manual`` the NEXT ``new-session`` dies with a SIGSEGV in
        ``clients_calculate_size`` -- 15/15 in both lanes (spike F1/F9). It lands on the
        *second* create, so by then other pooled agents hold sessions on that server and one
        agent's ``shell_create`` destroys all of them. The per-window ``-w`` form is 0/15.
        ``tests/unit/test_no_global_window_size.py`` enforces the distinction structurally.

        Why this is needed at all: an attach is a tmux *client*, and a client's size drives the
        window's. ``TmuxConfig.default_terminal`` is small on purpose because first attach GROWS
        the window losslessly -- but a 120x40 viewer moving an 80x24 agent window is PM3, and it
        was measured on 3.4 rather than feared (F16, the control row reflows).

        Placement is at attach time, ``before_attach``, which F16 decided: its exposure window
        measured EMPTY over 1714 samples, one call protects every later viewer including a
        second client at a different size, and it leaves the shipped create chain -- the
        composition four review rounds were spent on -- untouched. The create-time placement is
        equally safe (0/15) but freezes a path that the 1-32 agents who never open a browser
        would pay for.
        """
        naming.validate_session_name(name)
        self._resolve_owned(name)
        result = self._run("set-option", "-w", "-t", target(name), "window-size", "manual")
        if result.rc != 0:
            raise tmux_failure(result.stderr, session=name, context="set-option failed")

    def prepare_attach(self, name: str) -> list[str]:
        """Resolve ownership, freeze the window size, and return the argv to exec. In that order.

        The order is the whole method. Freezing after the client is live costs one real reflow
        of the agent's pane -- it reverts on its own (F16 consequence 3), but it is visible, and
        it is avoidable by doing it first.

        Raises ``NotFound`` for a session that does not exist or that carries no incarnation.
        shellbox does not attach a pty to a session it cannot prove it owns, for the same reason
        it will not kill one.
        """
        self.freeze_window_size(name)
        return self.attach_argv(name)

    def pane_dead(self, name: str) -> bool | None:
        """Whether the pane's process has exited. ``None`` means the session did not resolve.

        A thin accessor over ``_display_numeric``, and NOT a new tmux form: ``#{pane_dead}`` is
        already in ``LIST_FIELDS`` and ``_READ_FIELDS``, and ``read()`` already returns it as
        ``ReadResult.alive`` -- which this module calls the single source of truth for liveness.
        Deliberately not ``list-panes``, which would be a new verb for a value already read.

        It exists as its own accessor only because ``read()`` also performs a ``capture-pane``
        and raises ``NotFound`` on a session carrying no incarnation. A liveness probe wants
        neither: it runs on every close to answer one question, and it must be able to report
        "gone" rather than raise.

        WARNING: This is the signal that distinguishes a DETACH from a dead pane, and the two
        must not be collapsed. ``has-session`` cannot do it -- shellbox sets ``remain-on-exit
        on`` globally, so the session outlives its process by design. Measured both directions
        with a live client attached (spike F19): on detach the pane reads ``0`` and stays ``0``;
        when the process exits it reads ``1`` while ``has-session`` still returns rc=0.

        The consequence for the close code: a dead pane is terminal-gone and stops the client's
        reconnect loop; a detach must NOT be, because a detach misread as terminal-gone tears
        down the whole session. F19 also found that the attach client OUTLIVES the pane's
        process, which is what makes the distinction reportable on a socket that is still up.
        """
        naming.validate_session_name(name)
        metrics = self._display_numeric(name, ("#{pane_dead}",))
        if metrics is None:
            return None
        return metrics[0] == "1"

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

    # -- the host stamp (W7f) ------------------------------------------------------------
    #
    # Read and written through `show-options`/`set-option` rather than the `-F` group, for the
    # reason given at `HOST_ID_OPTION`. Both are deliberately NON-RAISING on a missing session
    # or a dead server: they are called from enrollment, which runs in the background and must
    # never be able to fail a tool call.

    def read_host_stamp(self, name: str) -> str | None:
        """The session's ``@shellbox_host_id``, or ``None`` if absent/unusable/unreachable.

        ``None`` collapses four cases on purpose -- no server, no session, no option, and an
        option whose value cannot have come from shellbox. Every caller wants the same answer
        for all four ("this session tells me nothing about my identity"), and the *reason* is
        already logged where it matters.

        Only shape problems that break the read-back mechanically are rejected here:
        ``show-options -v`` output is read as a single line, so a value containing an LF or TAB
        would be silently truncated and a truncated `host_id` is a DIFFERENT host_id -- which
        would then be adopted and stamped into every `session_id` on the host. Semantic
        validation (colons, which make `'<host_id>:<tmux_name>'` ambiguous) belongs to
        `identity.py`, the layer that owns what a `host_id` may be.
        """
        naming.validate_session_name(name)
        result = self._run("show-options", "-v", "-t", target(name), HOST_ID_OPTION)
        if result.rc != 0:
            return None
        # `-v` on an unset option prints an empty line rather than failing, so emptiness is
        # the normal "not stamped" answer and not an error.
        raw = result.stdout_raw.split("\n", 1)[0]
        if not raw or raw != raw.strip() or any(char.isspace() for char in raw):
            if raw:
                logger.error(
                    "ignoring %s=%r on session %r: it contains whitespace, so the value read "
                    "back cannot be trusted to be the value that was written",
                    HOST_ID_OPTION,
                    raw,
                    name,
                )
            return None
        return raw

    def stamp_host_id(self, name: str, host_id: str) -> bool:
        """Stamp ``@shellbox_host_id`` on a session. False if it could not be written.

        Idempotent by nature (``set-option`` overwrites), and never raises: a host that cannot
        stamp is a host whose identity cache is merely un-backed-up, which is a degradation and
        not a failure.
        """
        naming.validate_session_name(name)
        result = self._run("set-option", "-t", target(name), HOST_ID_OPTION, host_id)
        if result.rc != 0:
            logger.warning(
                "could not stamp %s on session %r: %s",
                HOST_ID_OPTION,
                name,
                result.stderr.strip(),
            )
            return False
        return True


def _encode_text(text: str, name: str) -> bytes:
    """Encode ``text`` to the bytes that will be pasted, or reject it before tmux is touched.

    Every other guard on this path exists because an over-long line is *silently* destroyed or
    mutated; this one exists for the same reason one layer earlier. A str carrying a lone
    surrogate (which an MCP client can send: ``json.loads('"\\ud800"')``) has no UTF-8 encoding,
    and the two ways to encode it anyway both corrupt the payload -- ``errors="replace"`` swaps
    in U+FFFD, ``"surrogatepass"`` emits bytes that are not UTF-8. So it is an error, and an
    error inside the taxonomy rather than a bare ``UnicodeEncodeError`` from a tool whose
    documented failures are all structured payloads.
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise UnencodableText(
            f"text is not encodable as UTF-8 ({exc.reason} at position {exc.start}): shellbox "
            "will not substitute or reinterpret bytes the caller did not send",
            session=name,
        ) from exc


def _as_int(value: str, default: int) -> int:
    """Parse a tmux numeric format field, defaulting rather than raising.

    A malformed numeric field is an inventory row that is slightly wrong; raising here
    would make one odd session take down a whole ``shell_list``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
