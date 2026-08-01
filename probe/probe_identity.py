"""Measure a Lakebox sandbox's identity and boot-templating behaviour. See shellbox#12.

Run this INSIDE a sandbox. It emits newline-delimited JSON — one record per observation,
flushed immediately — following `drive.py`'s pattern so a partial run is still useful.

    databricks sandbox ssh <id> --profile <p> -- \
      "PROBE_SANDBOX_ID=<id> python3 /tmp/probe_identity.py"

Every claim in `.omc/plans/phase-2-completion.md` §0 and `docs/sandbox-environment.md` comes
from a record here. That is the point of committing it: r1 of that plan asserted §0 from
prose in an uncommitted file, and the JSONL it cited contained none of the observations the
conclusions rested on.

⚠️ **This script must never emit a credential value.** It runs against four files that hold
live secrets (`~/.databrickscfg`, `~/.databricks/token-cache.json`,
`~/.claude/settings.json`'s `apiKeyHelper`, `~/.codex/config.toml`). The whole premise of
enrollment (D4) is that the first of these authenticates as the sandbox creator — a
workspace admin — so its contents are precisely what must not reach a log. Secrets are
reported as **key names, lengths, and 4-character prefixes only**. Keep it that way: the
output of this script is committed to the repo.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time

SANDBOX_ID = os.environ.get("PROBE_SANDBOX_ID", "")

# The four files `/etc/lakebox/setup-home-directory.sh` symlinks into /run/lakebox. Named
# here as data because "how many are there" is itself a finding: r1 of the plan found three
# and missed the fourth, whose consequence (a boot-wiped OAuth token cache) lands on Phase 3.
TEMPLATED = (
    "~/.databrickscfg",
    "~/.codex/config.toml",
    "~/.claude/settings.json",
    "~/.databricks/token-cache.json",
)

BOOT_HOOK = "/etc/lakebox/setup-home-directory.sh"


def rec(kind, **fields):
    """Append one observation. Flushed immediately so a long run is inspectable in flight."""
    print(json.dumps({"kind": kind, "at": time.time(), **fields}), flush=True)


def run(*argv, timeout=60):
    """Run a command, capturing rc/stdout/stderr. Never raises — a failing probe lane must
    still produce a record, because "this command does not exist here" is usually the finding."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()[:500]}
    except FileNotFoundError:
        return {"rc": None, "out": "", "err": "command not found"}
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": "", "err": "timeout"}


def read(path, limit=8000):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError as exc:
        return f"<{exc.__class__.__name__}: {exc}>"


# --------------------------------------------------------------------- OQ-A: sandbox_id
def oq_a():
    """Can the sandbox learn its own `sandbox_id`? Plan §10 step 3 lives or dies here.

    Five searches, because a single empty result is weak evidence and the plan's derivation
    step 3 was *assumed* on the strength of never having looked.
    """
    env = dict(os.environ)
    rec("env_keys", keys=sorted(env), count=len(env))

    # Values only for keys that could plausibly identify the host, and never for anything
    # whose NAME suggests a secret.
    identifying = {}
    for key, value in env.items():
        if re.search(r"sandbox|lakebox|host|machine|instance|vm|gateway|databricks", key, re.I):
            secret = re.search(r"token|secret|password|key", key, re.I)
            identifying[key] = "<redacted>" if secret else value
    rec("env_identifying_keys", matches=identifying)

    # A LOGIN shell differs from the exec environment above (it sources ~/.bashrc and
    # /etc/profile.d), and an agent may well be spawned from one. Measured separately
    # because concluding "no identifying variable exists" from one of the two is not sound.
    rec("env_login_shell", run=run("bash", "-lc", "env"))
    rec("bashrc_exports", run=run("bash", "-lc", f"grep -nE 'export' {os.path.expanduser('~/.bashrc')}"))

    if SANDBOX_ID:
        rec(
            "env_contains_sandbox_id",
            sandbox_id=SANDBOX_ID,
            hits={k: v for k, v in env.items() if SANDBOX_ID in str(v)},
        )
        rec(
            "disk_contains_sandbox_id",
            run=run(
                "bash",
                "-lc",
                f"grep -rIl {SANDBOX_ID!r} /etc /run /var/lib /opt /usr/local 2>/dev/null | head -20",
                timeout=120,
            ),
        )

    rec(
        "host_naming",
        hostname=socket.gethostname(),
        fqdn=socket.getfqdn(),
        etc_hostname=read("/etc/hostname").strip(),
        proc_cmdline=read("/proc/cmdline").strip(),
        uid=os.getuid(),
        user=env.get("USER"),
        home=env.get("HOME"),
    )

    # What a sandbox CAN prove locally: that it IS a Lakebox. This is `hosts.kind`'s only
    # available predicate once the sandbox_id-derived prefix is abandoned (ADR-6/RC-2).
    rec(
        "is_lakebox_predicate",
        pid1=run("bash", "-lc", "tr '\\0' ' ' < /proc/1/cmdline"),
        etc_lakebox=run("bash", "-lc", f"ls -la {os.path.dirname(BOOT_HOOK)}"),
    )

    # PID 1 may hold the id in its own environment. Unreadable as the sandbox user, but
    # passwordless root is available (FINDINGS.md), so this is answerable rather than a gap.
    rec("pid1_environ_unprivileged", run=run("bash", "-lc", "tr '\\0' '\\n' < /proc/1/environ"))
    # `sudo -n tr ... < /proc/1/environ` does NOT work: the redirect is performed by the
    # CALLING shell, as the sandbox user, so it fails with EACCES before sudo runs at all.
    # `sudo` must be the process that opens the file. This cost one probe round.
    rec("pid1_environ_root", run=run("bash", "-lc", "sudo -n cat /proc/1/environ | tr '\\0' '\\n'"))

    rec("cli_sandbox_list", run=run("databricks", "sandbox", "list", "-o", "json"))
    rec("cli_version", run=run("databricks", "--version"))

    # ⚠️ THE TRAP. ~/.databricks/sandbox.json is a REGULAR file in persistent $HOME and does
    # contain this sandbox's id -- but it is the CLI's own client-side cache of the
    # CALLER-SCOPED list, written by whoever last ran `databricks sandbox list`, and it holds
    # the right id only when the caller owns exactly one sandbox. Its mtime versus the boot
    # time is what distinguishes "the platform provisioned this" from "we just created it by
    # probing". Recorded WITH provenance so a later reader cannot mistake it for an answer.
    cache = os.path.expanduser("~/.databricks/sandbox.json")
    rec(
        "sandbox_json_trap",
        exists=os.path.exists(cache),
        contents=read(cache, 2000),
        mtime=run("bash", "-lc", f"stat -c %y {cache} 2>&1"),
        boot_time=run("bash", "-lc", "uptime -s 2>&1"),
        referenced_by_boot_hook=run("bash", "-lc", f"grep -c 'sandbox.json' {BOOT_HOOK} 2>&1"),
    )


# ------------------------------------------------- machine-id provenance (plan §0.2)
def machine_id_provenance():
    """Is `/etc/machine-id` per-sandbox, or baked into the image?

    Load-bearing because the abandoned derivation ladder's last resort was
    `unknown:<machine-id>`: if it is image-baked, that is not a weak host id, it is ONE
    `hosts` row shared by every sandbox in the fleet. An mtime at the image build date, on a
    path served from a read-only overlay lower layer, is the signature of image-baked.
    """
    rec(
        "machine_id",
        value=read("/etc/machine-id").strip(),
        stat=run("bash", "-lc", "stat -c 'mtime=%y size=%s links=%h' /etc/machine-id"),
        # A per-boot value, for contrast: this one DOES change, which is what makes it
        # useless as a host id and useful as a "new boot?" signal.
        boot_id=read("/proc/sys/kernel/random/boot_id").strip(),
    )
    rec(
        "mounts",
        proc_mounts=read("/proc/mounts", 4000),
        etc_mounted=run("bash", "-lc", "grep -E ' /etc | machine-id ' /proc/mounts || echo none"),
    )
    rec(
        "image_vs_boot_dates",
        run=run(
            "bash",
            "-lc",
            "stat -c '%n %y' /etc/machine-id /etc/hostname /usr/bin/tmux "
            "/run/lakebox/databrickscfg 2>&1",
        ),
    )


# ------------------------------------------------ OQ1 + the templated set (plan §0.3/§0.4)
def boot_templating():
    """Which $HOME files does the platform rewrite at boot, and how?

    Read from the platform's own script rather than inferred from mtimes. The script is the
    only thing that can answer "is the symlink re-pointed unconditionally?" -- and it is,
    via `ln -sfn`, which is why the reset is per-boot rather than once-per-sandbox.
    """
    rec("boot_hook_stat", run=run("bash", "-lc", f"ls -la {BOOT_HOOK} 2>&1"))
    rec("boot_hook_source", contents=read(BOOT_HOOK))
    # The marker holds a boot id, which is the mechanism behind "once per boot".
    marker = os.path.expanduser("~/.home-setup-complete")
    rec(
        "boot_marker",
        contents=read(marker, 200).strip(),
        current_boot_id=read("/proc/sys/kernel/random/boot_id").strip(),
        lock=run("bash", "-lc", "ls -la /run/lakebox-home-setup.lock 2>&1"),
    )
    rec("run_lakebox_contents", run=run("bash", "-lc", "ls -la /run/lakebox/ 2>&1"))

    for path in TEMPLATED:
        expanded = os.path.expanduser(path)
        try:
            is_link = os.path.islink(expanded)
            target = os.readlink(expanded) if is_link else None
            mode = oct(os.lstat(expanded).st_mode & 0o777)
        except OSError as exc:
            rec("templated_file", path=path, exists=False, error=str(exc))
            continue
        rec(
            "templated_file",
            path=path,
            exists=True,
            is_symlink=is_link,
            symlink_target=target,
            realpath=os.path.realpath(expanded),
            link_mode=mode,
            # A symlink can exist while its TARGET does not. Measured because it is the
            # actual state of ~/.databricks/token-cache.json: the boot hook writes a
            # placeholder for it, yet the target is absent, so the OAuth token cache is not
            # merely emptied at boot -- right now it does not exist. `os.path.exists`
            # follows the link, which is exactly the question being asked.
            target_exists=os.path.exists(expanded),
            dangling=is_link and not os.path.exists(expanded),
            # Whether the PARENT is itself templated decides if writing a regular file in
            # its place can persist at all (ADR-7's premise for its non-cfg callers).
            parent=run("bash", "-lc", f"ls -ld {os.path.dirname(expanded)} 2>&1"),
        )

    # ~/.claude.json is deliberately NOT in TEMPLATED: it is absent from the boot hook and is
    # a real file holding the harness's own state, so registration there is durable. Asserted
    # rather than assumed, because it is the headline instruction in docs/registration.md.
    claude_json = os.path.expanduser("~/.claude.json")
    rec(
        "claude_json",
        stat=run("bash", "-lc", f"ls -la {claude_json} 2>&1"),
        in_boot_hook=run("bash", "-lc", f"grep -c 'claude.json' {BOOT_HOOK} 2>&1"),
    )


# --------------------------------------------------- credential + template SHAPES only
def credential_shapes():
    """Shape of every secret-bearing file. **Key names and lengths only, never values.**

    The Codex and Claude templates carry keys the harness needs (`model_provider(s)`,
    `apiKeyHelper`), which is why the boot-templated writer must MERGE rather than replace.
    That is measured here rather than argued.
    """
    cfg = read(os.path.expanduser("~/.databrickscfg"))
    profiles = re.findall(r"^\s*\[([^\]]+)\]", cfg, re.M)
    tokens = re.findall(r"^\s*token\s*=\s*(\S+)", cfg, re.M)
    lines = cfg.splitlines()
    rec(
        "databrickscfg_shape",
        profiles=profiles,
        hosts=re.findall(r"^\s*host\s*=\s*(\S+)", cfg, re.M),
        token_count=len(tokens),
        token_lengths=[len(t) for t in tokens],
        token_prefixes=[t[:4] for t in tokens],
        size_bytes=len(cfg),
        # FINDINGS.md recorded a "degenerate 61-byte comment-only stub" as a mystery. It is
        # this script's own placeholder, so the real diagnosis is "home setup ran and
        # credential provisioning never landed on top of it" -- which `doctor` must say.
        zero_profiles=not profiles,
        comment_only=bool(cfg.strip())
        and all(not ln.strip() or ln.strip().startswith((";", "#")) for ln in lines),
    )

    # Per file, not one script over all of them: a single missing file must not cost the
    # shapes of the others. The first version of this lane crashed wholesale on the absent
    # token cache and reported nothing at all, hiding three good measurements behind one
    # FileNotFoundError -- and the absence was itself the most interesting finding.
    rec(
        "template_shapes",
        run=run(
            "python3",
            "-c",
            "import json,tomllib,os\n"
            "out={}\n"
            "def shape(path, loader):\n"
            "    if not os.path.exists(path):\n"
            "        return {'exists': False}\n"
            "    try:\n"
            "        return {'exists': True, 'keys': sorted(loader(path))}\n"
            "    except Exception as exc:\n"
            "        return {'exists': True, 'error': f'{type(exc).__name__}: {exc}'}\n"
            "j=lambda p: json.load(open(p))\n"
            "t=lambda p: tomllib.load(open(p,'rb'))\n"
            "out['claude_settings']=shape('/run/lakebox/claude-settings.json', j)\n"
            "out['codex_config']=shape('/run/lakebox/codex-config.toml', t)\n"
            "out['token_cache']=shape('/run/lakebox/token-cache.json', j)\n"
            "print(json.dumps(out))",
        ),
    )


# ------------------------------------------------------------------- D4: identity
def identity():
    """D4's premise: does the ambient credential resolve the *creating* user?"""
    rec(
        "sdk_current_user",
        run=run(
            "python3",
            "-c",
            "import json\nfrom databricks.sdk import WorkspaceClient\n"
            "u=WorkspaceClient().current_user.me()\n"
            "print(json.dumps({'user_name':u.user_name,'id':u.id,"
            "'groups':[g.display for g in (u.groups or [])]}))",
            timeout=120,
        ),
    )
    rec("cli_current_user", run=run("databricks", "current-user", "me", "-o", "json"))


# --------------------------------------------------------- R3: is there ANY boot hook?
def boot_hooks():
    """R3 claims sessions cannot survive a sandbox restart because nothing we control runs at
    boot. Measured rather than asserted -- the plan previously sourced this to the wrong place."""
    rec(
        "boot_hook_survey",
        systemd_system=run("bash", "-lc", "sudo -n systemctl list-units --type=service --no-pager"),
        systemd_user=run("bash", "-lc", "systemctl --user list-units --no-pager"),
        crontab=run("bash", "-lc", "crontab -l"),
        rc_local=run("bash", "-lc", "ls -la /etc/rc.local"),
        sudo_n=run("bash", "-lc", "sudo -n true && echo 'passwordless root'"),
    )


# ------------------------------------------------------------------------- riders
def riders():
    rec("tmux", version=run("tmux", "-V"), which=run("bash", "-lc", "command -v tmux"))
    rec("term", TERM=os.environ.get("TERM"), isatty=sys.stdout.isatty())
    # OQ-E: `default-terminal` was chosen without measuring the image's terminfo database.
    rec(
        "terminfo",
        run=run(
            "bash",
            "-lc",
            "for t in tmux-256color screen-256color xterm-256color; do "
            'infocmp $t >/dev/null 2>&1 && echo "$t present" || echo "$t absent"; done',
        ),
    )
    rec("python", version=sys.version.split()[0])
    rec("resources", run=run("bash", "-lc", "free -m | head -2; nproc"))
    rec("state_dir", run=run("bash", "-lc", "ls -la ~/.shellbox 2>&1"))


if __name__ == "__main__":
    rec("probe_start", sandbox_id_hint=SANDBOX_ID, argv=sys.argv)
    for lane in (
        oq_a,
        machine_id_provenance,
        boot_templating,
        credential_shapes,
        identity,
        boot_hooks,
        riders,
    ):
        try:
            lane()
        except Exception as exc:  # one failing lane must not cost the others
            rec("lane_error", lane=lane.__name__, error=f"{exc.__class__.__name__}: {exc}")
    rec("probe_done")
