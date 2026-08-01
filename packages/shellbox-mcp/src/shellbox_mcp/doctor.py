"""``shellbox-mcp doctor`` — why this host is not working, answered in one command (W8).

Every check here exists because something in this project was once diagnosed the hard way.
The socket-length check exists because a too-long path surfaces as a generic connect
failure; the config three-state check exists because a comment-only stub was once read as
"mis-provisioned" when it is the boot script's own placeholder; the
`DATABRICKS_CONFIG_FILE` check exists because that variable can make a security-relevant
reset a silent no-op.

## Levels, and why "the baked PAT is present" is not a failure

* **FAIL** — shellbox cannot do its job here. Exits non-zero.
* **WARN** — degraded, or a posture worth knowing about, but shells work.
* **OK / INFO** — reported because the value is what an operator would otherwise go
  looking for.

The distinction is deliberate, and the PAT is the case that forces it. An un-reset creator
PAT is a confused-deputy hazard (R6) — but **resetting it today would strand the sandbox**,
because the OAuth login that replaces it is Phase 3's and does not exist yet. So a present
PAT is currently the *correct* state, and reporting it as a failure would train operators
to ignore a red doctor. It is a WARN that says exactly that.

⚠️ **`doctor` never prints a credential.** It reports whether one exists, its length and
its 4-character prefix — the same discipline `probe/probe_identity.py` follows, for the
same reason: this output is exactly what someone pastes into an issue.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from shellbox_mcp import boot_templated, identity, naming
from shellbox_mcp.config import ConfigError, Settings

logger = logging.getLogger(__name__)

__all__ = ["Check", "Level", "render", "run_checks"]


class Level(Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Check:
    name: str
    level: Level
    detail: str
    remedy: str | None = None
    """What to actually do. A diagnostic that names a problem without naming the fix sends
    the reader back to the source, which is the thing `doctor` exists to avoid."""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: Level, detail: str, remedy: str | None = None) -> None:
        self.checks.append(Check(name, level, detail, remedy))

    @property
    def failed(self) -> bool:
        return any(c.level is Level.FAIL for c in self.checks)


# --------------------------------------------------------------------------------------
def run_checks(settings: Settings | None = None) -> Report:
    """Every check, in the order an operator would want them. Never raises."""
    report = Report()

    try:
        resolved = Settings.from_env() if settings is None else settings
    except ConfigError as exc:
        report.add(
            "configuration",
            Level.FAIL,
            f"{exc}",
            "Fix the named environment variable; the process will not start until you do.",
        )
        return report

    _check_identity(report, resolved)
    _check_tmux(report, resolved)
    _check_socket(report, resolved)
    _check_boot_templated(report)
    _check_config_overrides(report)
    _check_registry(report, resolved)
    return report


def _check_identity(report: Report, settings: Settings) -> None:
    """Who this host thinks it is — and ADR-8's "was this ever bootstrapped?" line.

    🔴 **Read-only, deliberately.** An earlier version called `resolve_host_id`, which
    *assigns and persists* a uuid4 when no cache exists — so merely running the diagnostic
    minted a permanent identity, in a real `$HOME`, on a machine that may never serve.
    A command you run *because* something is wrong must not change what it is inspecting.
    So this reads the cache and reports what `serve` **would** do, without doing it.
    """
    report.add("state_dir", Level.INFO, settings.state_dir)

    if settings.host_id_override:
        report.add(
            "host_id",
            Level.INFO,
            f"{settings.host_id_override} (from SHELLBOX_HOST_ID — an override, not cached)",
        )
        cached: dict[str, object] | None = None
    else:
        state, cached, _ = identity._load_cache(Path(settings.state_dir) / identity.HOST_JSON_NAME)
        if cached is not None:
            report.add("host_id", Level.OK, f"{cached['host_id']} (from the identity cache)")
        elif state is identity._CacheState.ABSENT:
            report.add(
                "host_id",
                Level.INFO,
                "not yet assigned — the first `serve` on this host will mint one and cache "
                "it. Not an error; this is what a host that has never run looks like.",
            )
        else:
            report.add(
                "host identity",
                Level.FAIL,
                f"the identity cache is {state.value} and cannot be used",
                "Inspect it by hand. Deleting it re-keys every session_id on this host, so "
                "it is not a safe first move — earlier log lines say what is wrong with it.",
            )

    report.add("host kind", Level.INFO, identity.lakebox_kind())

    sandbox_id = cached.get("sandbox_id") if cached else None
    if sandbox_id:
        report.add("sandbox_id", Level.OK, str(sandbox_id))
    elif _is_lakebox():
        # ADR-8: a sandbox cannot learn its own id, so it is injected from outside or
        # absent. Only worth flagging ON a sandbox -- a laptop legitimately has none.
        report.add(
            "sandbox_id",
            Level.WARN,
            "NULL — this sandbox has never been bootstrapped. It cannot learn its own id "
            "(the workspace API is caller-scoped with no local field to match on), so "
            "idle-autostop settings cannot be checked and the inventory cannot name this "
            "sandbox to a human.",
            "From OUTSIDE the sandbox, which knows the id:  databricks sandbox ssh <id> -- "
            "shellbox-mcp bootstrap --sandbox-id <id>   (per-boot)",
        )
    else:
        report.add("sandbox_id", Level.INFO, "none — not a Lakebox sandbox")

    owner = cached.get("owner_email") if cached else None
    if owner:
        report.add("owner_email", Level.OK, f"{owner} (from the identity cache)")
    elif settings.owner_email:
        report.add("owner_email", Level.OK, f"{settings.owner_email} (from SHELLBOX_OWNER_EMAIL)")
    else:
        report.add(
            "owner_email",
            Level.WARN,
            "unresolved — enrollment is DEFERRED, so sessions are NOT recorded in the "
            "inventory. Shell tools are unaffected.",
            "Set SHELLBOX_OWNER_EMAIL, or make a workspace credential available so "
            "enrollment can resolve the sandbox creator from it.",
        )


def _check_tmux(report: Report, settings: Settings) -> None:
    """Which tmux, and can it run at all. Version is recorded because §7 is transcribed
    from a spike on 3.6b and gated in CI on 3.4."""
    binary = shutil.which(settings.tmux_bin) or settings.tmux_bin
    if not Path(binary).exists():
        report.add(
            "tmux",
            Level.FAIL,
            f"{settings.tmux_bin!r} is not on PATH and does not exist",
            "Install tmux, or set SHELLBOX_TMUX_BIN to its path. Every tool call reports "
            "tmux_unavailable until this resolves.",
        )
        return
    try:
        result = subprocess.run(
            [binary, "-V"], capture_output=True, text=True, timeout=5, shell=False
        )
        version = result.stdout.strip() or result.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        report.add("tmux", Level.FAIL, f"{binary} could not be run: {exc}")
        return
    report.add("tmux", Level.OK, f"{version} at {binary}")


def _check_socket(report: Report, settings: Settings) -> None:
    """The socket path against the **platform** `sun_path` limit, and whether a server
    answers there. A too-long path is otherwise a generic connect failure."""
    limit = naming.sun_path_limit()
    length = len(settings.socket_path.encode())
    try:
        naming.validate_socket_path(settings.socket_path)
    except Exception as exc:  # noqa: BLE001 - reported, never raised out of doctor
        report.add(
            "tmux socket path",
            Level.FAIL,
            f"{settings.socket_path} is {length} bytes, over this platform's sun_path "
            f"limit of {limit}: {exc}",
            "Set SHELLBOX_TMUX_SOCKET (or SHELLBOX_STATE_DIR) to a shorter path.",
        )
        return
    report.add(
        "tmux socket path",
        Level.OK,
        f"{settings.socket_path} ({length}/{limit} bytes)",
    )

    # Whether a server is actually there. Not a failure: no server simply means no
    # sessions yet, which is the normal state of a fresh sandbox.
    state = boot_templated.inspect_path(settings.socket_path)
    if state.state is boot_templated.PathState.ABSENT:
        report.add("tmux server", Level.INFO, "no socket yet — no sessions have been created")
        return
    try:
        result = subprocess.run(
            [
                settings.tmux_bin,
                "-S",
                settings.socket_path,
                "list-sessions",
                "-F",
                "#{session_name}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.add("tmux server", Level.WARN, f"could not query the server: {exc}")
        return
    if result.returncode == 0:
        names = [n for n in result.stdout.split("\n") if n]
        report.add("tmux server", Level.OK, f"{len(names)} session(s): {', '.join(names) or '-'}")
    else:
        # A socket file with no server behind it is the normal state after a sandbox
        # restart: the file lives in $HOME and survives, the server process does not.
        report.add(
            "tmux server",
            Level.INFO,
            f"socket present but no server answers ({result.stderr.strip()}). Normal after "
            "a sandbox restart — the socket file survives in $HOME, the server does not.",
        )


def _is_lakebox() -> bool:
    """Whether the boot-templating claims apply at all.

    Everything about `/run/lakebox` symlinks and a "baked creator PAT" is true of a Lakebox
    and false of a laptop, where these are ordinary files holding an ordinary credential.
    Reporting a developer's own `~/.databrickscfg` as a confused-deputy hazard is how a
    diagnostic teaches people to ignore it.
    """
    return identity.lakebox_kind() == identity.KIND_LAKEBOX


def _check_boot_templated(report: Report) -> None:
    """The four paths the platform re-points every boot, plus the credential's real state."""
    if not _is_lakebox():
        report.add(
            "boot-templated files",
            Level.INFO,
            "not a Lakebox sandbox — the four /run/lakebox symlinks do not apply here",
        )
        _check_credential_state(report)
        return
    for templated in boot_templated.TEMPLATED_PATHS:
        found = boot_templated.inspect_path(templated.path)
        label = f"{templated.path}"
        if found.state is boot_templated.PathState.DANGLING:
            report.add(
                label,
                Level.WARN,
                f"DANGLING symlink → {found.target} ({templated.what}). The link exists and "
                "its target does not, so this file is not merely empty, it is absent.",
                "Expected for the OAuth token cache in a sandbox that has not logged in. "
                "For the others, restart the sandbox to re-run home setup.",
            )
        elif found.state is boot_templated.PathState.SYMLINK:
            report.add(
                label, Level.INFO, f"boot-templated symlink → {found.target} ({templated.what})"
            )
        elif found.state is boot_templated.PathState.REGULAR:
            report.add(
                label,
                Level.OK,
                f"regular file, mode {found.mode}, {found.size} bytes ({templated.what}) — "
                "replaced this boot; it reverts to a symlink at the next start",
            )
        elif found.state is boot_templated.PathState.ABSENT:
            report.add(label, Level.INFO, f"absent ({templated.what})")
        else:
            report.add(label, Level.WARN, f"unreadable: {found.error}")

    _check_credential_state(report)


def _check_credential_state(report: Report) -> None:
    """The three states of `~/.databrickscfg`, which need three different actions."""
    path = Path("~/.databrickscfg").expanduser()
    try:
        contents: str | None = path.read_text(encoding="utf-8")
    except OSError:
        contents = None

    state, explanation = boot_templated.describe_cfg(contents)
    if not _is_lakebox():
        # A token here is just this developer's credential. Report its shape and stop.
        shape = "no credential" if state != "credentialed" else "a credential is configured"
        report.add("workspace credential", Level.INFO, f"{shape} (not a Lakebox sandbox)")
        return
    if state == "placeholder":
        report.add("workspace credential", Level.WARN, explanation, "Restart the sandbox.")
    elif state == "credentialed":
        tokens = boot_templated._TOKEN_RE.findall(contents or "")
        shape = ", ".join(f"{len(t)} chars, {t[:4]}…" for t in tokens)
        # WARN, not FAIL -- see the module docstring. Resetting today strands the sandbox,
        # because the login that replaces this credential is Phase 3's.
        report.add(
            "workspace credential",
            Level.WARN,
            f"{explanation} ({shape})",
            "Any agent in this sandbox can act as the creating user. Reset it with "
            "`shellbox-mcp bootstrap --reset-pat` ONLY once an OAuth login exists to "
            "replace it — resetting now leaves the sandbox with no workspace credential "
            "after the next reboot, because the CLI's token cache is also boot-wiped.",
        )
    elif state == "reset":
        report.add("workspace credential", Level.OK, explanation)
    else:
        report.add("workspace credential", Level.INFO, explanation)


def _check_config_overrides(report: Report) -> None:
    """🔴 The variables that can make the PAT reset a silent no-op (§0.6)."""
    overrides = boot_templated.config_file_overrides()
    if not overrides:
        report.add(
            "DATABRICKS_*_FILE overrides",
            Level.OK,
            "none set — the CLI and SDK read the default ~/ paths",
        )
        return
    for var, value in overrides.items():
        report.add(
            var,
            Level.WARN,
            f"set to {value} — this OVERRIDES the default ~/ path for the CLI and the SDK. "
            "A reset that writes only ~/.databrickscfg would change nothing while the "
            "credential here stays in use.",
            "`shellbox-mcp bootstrap --reset-pat` handles this path too and verifies no "
            "credential survives, so it cannot report success while one does.",
        )


def _check_registry(report: Report, settings: Settings) -> None:
    """Reachability, and it is explicitly fine for there to be none."""
    if not settings.database_dsn:
        report.add(
            "registry",
            Level.INFO,
            "no SHELLBOX_DATABASE_URL — running with NullRegistry. Shell tools work "
            "fully; sessions are not recorded in any inventory.",
        )
        return

    from shellbox_registry.dsn import redact

    try:
        from shellbox_registry import create_registry

        registry = create_registry(settings.database_dsn)
        get_host = getattr(registry, "get_host", None)
        if get_host is not None:
            get_host("doctor-probe")
    except Exception as exc:  # noqa: BLE001 - reported, never raised out of doctor
        report.add(
            "registry",
            Level.WARN,
            f"{redact(settings.database_dsn)} is not reachable ({type(exc).__name__}: {exc})",
            "Shell tools still work — registry writes are non-fatal by design. The "
            "inventory will be stale until this resolves.",
        )
        return
    report.add("registry", Level.OK, f"reachable at {redact(settings.database_dsn)}")


# --------------------------------------------------------------------------------------
_GLYPH = {Level.OK: "ok  ", Level.INFO: "--  ", Level.WARN: "WARN", Level.FAIL: "FAIL"}


def render(report: Report) -> str:
    """The report as text. Written to **stderr** by the CLI, never stdout — a `doctor` run
    against a misconfigured server could otherwise land in a client's JSON-RPC stream."""
    width = max((len(c.name) for c in report.checks), default=0)
    lines = ["shellbox-mcp doctor", ""]
    for check in report.checks:
        lines.append(f"  [{_GLYPH[check.level]}] {check.name.ljust(width)}  {check.detail}")
        if check.remedy:
            lines.append(f"          {' ' * width}  -> {check.remedy}")
    failures = sum(1 for c in report.checks if c.level is Level.FAIL)
    warnings = sum(1 for c in report.checks if c.level is Level.WARN)
    lines += ["", f"  {failures} failed, {warnings} warning(s), {len(report.checks)} checks"]
    return "\n".join(lines) + "\n"
