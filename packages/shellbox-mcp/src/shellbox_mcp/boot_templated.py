"""Writing to `$HOME` files the platform re-templates at every boot (W8, ADR-7).

A Lakebox sandbox replaces four `$HOME` files on **every** boot. Read from the platform's
own `/etc/lakebox/setup-home-directory.sh` rather than inferred
(`docs/sandbox-environment.md` §3):

```sh
link() { ... ln -sfn "$target" "$linkpath"; }   # UNCONDITIONAL, every boot
```

| Symlink | Target under `/run/lakebox/` |
|---|---|
| `~/.databrickscfg` | the workspace credential |
| `~/.codex/config.toml` | Codex's model config |
| `~/.claude/settings.json` | Claude Code's settings |
| `~/.databricks/token-cache.json` | the CLI's OAuth token cache |

Three consequences, each of which this module exists to handle:

1. **Never write *through* the symlink.** The target lives in `/run`, which is wiped
   between boots, so a write there is gone at the next start. The symlink itself must be
   unlinked and a regular file written in its place.
2. **Every such write is per-boot, not once-per-sandbox.** `ln -sfn` is unconditional, so
   whatever we write is replaced by a symlink again at the next boot. Callers document
   their operation as per-boot; they do not get to be idempotent across restarts.
3. **Merge, do not replace.** Two of the four templates carry keys the *harness* needs —
   `apiKeyHelper` (how Claude Code authenticates) and `model_provider`/`model_providers`
   (how Codex reaches its model). Overwriting them wholesale breaks the agent that was
   going to use shellbox. So the contract is:

   ```
   read the symlink TARGET's contents -> unlink the SYMLINK -> write merge(prior) as 0600
   ```

   Wholesale replacement is expressed as a merge function that ignores its input, so both
   callers share one code path and the dangerous one is not the default.

⚠️ **`~/.claude.json` is deliberately NOT in this module.** It is a real file in persistent
`$HOME`, absent from the boot script, holding Claude Code's own state (`projects`, existing
`mcpServers`). Routing it through a symlink-aware writer would be pointless at best and
destructive at worst.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "TEMPLATED_PATHS",
    "Inspection",
    "PathState",
    "Replacement",
    "TemplatedPath",
    "codex_mcp_registration",
    "config_file_overrides",
    "credential_less_cfg",
    "cfg_carries_a_token",
    "describe_cfg",
    "inspect_path",
    "replace_templated",
]

RUN_LAKEBOX = "/run/lakebox"


@dataclass(frozen=True, slots=True)
class TemplatedPath:
    """One `$HOME` path the boot script re-points, and what it is for."""

    path: str
    target: str
    what: str


# The complete set, from the boot script's own comment. An earlier plan revision listed
# three and missed the token cache -- whose absence turned out to be the finding that
# lands on Phase 3, because a PAT-reset sandbox then has no credential at all after a
# reboot. Keep this list matched to the script.
TEMPLATED_PATHS: tuple[TemplatedPath, ...] = (
    TemplatedPath("~/.databrickscfg", f"{RUN_LAKEBOX}/databrickscfg", "workspace credential"),
    TemplatedPath("~/.codex/config.toml", f"{RUN_LAKEBOX}/codex-config.toml", "Codex config"),
    TemplatedPath(
        "~/.claude/settings.json", f"{RUN_LAKEBOX}/claude-settings.json", "Claude Code settings"
    ),
    TemplatedPath(
        "~/.databricks/token-cache.json", f"{RUN_LAKEBOX}/token-cache.json", "CLI OAuth token cache"
    ),
)


class PathState(Enum):
    """What is actually at a templated path. The distinctions all drive different action."""

    ABSENT = "absent"
    REGULAR = "regular"
    """A real file. Either we already replaced it this boot, or this is not a sandbox."""
    SYMLINK = "symlink"
    """Boot-templated and intact: the platform's file, which a write must not follow."""
    DANGLING = "dangling"
    """A symlink whose target does not exist. Measured on `~/.databricks/token-cache.json`
    -- so the OAuth cache is not merely emptied at boot, it is absent."""
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class Inspection:
    path: Path
    state: PathState
    target: str | None = None
    mode: str | None = None
    size: int | None = None
    error: str | None = None

    @property
    def is_boot_templated(self) -> bool:
        return self.state in (PathState.SYMLINK, PathState.DANGLING)


@dataclass(frozen=True, slots=True)
class Replacement:
    """What `replace_templated` did. Returned rather than logged so `doctor` can report it."""

    path: Path
    before: PathState
    changed: bool
    """False when the file already held exactly the desired content -- which is what makes
    running a bootstrap twice in one boot a no-op."""
    unlinked_symlink: bool
    target_preserved: bool
    """True when a symlink target existed and was left byte-unchanged. The assertion that
    the write did not follow the link."""


def inspect_path(path: str | Path) -> Inspection:
    """Classify a path without following it. Never raises."""
    resolved = Path(path).expanduser()
    try:
        is_link = resolved.is_symlink()
        if is_link:
            target = os.readlink(resolved)
            state = PathState.SYMLINK if resolved.exists() else PathState.DANGLING
            size = resolved.stat().st_size if state is PathState.SYMLINK else None
            return Inspection(resolved, state, target=target, mode=_mode(resolved), size=size)
        if not resolved.exists():
            return Inspection(resolved, PathState.ABSENT)
        return Inspection(
            resolved,
            PathState.REGULAR,
            mode=_mode(resolved),
            size=resolved.stat().st_size,
        )
    except OSError as exc:
        return Inspection(resolved, PathState.UNREADABLE, error=str(exc))


def _mode(path: Path) -> str:
    return oct(path.lstat().st_mode & 0o777)


MergeFn = Callable[[str | None], str]
"""Given the prior contents (``None`` when there were none), return what to write."""


def replace_templated(path: str | Path, merge: MergeFn, *, mode: int = 0o600) -> Replacement:
    """Replace a boot-templated path with a regular file holding ``merge(prior)``.

    The ordering is the whole contract, and each step is load-bearing:

    1. **Read the target's contents first.** They are the input to the merge, and once the
       symlink is unlinked they are no longer reachable by this path.
    2. **Unlink the symlink itself**, never `open(path, "w")` — that would follow the link
       and write into `/run`, which is wiped at the next boot, so the change would appear
       to work and silently vanish.
    3. **Write a regular file at 0600**, mode set at `open()` rather than by a later chmod
       so it is never briefly world-readable.
    """
    resolved = Path(path).expanduser()
    before = inspect_path(resolved)

    prior: str | None = None
    if before.state in (PathState.SYMLINK, PathState.REGULAR):
        try:
            prior = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read %s before replacing it (%s)", resolved, exc)

    target_bytes: bytes | None = None
    if before.state is PathState.SYMLINK and before.target:
        # Snapshot the target so a caller can prove the write did not follow the link.
        try:
            target_bytes = Path(before.target).read_bytes()
        except OSError:
            target_bytes = None

    desired = merge(prior)
    if before.state is PathState.REGULAR and prior == desired:
        # Already exactly right. Not merely "already a regular file" -- a bootstrap that
        # ran, then had its content changed, must still be corrected.
        return Replacement(resolved, before.state, False, False, True)

    unlinked = False
    if before.is_boot_templated:
        resolved.unlink()
        unlinked = True

    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(desired)

    target_preserved = True
    if target_bytes is not None and before.target:
        try:
            target_preserved = Path(before.target).read_bytes() == target_bytes
        except OSError:
            target_preserved = False
        if not target_preserved:
            # The write followed the symlink. Loud, because the symptom otherwise is
            # "the reset stopped working after the next reboot" months later.
            logger.critical(
                "writing %s MODIFIED its former symlink target %s — the write followed the "
                "link instead of replacing it, so it lands in /run and is wiped at the next "
                "boot",
                resolved,
                before.target,
            )

    logger.info(
        "replaced %s (was %s) with a regular %s file", resolved, before.state.value, oct(mode)
    )
    return Replacement(resolved, before.state, True, unlinked, target_preserved)


# --------------------------------------------------------------------------------------
# Merge functions
# --------------------------------------------------------------------------------------
_HOST_RE = re.compile(r"^\s*host\s*=\s*(\S+)\s*$", re.M)
_TOKEN_RE = re.compile(r"^\s*token\s*=\s*(\S+)\s*$", re.M)
_PROFILE_RE = re.compile(r"^\s*\[([^\]]+)\]", re.M)


def credential_less_cfg(prior: str | None) -> str:
    """A `~/.databrickscfg` with the host preserved and **no credential**.

    Not a wholesale overwrite: the workspace `host` is carried over, because losing it
    turns every later CLI invocation into "which workspace?" and the operator has no
    obvious way to recover it from inside the sandbox.
    """
    host = None
    if prior:
        match = _HOST_RE.search(prior)
        host = match.group(1) if match else None

    lines = [
        "; shellbox reset this file: the sandbox's baked creator PAT was removed.",
        "; This is a PER-BOOT operation -- /etc/lakebox/setup-home-directory.sh re-points",
        "; this path at /run/lakebox/databrickscfg on every start, restoring the PAT.",
        "[DEFAULT]",
    ]
    if host:
        lines.append(f"host = {host}")
    return "\n".join(lines) + "\n"


def cfg_carries_a_token(contents: str | None) -> bool:
    """Whether a config body still holds a credential. The reset's success criterion."""
    return bool(contents) and bool(_TOKEN_RE.search(contents or ""))


def describe_cfg(contents: str | None) -> tuple[str, str]:
    """``(state, human explanation)`` for a `~/.databrickscfg` body.

    Three states, not two — the distinction matters because they need opposite actions and
    an earlier diagnosis conflated the first with "mis-provisioned, restart it":

    * **placeholder** — the boot script's own `write_placeholder` output. Home setup ran and
      credential provisioning never landed on top of it.
    * **credentialed** — a real PAT is present.
    * **reset** — a credential-less `[DEFAULT]`, i.e. the bootstrap has run this boot.
    """
    if contents is None:
        return "absent", "no config file at all"
    profiles = _PROFILE_RE.findall(contents)
    if not profiles:
        return (
            "placeholder",
            "zero profiles — this is the boot script's placeholder, so home setup ran but "
            "credential provisioning never landed on top of it. Restart the sandbox; a "
            "restart has been measured to repair it.",
        )
    if cfg_carries_a_token(contents):
        return (
            "credentialed",
            "a token is present — the sandbox's baked creator PAT. `shellbox-mcp bootstrap "
            "--reset-pat` has NOT run since the last boot (the reset is per-boot).",
        )
    return "reset", f"credential-less, profiles {profiles} — the PAT reset has run this boot"


def codex_mcp_registration(
    command: str = "shellbox-mcp",
    *,
    server_id: str = "shellbox",
) -> MergeFn:
    """A merge that **appends** shellbox's `[mcp_servers.<id>]` to Codex's config.

    Appends the section textually rather than round-tripping through a TOML parser, and
    that is a deliberate choice with two reasons:

    * **The template must survive byte-for-byte.** It carries `model_provider` and
      `model_providers`, which are how Codex reaches its model. A parse-and-rewrite would
      preserve the *values* but reformat the file and drop every comment — and the
      placeholder this replaces is itself nothing but a comment.
    * **No new dependency.** `tomllib` reads but does not write; the alternative is adding
      a TOML writer to serialise something we can express in four lines.

    `tomllib` is still used to *decide*: parsing tells us whether the section already
    exists, so running twice appends nothing.
    """

    def merge(prior: str | None) -> str:
        body = prior or ""
        if _codex_has_server(body, server_id):
            return body

        section = (
            f"\n# Added by shellbox. `args` is empty on purpose: every setting comes from the\n"
            f"# environment, so this registration works in harnesses that cannot pass flags.\n"
            f"[mcp_servers.{server_id}]\n"
            f'command = "{command}"\n'
            f"args = []\n"
        )
        if body and not body.endswith("\n"):
            body += "\n"
        return body + section

    return merge


def _codex_has_server(body: str, server_id: str) -> bool:
    """Whether `[mcp_servers.<id>]` is already present, by parsing rather than by grep."""
    if not body.strip():
        return False
    try:
        parsed = tomllib.loads(body)
    except tomllib.TOMLDecodeError:
        # Unparseable: fall back to a textual check rather than appending blindly and
        # making a broken file worse.
        return f"[mcp_servers.{server_id}]" in body
    servers = parsed.get("mcp_servers")
    return isinstance(servers, dict) and server_id in servers


# --------------------------------------------------------------------------------------
# §0.6 -- the environment overrides that can make a reset a silent no-op
# --------------------------------------------------------------------------------------
CONFIG_FILE_VAR = "DATABRICKS_CONFIG_FILE"
TOKEN_CACHE_VAR = "DATABRICKS_TOKEN_CACHE_FILE"


def config_file_overrides(env: dict[str, str] | None = None) -> dict[str, str]:
    """Any `DATABRICKS_*_FILE` override in effect, as `{var: path}`.

    🔴 **This is why the PAT reset is not simply "write `~/.databrickscfg`".** PID 1 in a
    Lakebox exports both of these, pointing at `/run/lakebox/...`, and they **override the
    `~/` defaults** for the CLI and the SDK alike. Measured: an sshd-spawned shell does not
    inherit them, but `ttyd` — which also runs in the image — carries both, so an agent
    started that way may.

    Where they are inherited, writing a credential-less `~/.databrickscfg` changes nothing:
    the baked PAT at the overridden path stays in use. That is the worst available shape
    for a security-relevant operation — **it reports success and has no effect** — so the
    reset consults this and refuses to claim success while any reachable config still
    carries a token.
    """
    source = os.environ if env is None else env
    return {var: source[var] for var in (CONFIG_FILE_VAR, TOKEN_CACHE_VAR) if source.get(var)}


class ResetIncomplete(Exception):
    """The PAT reset ran and a credential is still reachable. Never a warning.

    Separate from a write failure on purpose: a write that fails is obvious, while a write
    that *succeeds at the wrong path* looks exactly like success. This is the exception for
    the second case.
    """


@dataclass(frozen=True, slots=True)
class ResetOutcome:
    replacements: tuple[Replacement, ...]
    paths_checked: tuple[Path, ...]
    overrides: dict[str, str]

    @property
    def changed(self) -> bool:
        return any(r.changed for r in self.replacements)


def reset_pat(env: dict[str, str] | None = None) -> ResetOutcome:
    """Remove the sandbox's baked creator PAT from **every config the SDK would read**.

    🔴 The verification at the end is the point of this function. Writing
    `~/.databrickscfg` is the easy part; what makes a reset *true* is that no reachable
    config still carries a token afterwards. With `DATABRICKS_CONFIG_FILE` set — which PID 1
    and `ttyd` both export in a Lakebox — writing only `~/.databrickscfg` leaves the baked
    PAT in use and reports success.

    So: reset `~/.databrickscfg` **and** any overridden path, then re-read all of them and
    raise if a credential survives. Handling the variables unconditionally means this is
    correct whether or not the process inherited them, which matters because whether
    `login` scrubs them is a util-linux detail an image bump could change silently.

    ⚠️ **Per-boot.** `ln -sfn` re-points these paths at every start, so this must run again
    after every restart. `doctor` reports when it has not.
    """
    source = dict(os.environ if env is None else env)
    overrides = config_file_overrides(source)

    targets: list[Path] = [Path("~/.databrickscfg").expanduser()]
    if CONFIG_FILE_VAR in overrides:
        override_path = Path(overrides[CONFIG_FILE_VAR]).expanduser()
        if override_path not in targets:
            logger.warning(
                "%s=%s overrides the default config path, so resetting only "
                "~/.databrickscfg would leave the baked PAT in use. Resetting both.",
                CONFIG_FILE_VAR,
                override_path,
            )
            targets.append(override_path)

    replacements = []
    for target in targets:
        if inspect_path(target).state is PathState.ABSENT:
            logger.info("no config at %s; nothing to reset there", target)
            continue
        try:
            replacements.append(replace_templated(target, credential_less_cfg))
        except OSError as exc:
            # Attempt every path, then let the verification below decide. Aborting here
            # would leave the *other* paths unreset AND report the failure as a write
            # error, when the thing the caller needs to know is which credentials are
            # still live. A read-only mount is the realistic cause.
            logger.error("could not reset %s: %s", target, exc)

    # The verification. Re-read from disk rather than trusting what was just written.
    still_credentialed = []
    for target in targets:
        try:
            if cfg_carries_a_token(target.read_text(encoding="utf-8")):
                still_credentialed.append(str(target))
        except OSError:
            continue
    if still_credentialed:
        raise ResetIncomplete(
            "the PAT reset did NOT take effect: a credential is still present at "
            f"{', '.join(still_credentialed)}. The sandbox's baked creator PAT remains "
            "usable by any agent in this sandbox."
        )

    return ResetOutcome(tuple(replacements), tuple(targets), overrides)
