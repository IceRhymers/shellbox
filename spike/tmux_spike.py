#!/usr/bin/env python3
"""Executable spike for the shellbox tmux adapter -- Phase 2 section 7, and Phase 3's W14.

Purpose: settle B1-B4 from the iteration-2 architect review by RUNNING the command
compositions the plan prescribes, rather than reasoning about them. The plan's
measurement appendix tests fragments; every defect found so far lived in the
composition. This runs compositions.

The Phase 3 additions (`S-ATTACH`, `S-PANE-DEAD`, `S-PIPE`, `S-CLAIM`, from
`.omc/plans/phase-3-transport.md` W14) are here for the same reason and under the same
rule: a new tmux form goes into this file FIRST and into a module SECOND. They also
carry a gate -- see the block comment above `check_s_attach`.

Emits one JSON object per check to stdout (JSONL). Run under two tmux versions:

    python3 spike/tmux_spike.py                     # local (3.6b)
    docker run --rm -v "$PWD:/w" -w /w ubuntu:24.04 \
        sh -c 'apt-get update -qq && apt-get install -y -qq tmux python3 \
               && python3 spike/tmux_spike.py'      # 3.4

Section 7 of the plan should then be transcribed FROM this output.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import uuid
import warnings

TMUX = os.environ.get("SHELLBOX_TMUX_BIN") or shutil.which("tmux") or "tmux"

# tmux socket paths go in a sockaddr_un.sun_path: 104 bytes on macOS/BSD, 108 on
# Linux. A long TMPDIR silently breaks every call with "File name too long", so
# keep this short and assert it. (Plan M13.)
SOCKET_ROOT = "/tmp"

# Every failed assertion lands here; main() exits non-zero if any did. Without
# this the suite emits JSONL and returns 0 whatever happens, which cannot gate CI.
FAILURES: list[str] = []


def T(name: str) -> str:
    """The ONE safe target form (spike F3): prefix-safe for every targeting verb.

    Constructed only here so the plan's 'no bare =name anywhere' rule is
    mechanically enforceable -- see check_self().
    """
    return f"={name}:"


def expect(label: str, ok: bool, detail: str = "") -> bool:
    if not ok:
        FAILURES.append(f"{label}: {detail}" if detail else label)
    return ok


def tmux_version() -> str:
    out = subprocess.run([TMUX, "-V"], capture_output=True, text=True)
    return out.stdout.strip() or out.stderr.strip()


class Server:
    """One tmux server on its own short socket, torn down on exit."""

    def __init__(self) -> None:
        self.sock = os.path.join(SOCKET_ROOT, f"sbx{uuid.uuid4().hex[:8]}")
        assert len(self.sock) < 100, self.sock

    def run(self, *args: str, stdin: bytes | None = None):
        argv = [TMUX, "-S", self.sock, "-f", "/dev/null", *args]
        p = subprocess.run(argv, capture_output=True, input=stdin)
        raw = p.stdout.decode(errors="replace")
        return {
            "argv": " ".join(args),
            "rc": p.returncode,
            "stdout": raw.strip(),
            # NEVER strip() before counting TAB-separated fields: a session with
            # empty trailing fields (an unstamped @shellbox_incarnation) ends the
            # line in tabs, and strip() silently eats them -- turning 8 fields into
            # 6 and making a field-count check misfire. See the FIELDS finding.
            "stdout_raw": raw,
            "stderr": p.stderr.decode(errors="replace").strip(),
        }

    def kill(self) -> None:
        subprocess.run(
            [TMUX, "-S", self.sock, "kill-server"],
            capture_output=True,
        )
        try:
            os.unlink(self.sock)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.kill()
        return False


def emit(check: str, question: str, **fields) -> None:
    rec = {"check": check, "question": question, **fields}
    print(json.dumps(rec, default=str), flush=True)


def wait_for(path: str, minimum: int, timeout: float = 5.0) -> int:
    """Poll until `path` has >= minimum bytes, or timeout. Returns size seen.

    The plan (§11.1) calls a synchronization model 'required infrastructure';
    this is the smallest honest version of it. Never sleep a fixed interval.
    """
    deadline = time.time() + timeout
    size = 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size >= minimum:
            return size
        time.sleep(0.02)
    return size


# ---------------------------------------------------------------------------
# B1 -- does `window-size manual` crash the server on a subsequent new-session?
#
# The r1 plan put options BEFORE new-session and crashed. r2 concluded the
# variable was ordering and moved them after. The architect's counter-claim is
# that the variable is the OPTION, not the ordering, and that r2's form crashes
# on the *second* create -- which is worse, because by then other pooled agents
# hold sessions on that server.
# ---------------------------------------------------------------------------
def check_b1(trials: int = 15) -> None:
    # Expected second-create failure rate per variant. `global` documents the bug;
    # the other three must be clean. `per_window` is the R10 mitigation.
    variants = {
        "no_window_size_manual": [],
        "window_size_manual_separate_call": [("set-option", "-g", "window-size", "manual")],
        "window_size_manual_chained_r2_form": None,  # handled below
        "window_size_manual_PER_WINDOW": [
            ("set-option", "-w", "-t", T("a"), "window-size", "manual")
        ],
    }
    must_fail_all = {"window_size_manual_separate_call", "window_size_manual_chained_r2_form"}

    for name, opts in variants.items():
        crashes = 0
        first_err = ""
        for _ in range(trials):
            with Server() as s:
                if name == "window_size_manual_chained_r2_form":
                    # r2 §7.2's prescribed form, verbatim: create, then options
                    # chained in the same invocation.
                    s.run(
                        "new-session",
                        "-d",
                        "-s",
                        "a",
                        "-x",
                        "80",
                        "-y",
                        "24",
                        "sh",
                        ";",
                        "set-option",
                        "-g",
                        "history-limit",
                        "20000",
                        ";",
                        "set-option",
                        "-g",
                        "status",
                        "off",
                        ";",
                        "set-option",
                        "-g",
                        "remain-on-exit",
                        "on",
                        ";",
                        "set-option",
                        "-g",
                        "window-size",
                        "manual",
                    )
                else:
                    s.run("new-session", "-d", "-s", "a", "-x", "80", "-y", "24", "sh")
                    for o in opts or []:
                        s.run(*o)

                # The claim under test: the SECOND create is what dies.
                second = s.run("new-session", "-d", "-s", "b", "-x", "80", "-y", "24", "sh")
                if second["rc"] != 0:
                    crashes += 1
                    first_err = first_err or second["stderr"]

        want = trials if name in must_fail_all else 0
        ok = expect(
            f"B1[{name}]",
            crashes == want,
            f"expected {want}/{trials} second-create failures, got {crashes}",
        )
        emit(
            "B1",
            "does `window-size manual` make a subsequent new-session fail?",
            variant=name,
            trials=trials,
            second_create_failures=crashes,
            expected_failures=want,
            ok=ok,
            first_error=first_err,
        )


# ---------------------------------------------------------------------------
# B2 -- which target form does `set-option -t` accept?
# If it rejects '=name', @shellbox_incarnation is never set and the incarnation
# tests compare "" == "" and pass green.
# ---------------------------------------------------------------------------
def check_b2() -> None:
    for form in ("build", "=build", "=build:", "bui"):
        with Server() as s:
            s.run("new-session", "-d", "-s", "build", "sh")
            s.run("new-session", "-d", "-s", "envtest", "sh")
            r = s.run(
                "set-option",
                "-t",
                form,
                "@shellbox_incarnation",
                "00000000-0000-4000-8000-000000000001",
            )
            readback = s.run("list-sessions", "-F", "#{session_name}\t#{@shellbox_incarnation}")
            stored = "00000000-0000-4000-8000-000000000001" in readback["stdout"]
            # The two that matter: '=name:' must work, '=name' must not.
            ok = True
            if form == "=build:":
                ok = expect("B2[=name: must work]", r["rc"] == 0 and stored, r["stderr"])
            elif form == "=build":
                ok = expect("B2[=name must be rejected]", r["rc"] != 0, "unexpectedly accepted")
            emit(
                "B2",
                "which target form does `set-option -t` accept, and does the value stick?",
                target_form=form,
                rc=r["rc"],
                stderr=r["stderr"],
                value_stored=stored,
                ok=ok,
                list_sessions=readback["stdout"].replace("\n", " | "),
            )


# ---------------------------------------------------------------------------
# B3 -- per-verb targeting. Which verbs does the '=' anchor actually protect?
# A prefix that resolves is a cross-agent addressing bug under default-open
# access (D6): agent A reaches agent B's session by naming a prefix of it.
# ---------------------------------------------------------------------------
def check_b3() -> None:
    # (verb, extra args, needs_pane_target)
    verbs = [
        ("has-session", []),
        ("kill-session", []),
        ("resize-window", ["-x", "100", "-y", "30"]),
        ("capture-pane", ["-p"]),
        ("send-keys", ["-l", "x"]),
        ("display-message", ["-p", "#{pane_current_path}"]),
        ("set-option", ["@k", "v"]),
    ]
    for verb, extra in verbs:
        for form in ("bui", "=bui", "=build", "=build:", "=bui:"):
            with Server() as s:
                s.run("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
                s.run("new-session", "-d", "-s", "envtest", "sh")
                r = s.run(verb, "-t", form, *extra)

                # Did it touch `build` even though `build` was not named?
                after = s.run(
                    "list-sessions",
                    "-F",
                    "#{session_name}\t#{window_width}x#{window_height}\t#{@k}",
                )
                # The control: `=bui:` names a session that does not exist, so EVERY
                # targeting verb must reject it. This is the assertion that would have
                # caught `resize-window`, which `=bui` silently resolved.
                ok = True
                if form == "=bui:" and verb != "display-message":
                    ok = expect(
                        f"B3[{verb} must reject =bui:]",
                        r["rc"] != 0,
                        "nonexistent target accepted",
                    )
                elif form == "=build:":
                    ok = expect(f"B3[{verb} must accept =build:]", r["rc"] == 0, r["stderr"])
                emit(
                    "B3",
                    "does the '=' anchor stop prefix/fnmatch matching, per verb?",
                    verb=verb,
                    target_form=form,
                    rc=r["rc"],
                    stderr=r["stderr"],
                    ok=ok,
                    sessions_after=after["stdout"].replace("\n", " | "),
                )

    # `-s` on new-session is a NAME, not a target. Anchoring it is a category error.
    with Server() as s:
        r = s.run("new-session", "-d", "-s", "=build", "sh")
        listing = s.run("list-sessions", "-F", "#{session_name}")
        probe = s.run("has-session", "-t", "=build")
        emit(
            "B3",
            "what does `new-session -s '=build'` actually create?",
            verb="new-session -s",
            target_form="=build",
            rc=r["rc"],
            stderr=r["stderr"],
            created_session_names=listing["stdout"].replace("\n", " | "),
            has_session_eq_build_rc=probe["rc"],
        )


# ---------------------------------------------------------------------------
# B4 -- does `history-limit` actually reach the pane?
# A pane's history limit is fixed at creation. Setting the global AFTER
# new-session leaves every real pane at the 2000 default, while `show-options`
# reads the global and passes green.
# ---------------------------------------------------------------------------
def check_b4() -> None:
    want = "20000"

    # (a) r2's form: global set after the session exists.
    with Server() as s:
        s.run("new-session", "-d", "-s", "a", "sh")
        s.run("set-option", "-g", "history-limit", want)
        g = s.run("show-options", "-g", "history-limit")
        pane = s.run("display-message", "-p", "-t", T("a"), "#{history_limit}")
        emit(
            "B4",
            "does setting the global history-limit after new-session reach the pane?",
            variant="set_global_after_new_session_(r2_form)",
            want=want,
            global_option=g["stdout"],
            pane_history_limit=pane["stdout"],
            reaches_pane=pane["stdout"] == want,
            # This variant is EXPECTED to fail -- it documents the r2 defect.
            ok=expect(
                "B4[r2 form must NOT reach the pane]",
                pane["stdout"] != want,
                "r2's form unexpectedly worked; the defect may be version-specific",
            ),
        )

    # (b) candidate fix: a config file, so the option is set before the first
    #     session is spawned by the same server process.
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
        f.write(f"set -g history-limit {want}\n")
        conf = f.name
    try:
        s = Server()
        try:
            argv = [TMUX, "-S", s.sock, "-f", conf, "new-session", "-d", "-s", "a", "sh"]
            p = subprocess.run(argv, capture_output=True, text=True)
            pane = s.run("display-message", "-p", "-t", T("a"), "#{history_limit}")
            emit(
                "B4",
                "does a -f config file set history-limit before the first pane spawns?",
                variant="config_file_-f",
                want=want,
                rc=p.returncode,
                stderr=p.stderr.strip(),
                pane_history_limit=pane["stdout"],
                reaches_pane=pane["stdout"] == want,
                ok=expect(
                    "B4[-f config reaches pane]",
                    pane["stdout"] == want,
                    f"pane read {pane['stdout']!r}",
                ),
            )
        finally:
            s.kill()
    finally:
        os.unlink(conf)

    # (c) candidate fix: start-server, set, new-session -- all one invocation.
    with Server() as s:
        r = s.run(
            "start-server",
            ";",
            "set-option",
            "-g",
            "history-limit",
            want,
            ";",
            "new-session",
            "-d",
            "-s",
            "a",
            "sh",
        )
        pane = s.run("display-message", "-p", "-t", T("a"), "#{history_limit}")
        emit(
            "B4",
            "does start-server + set + new-session in ONE invocation reach the pane?",
            variant="chained_start-server_set_new-session",
            want=want,
            rc=r["rc"],
            stderr=r["stderr"],
            pane_history_limit=pane["stdout"],
            reaches_pane=pane["stdout"] == want,
            ok=expect(
                "B4[chained start-server reaches pane]",
                pane["stdout"] == want,
                f"pane read {pane['stdout']!r}",
            ),
        )


# ---------------------------------------------------------------------------
# H4 -- the pty line discipline silently discards over-long lines in canonical
# mode. This is the hazard that outranks the tmux-argv layer entirely, because
# it depends on the termios state of whatever the pane happens to be running.
# ---------------------------------------------------------------------------
def check_h4() -> None:
    for mode in ("canonical", "raw"):
        for length in (500, 1023, 1024, 4095, 4096, 8192):
            with Server() as s:
                out = os.path.join(tempfile.mkdtemp(prefix="sbx"), "out")
                reader = f"cat > {out}" if mode == "canonical" else f"stty -icanon; cat > {out}"
                s.run("new-session", "-d", "-s", "a", "sh", "-c", reader)
                time.sleep(0.3)  # let the reader install its termios state

                payload = (b"x" * length) + b"\n"
                buf = f"sb-{uuid.uuid4().hex[:8]}"
                lb = s.run("load-buffer", "-b", buf, "-", stdin=payload)
                pb = s.run("paste-buffer", "-d", "-b", buf, "-t", T("a"))
                got = wait_for(out, len(payload), timeout=2.0)
                lossless = got == len(payload)

                # Raw mode must ALWAYS be lossless -- that is what makes the split
                # oracle valid. Canonical mode at 8192 must ALWAYS lose data, on
                # every platform; whether it drops (macOS) or truncates (Linux)
                # differs, so only the loss itself is asserted.
                ok = True
                if mode == "raw":
                    ok = expect(
                        f"H4[raw must be lossless @{length}]",
                        lossless,
                        f"got {got}/{len(payload)}",
                    )
                elif length == 8192:
                    ok = expect(
                        "H4[canonical must lose data @8192]",
                        not lossless,
                        "canonical mode unexpectedly delivered everything",
                    )
                emit(
                    "H4",
                    "does an over-long line survive the pty line discipline?",
                    reader_mode=mode,
                    line_length=length,
                    sent_bytes=len(payload),
                    delivered_bytes=got,
                    load_buffer_rc=lb["rc"],
                    paste_buffer_rc=pb["rc"],
                    lossless=lossless,
                    ok=ok,
                )


# ---------------------------------------------------------------------------
# The composition the plan actually ships: create -> send -> read -> list ->
# resize -> kill, end to end, using ONLY forms this spike found to work.
# ---------------------------------------------------------------------------
def check_cwd_injection() -> None:
    """`@shellbox_cwd` is user-controlled and breaks the TAB format. Measured, both lanes.

    The plan excludes `#{pane_current_path}` from the format because a path may
    contain a TAB or LF, then includes `@shellbox_cwd` -- which has the identical
    property -- and asserts it rides along safely. It does not.

    The LF case is the dangerous one: it still yields 8 fields, so it PASSES a
    field-count check while the cwd is silently truncated.
    """
    base = tempfile.mkdtemp(prefix="sbxcwd")
    try:
        for kind, leaf in (("tab", "a\tb"), ("lf", "a\nb"), ("plain", "plain")):
            with Server() as s:
                d = os.path.join(base, leaf)
                os.makedirs(d, exist_ok=True)
                s.run("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
                s.run("set-option", "-t", T("build"), "@shellbox_cwd", d)
                raw = s.run("list-sessions", "-F", FIELDS_8)["stdout_raw"]
                lines = [line for line in raw.split("\n") if line]
                n = len(lines[0].split("\t")) if lines else 0
                got_cwd = lines[0].split("\t")[7] if lines and n > 7 else None

                if kind == "plain":
                    ok = expect(
                        "CWD[clean path yields 8 fields, exact]",
                        n == 8 and got_cwd == d,
                        f"{n} fields, cwd={got_cwd!r}",
                    )
                else:
                    # Assert the HAZARD is real, so the guard can never be dropped
                    # silently: either the field count breaks or the value is wrong.
                    ok = expect(
                        f"CWD[{kind} must corrupt the record]",
                        n != 8 or got_cwd != d,
                        "hazard did not reproduce; the naming.py guard may look unnecessary",
                    )
                emit(
                    "CWD",
                    "does a TAB/LF in @shellbox_cwd corrupt the -F record?",
                    cwd_kind=kind,
                    output_lines=len(lines),
                    fields_on_line1=n,
                    cwd_field=repr(got_cwd),
                    cwd_intact=got_cwd == d,
                    ok=ok,
                )
    finally:
        shutil.rmtree(base, ignore_errors=True)


# The plan's §7.2 normative create chain, VERBATIM as a single invocation.
# The earlier version of this spike ran new-session first and the globals as
# separate calls -- i.e. the form §7.3 proves leaves the pane at 2000 -- and
# never read the pane's history_limit, so it passed anyway. That let the plan
# cite this check for a composition it had not run. This is that fix.
# NORMATIVE FIELD ORDER. The plan and this suite MUST agree, and W2 makes this
# suite the oracle -- so this ordering is the one to transcribe, not the other way
# round. r3's §7.4 had `incarnation` in position 2; that ordering is unsafe,
# because a parser written to it reads `session_created` (always NON-empty) as the
# incarnation, so the "empty is never a match" rule passes and every session is
# misidentified as shellbox-owned with a bogus incarnation. Silent.
#
# The rule: the two fields that may legitimately be EMPTY go LAST.
FIELDS_8 = (
    "#{session_name}\t#{session_created}\t#{session_activity}\t"
    "#{window_width}\t#{window_height}\t#{pane_dead}\t"
    "#{@shellbox_incarnation}\t#{@shellbox_cwd}"
)


def check_chain_verbatim() -> None:
    inc = str(uuid.uuid4())
    with Server() as s:
        chain = s.run(
            "start-server",
            ";",
            "set-option",
            "-g",
            "history-limit",
            "20000",
            ";",
            "set-option",
            "-g",
            "status",
            "off",
            ";",
            "set-option",
            "-g",
            "default-terminal",
            "screen-256color",
            ";",
            "set-option",
            "-g",
            "remain-on-exit",
            "on",
            ";",
            "new-session",
            "-d",
            "-s",
            "build",
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            "/tmp",
            "-e",
            "FOO=a\nb",
            "-e",
            "BAR=x;y",
            "sh",
            ";",
            "set-option",
            "-t",
            T("build"),
            "@shellbox_incarnation",
            inc,
            ";",
            "set-option",
            "-t",
            T("build"),
            "@shellbox_cwd",
            "/tmp",
        )
        pane_hist = s.run("display-message", "-p", "-t", T("build"), "#{history_limit}")
        dterm = s.run("show-options", "-g", "default-terminal")
        listing = s.run("list-sessions", "-F", FIELDS_8)
        # The F1 regression: a SECOND create on the same server must succeed.
        second = s.run("new-session", "-d", "-s", "other", "-x", "80", "-y", "24", "sh")

        fields = listing["stdout_raw"].split("\n")[0].split("\t") if listing["stdout"] else []
        oks = [
            expect("CHAIN[rc=0]", chain["rc"] == 0, chain["stderr"]),
            expect(
                "CHAIN[pane history_limit=20000]",
                pane_hist["stdout"] == "20000",
                f"pane read {pane_hist['stdout']!r}",
            ),
            expect(
                "CHAIN[default-terminal applied]",
                "screen-256color" in dterm["stdout"],
                dterm["stdout"],
            ),
            expect("CHAIN[incarnation round-trips]", inc in listing["stdout"], listing["stdout"]),
            expect("CHAIN[second create succeeds]", second["rc"] == 0, second["stderr"]),
            expect(
                "CHAIN[-F yields exactly 8 fields]",
                len(fields) == 8,
                f"got {len(fields)}: {fields}",
            ),
        ]
        emit(
            "CHAIN",
            "does the plan's verbatim §7.2 create chain execute, and does it reach the pane?",
            chain_rc=chain["rc"],
            stderr=chain["stderr"],
            pane_history_limit=pane_hist["stdout"],
            default_terminal=dterm["stdout"],
            incarnation_roundtrip_ok=inc in listing["stdout"],
            second_create_rc=second["rc"],
            field_count=len(fields),
            list_first_record=listing["stdout"].split("\n")[0] if listing["stdout"] else "",
            ok=all(oks),
        )

    # An UNSTAMPED session still yields 8 fields, two of them empty -- so a
    # field-count check does NOT catch a missing incarnation. Only the
    # empty-is-never-a-match rule does. The two rules are layered, not alternatives.
    with Server() as s:
        s.run("new-session", "-d", "-s", "foreign", "-x", "80", "-y", "24", "sh")
        listing = s.run("list-sessions", "-F", FIELDS_8)
        raw_fields = listing["stdout_raw"].split("\n")[0].split("\t")
        stripped_fields = listing["stdout"].split("\t")
        emit(
            "CHAIN",
            "does an unstamped (foreign) session still produce 8 fields?",
            field_count_raw=len(raw_fields),
            field_count_after_strip=len(stripped_fields),
            incarnation_field=repr(raw_fields[6] if len(raw_fields) > 6 else None),
            # The layered-rule point: 8 fields with two EMPTY, so a field-count
            # check does not detect a missing incarnation -- only the
            # empty-is-never-a-match rule does. And note the strip() trap: a
            # parser that strips first sees 6 and drops a legitimate record.
            ok=expect(
                "CHAIN[unstamped session yields 8 raw fields with empty incarnation]",
                len(raw_fields) == 8 and raw_fields[6] == "",
                f"got {len(raw_fields)} raw fields: {raw_fields}",
            ),
        )


def check_composition() -> None:
    """create -> send -> read -> list -> resize -> kill, using only safe forms."""
    with Server() as s:
        steps = []
        out = os.path.join(tempfile.mkdtemp(prefix="sbx"), "out")
        steps.append(
            s.run(
                "start-server",
                ";",
                "set-option",
                "-g",
                "history-limit",
                "20000",
                ";",
                "set-option",
                "-g",
                "remain-on-exit",
                "on",
                ";",
                "new-session",
                "-d",
                "-s",
                "build",
                "-x",
                "80",
                "-y",
                "24",
                "-c",
                "/tmp",
                "sh",
                "-c",
                f"stty -icanon; cat > {out}",
            )
        )

        payload = b"echo hello\n"
        buf = f"sb-{uuid.uuid4().hex[:8]}"
        steps.append(s.run("load-buffer", "-b", buf, "-", stdin=payload))
        steps.append(s.run("paste-buffer", "-d", "-b", buf, "-t", T("build")))
        delivered = wait_for(out, len(payload), timeout=2.0)

        read = s.run("capture-pane", "-p", "-e", "-t", T("build"))
        resize = s.run("resize-window", "-t", T("build"), "-x", "100", "-y", "30")
        after = s.run("list-sessions", "-F", "#{session_name}\t#{window_width}x#{window_height}")
        killed = s.run("kill-session", "-t", T("build"))
        gone = s.run("has-session", "-t", T("build"))

        oks = [
            expect("COMPOSITION[resize rc=0]", resize["rc"] == 0, resize["stderr"]),
            expect(
                "COMPOSITION[all steps rc=0]",
                all(st["rc"] == 0 for st in steps),
                str([st["argv"] for st in steps if st["rc"] != 0]),
            ),
            expect(
                "COMPOSITION[bytes delivered exactly]",
                delivered == len(payload),
                f"{delivered}/{len(payload)}",
            ),
            expect("COMPOSITION[capture-pane rc=0]", read["rc"] == 0, read["stderr"]),
            expect("COMPOSITION[resize applied]", "100x30" in after["stdout"], after["stdout"]),
            expect("COMPOSITION[kill rc=0]", killed["rc"] == 0, killed["stderr"]),
            expect("COMPOSITION[gone after kill]", gone["rc"] != 0, "session still present"),
        ]
        emit(
            "COMPOSITION",
            "does the full create->send->read->list->resize->kill sequence execute?",
            all_step_rcs=[st["rc"] for st in steps],
            delivered_bytes=delivered,
            sent_bytes=len(payload),
            after_resize=after["stdout"],
            kill_rc=killed["rc"],
            has_session_after_kill_rc=gone["rc"],
            ok=all(oks),
        )


# ---------------------------------------------------------------------------
# F11 -- `display-message` on a MISSING target emits the format's LITERALS.
#
# Added in W2, and it falsifies the obvious reading of F6's rule. F6 established
# "empty stdout is not_found". But the placeholders are what expand empty -- any
# LITERAL in the format still comes through. So a multi-field format against a
# session that does not exist prints the separators and NOTHING else: rc=0 AND
# non-empty stdout for a target that is not there.
#
# The adapter reads several numeric fields in one invocation, so this is exactly
# the shape it uses. The fix it adopts: every format LEADS with #{session_name},
# which is non-empty for any real session, and resolution is decided by that
# field alone.
# ---------------------------------------------------------------------------
def check_display_message_multifield() -> None:
    with Server() as s:
        s.run("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")

        single = s.run("display-message", "-p", "-t", T("nope"), "#{history_limit}")
        multi = s.run("display-message", "-p", "-t", T("nope"), "#{history_limit}\t#{pane_dead}")
        prefixed_missing = s.run(
            "display-message", "-p", "-t", T("nope"), "#{session_name}\t#{history_limit}"
        )
        prefixed_present = s.run(
            "display-message", "-p", "-t", T("build"), "#{session_name}\t#{history_limit}"
        )

        oks = [
            expect(
                "F11[single-field missing target is empty]",
                single["stdout"] == "",
                f"got {single['stdout']!r}",
            ),
            # The trap: this is NOT empty, so `if not stdout: not_found` fails here.
            expect(
                "F11[multi-field missing target emits the literal separators]",
                multi["stdout_raw"].strip("\n") != "" and multi["rc"] == 0,
                f"got {multi['stdout_raw']!r} rc={multi['rc']}",
            ),
            expect(
                "F11[and only separators -- every placeholder expanded empty]",
                multi["stdout_raw"].replace("\t", "").strip() == "",
                f"got {multi['stdout_raw']!r}",
            ),
            # The fix, both directions.
            expect(
                "F11[session_name-prefixed: first field EMPTY when missing]",
                prefixed_missing["stdout_raw"].split("\t")[0] == "",
                f"got {prefixed_missing['stdout_raw']!r}",
            ),
            expect(
                "F11[session_name-prefixed: first field is the NAME when present]",
                prefixed_present["stdout_raw"].split("\t")[0] == "build",
                f"got {prefixed_present['stdout_raw']!r}",
            ),
        ]
        emit(
            "F11",
            "does a multi-field display-message on a missing target really print nothing?",
            single_field_stdout=repr(single["stdout_raw"]),
            multi_field_stdout=repr(multi["stdout_raw"]),
            multi_field_rc=multi["rc"],
            prefixed_missing=repr(prefixed_missing["stdout_raw"]),
            prefixed_present=repr(prefixed_present["stdout_raw"]),
            ok=all(oks),
        )


# ---------------------------------------------------------------------------
# F12 -- the N1 stderr table, measured. Including the COLD-START signature the
# plan's table does not contain.
#
# `errors.py` classifies tmux failures by stderr substring, and a mapping table
# written from prose is a guess. Two rows here are new:
#
#   * a socket FILE that does not exist (no tmux server has ever run) fails with
#     `error connecting to <path> (No such file or directory)`, NOT with
#     `no server running`. Without that row the very first `shell_list` on a
#     fresh host is a `tmux_error` instead of an empty inventory.
#   * a too-long socket path fails with the SAME `error connecting to` prefix and
#     `(File name too long)` -- a misconfiguration, which must NOT be read as an
#     empty inventory. Hence each signature is a SET of required substrings.
# ---------------------------------------------------------------------------
def check_stderr_signatures() -> None:
    observed: dict[str, str] = {}

    s = Server()
    try:
        s.run("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
        observed["kill_missing"] = s.run("kill-session", "-t", T("nosuch"))["stderr"]
        observed["capture_missing"] = s.run("capture-pane", "-p", "-t", T("nosuch"))["stderr"]
        observed["set_option_missing"] = s.run("set-option", "-t", T("nosuch"), "@a", "b")["stderr"]
        observed["duplicate"] = s.run("new-session", "-d", "-s", "build")["stderr"]
        observed["no_tty"] = s.run("new-session", "-d", "-A", "-s", "build")["stderr"]

        # `no server running` requires the socket FILE to exist with nothing listening
        # behind it. This spike's own teardown UNLINKS the socket, which produces the
        # different `No such file or directory` message measured below -- the first draft
        # of this check conflated the two and the assertion caught it. The two states are
        # distinct and both are reachable in production: a killed server leaves the file,
        # a fresh host has no file at all.
        s.run("kill-server")
        observed["no_server_after_kill_server"] = s.run("list-sessions")["stderr"]
        observed["socket_survives_kill_server"] = str(os.path.exists(s.sock))
    finally:
        s.kill()

    # Synthesised rather than inherited from tmux's teardown, so this measurement does not
    # depend on whether a given tmux version unlinks its socket: a bound-but-not-listening
    # socket is refused at connect(), which is exactly the condition tmux reports.
    stale_sock = os.path.join(SOCKET_ROOT, f"sbxstale{uuid.uuid4().hex[:8]}")
    dummy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        dummy.bind(stale_sock)
        observed["no_server"] = (
            subprocess.run(
                [TMUX, "-S", stale_sock, "-f", "/dev/null", "list-sessions"], capture_output=True
            )
            .stderr.decode(errors="replace")
            .strip()
        )
    finally:
        dummy.close()
        try:
            os.unlink(stale_sock)
        except OSError:
            pass

    missing_sock = os.path.join(SOCKET_ROOT, f"sbxmissing{uuid.uuid4().hex[:8]}")
    observed["cold_start"] = (
        subprocess.run(
            [TMUX, "-S", missing_sock, "-f", "/dev/null", "list-sessions"], capture_output=True
        )
        .stderr.decode(errors="replace")
        .strip()
    )

    long_sock = os.path.join(SOCKET_ROOT, "s" * 200)
    observed["path_too_long"] = (
        subprocess.run(
            [TMUX, "-S", long_sock, "-f", "/dev/null", "list-sessions"], capture_output=True
        )
        .stderr.decode(errors="replace")
        .strip()
    )

    # (scenario, every substring errors.py requires for its classification)
    required = {
        "kill_missing": ["can't find session:"],
        "capture_missing": ["can't find session:"],
        "set_option_missing": ["no such session:"],
        "duplicate": ["duplicate session:"],
        "no_tty": ["open terminal failed"],
        "no_server": ["no server running"],
        "cold_start": ["error connecting to", "No such file or directory"],
        "path_too_long": ["error connecting to", "File name too long"],
    }
    oks = []
    for scenario, parts in required.items():
        text = observed[scenario]
        oks.append(
            expect(
                f"F12[{scenario} stderr matches its N1 signature]",
                all(part in text for part in parts),
                f"expected all of {parts} in {text!r}",
            )
        )
    # The distinction the two-part signature exists for.
    oks.append(
        expect(
            "F12[cold start and too-long path are DIFFERENT conditions]",
            "No such file or directory" not in observed["path_too_long"],
            f"path_too_long={observed['path_too_long']!r}",
        )
    )
    emit(
        "F12",
        "what does tmux actually print for each error errors.py classifies?",
        observed=observed,
        ok=all(oks),
    )


# ---------------------------------------------------------------------------
# F13 -- the socket-path limit, measured rather than assumed. `sun_path` is 104
# bytes on macOS/BSD and 108 on Linux, so ONE hardcoded number is wrong on one of
# the two platforms shellbox ships to, and the symptom is `File name too long` on
# every call with nothing naming the cause.
# ---------------------------------------------------------------------------
def check_socket_path_limit() -> None:
    longest = 0
    for length in range(90, 130):
        path = SOCKET_ROOT + "/" + "s" * (length - len(SOCKET_ROOT) - 1)
        assert len(path) == length
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
        except OSError:
            break
        else:
            longest = length
            os.unlink(path)
        finally:
            sock.close()

    # sun_path INCLUDES the NUL terminator, so the limit is one more than the
    # longest bindable path.
    sun_path = longest + 1
    expected = 104 if sys.platform != "linux" else 108
    emit(
        "F13",
        "what is this platform's sockaddr_un sun_path limit?",
        platform=sys.platform,
        longest_bindable_path=longest,
        sun_path_bytes=sun_path,
        expected=expected,
        ok=expect(
            "F13[sun_path limit is the platform's, not a hardcoded 108]",
            sun_path == expected,
            f"measured {sun_path}, expected {expected} on {sys.platform}",
        ),
    )


# ---------------------------------------------------------------------------
# F14 -- the forms W2's adapter introduced beyond this spike's original set.
# Every new form goes here FIRST and into tmux.py second.
# ---------------------------------------------------------------------------
def check_adapter_forms() -> None:
    with Server() as s:
        out = os.path.join(tempfile.mkdtemp(prefix="sbx"), "out")
        s.run(
            "start-server",
            ";",
            "set-option",
            "-g",
            "history-limit",
            "20000",
            ";",
            "set-option",
            "-g",
            "remain-on-exit",
            "on",
            ";",
            "new-session",
            "-d",
            "-s",
            "build",
            "-x",
            "80",
            "-y",
            "24",
            "-c",
            "/tmp",
            "sh",
            "-c",
            f"stty -icanon; cat > {out}",
        )

        # (a) the numeric group read, TAB-joined -- none of these can contain a TAB.
        numeric = s.run(
            "display-message",
            "-p",
            "-t",
            T("build"),
            "#{session_name}\t#{window_width}\t#{window_height}\t#{pane_dead}"
            "\t#{history_size}\t#{history_limit}",
        )
        fields = numeric["stdout_raw"].split("\n")[0].split("\t")

        # (b) capture-pane with a scrollback range.
        capture = s.run("capture-pane", "-p", "-e", "-t", T("build"), "-S", "-100")

        # (c) a named key via `send-keys -- <key>` (literal text goes via a buffer).
        keys = s.run("send-keys", "-t", T("build"), "--", "Enter")
        delivered = wait_for(out, 1, timeout=2.0)

        # (d) explicit `delete-buffer` -- the paste-buffer FAILURE path, since `-d`
        #     only fires on success and a leaked buffer both evicts other agents'
        #     buffers (buffer-limit is 50, server-wide) and retains agent input.
        buf = f"sb-{uuid.uuid4().hex[:8]}"
        s.run("load-buffer", "-b", buf, "-", stdin=b"leaked\n")
        listed_before = s.run("list-buffers")["stdout"]
        deleted = s.run("delete-buffer", "-b", buf)
        listed_after = s.run("list-buffers")["stdout"]

        oks = [
            expect("F14[numeric group yields 6 fields]", len(fields) == 6, str(fields)),
            expect(
                "F14[numeric group leads with the session name]", fields[0] == "build", str(fields)
            ),
            expect(
                "F14[pane history_limit in the group is 20000]", fields[5] == "20000", str(fields)
            ),
            expect("F14[capture-pane -S -N rc=0]", capture["rc"] == 0, capture["stderr"]),
            expect("F14[send-keys -- <key> rc=0]", keys["rc"] == 0, keys["stderr"]),
            expect(
                "F14[the named key reached the pane]",
                delivered >= 1,
                f"{delivered} bytes at the reader",
            ),
            expect("F14[buffer was present before delete]", buf in listed_before, listed_before),
            expect("F14[delete-buffer rc=0]", deleted["rc"] == 0, deleted["stderr"]),
            expect("F14[no buffer left behind]", listed_after == "", listed_after),
        ]
        emit(
            "F14",
            "do the forms W2's adapter adds behave as the adapter assumes?",
            numeric_fields=fields,
            capture_rc=capture["rc"],
            send_keys_rc=keys["rc"],
            key_bytes_delivered=delivered,
            buffers_before=listed_before,
            buffers_after=repr(listed_after),
            ok=all(oks),
        )


# ---------------------------------------------------------------------------
# F15 -- CRITICAL: the TAB separator survives ONLY under a UTF-8 ctype locale.
#
# The single most consequential finding in W2, and no prior lane could see it:
# this spike, the plan's §7 and every earlier measurement invoked tmux with the
# developer's FULL environment, which on a dev machine carries LANG=…UTF-8.
#
# When the invoking client's ctype locale is not UTF-8, tmux visually encodes the
# TAB in format output as `_` -- in `list-sessions -F` as well as
# `display-message`. All eight fields collapse into one:
#
#   build_1785477220_1785477230_80_24_0_<uuid>_/tmp
#
# Every record is then dropped as malformed, `shell_list` reports an EMPTY
# inventory, and E5 marks every live session on the host `orphaned` -- the exact
# catastrophe §12's "unknown stderr must never map to empty list" rule exists to
# prevent, reached through a channel nobody was watching.
#
# It matters because a locale is normally ABSENT in a container, a systemd unit
# and a sandbox. Passing `LANG` through is not a fix: if the parent has no locale
# there is nothing to pass. `tmux.py` therefore FORCES `LC_CTYPE=C.UTF-8`, and
# never passes `LC_ALL` (which would override it).
#
# Measured identically in both lanes.
# ---------------------------------------------------------------------------
def check_locale_tab_dependence() -> None:
    fmt = "#{session_name}\t#{@shellbox_incarnation}\t#{history_limit}"
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TERM": "xterm-256color",
    }
    variants = {
        # (env, does the TAB survive?)
        "no_locale": (dict(base), False),
        "LC_CTYPE=C.UTF-8": ({**base, "LC_CTYPE": "C.UTF-8"}, True),
        "LC_ALL=C.UTF-8": ({**base, "LC_ALL": "C.UTF-8"}, True),
        # LC_ALL overrides LC_CTYPE, so a hostile LC_ALL reinstates the bug. This is why
        # LC_ALL is not in tmux.py's pass-through allowlist.
        "LC_CTYPE=C.UTF-8+LC_ALL=C": ({**base, "LC_CTYPE": "C.UTF-8", "LC_ALL": "C"}, False),
    }

    with Server() as s:
        s.run("new-session", "-d", "-s", "build", "-x", "80", "-y", "24", "sh")
        s.run(
            "set-option",
            "-t",
            T("build"),
            "@shellbox_incarnation",
            "00000000-0000-4000-8000-000000000001",
        )

        results = {}
        oks = []
        for label, (env, tab_expected) in variants.items():
            argv = [TMUX, "-S", s.sock, "-f", "/dev/null", "list-sessions", "-F", fmt]
            listed = subprocess.run(argv, capture_output=True, env=env).stdout.decode(
                errors="replace"
            )
            argv = [
                TMUX,
                "-S",
                s.sock,
                "-f",
                "/dev/null",
                "display-message",
                "-p",
                "-t",
                T("build"),
                fmt,
            ]
            displayed = subprocess.run(argv, capture_output=True, env=env).stdout.decode(
                errors="replace"
            )
            results[label] = {"list": repr(listed), "display": repr(displayed)}
            oks.append(
                expect(
                    f"F15[{label}: TAB survives == {tab_expected}]",
                    (("\t" in listed) == tab_expected) and (("\t" in displayed) == tab_expected),
                    f"list={listed!r} display={displayed!r}",
                )
            )
            if not tab_expected:
                # And assert the SHAPE of the failure, because "the record is dropped" is a
                # much safer failure than "the record parses with wrong data".
                oks.append(
                    expect(
                        f"F15[{label}: fields collapse into ONE]",
                        len(listed.split("\n")[0].split("\t")) == 1,
                        f"got {listed!r}",
                    )
                )
        emit(
            "F15",
            "does the TAB separator in tmux format output depend on the client's locale?",
            variants=results,
            ok=all(oks),
        )


# ===========================================================================
# Phase 3 (issue #3), W14. Every check below measures a form W15/W19/W19b will
# ship, which is the only reason they may be written at all (Principle 5).
#
# S-ATTACH's first variant is a GATE, not a confirmation. R24 (High) records that
# per-window `window-size manual` holding a window's size under a LIVE attached
# client is INFERRED from F1/F9 and has no upstream precedent: omnigent sets no
# `window-size` option at all and accepts the reflow instead
# (`omnigent/terminals/ws_bridge.py:485-487`, HEAD fddb9b07). A negative result
# flips Decision A from an attached PTY to a `pipe-pane` sink -- which is why
# S-PIPE is measured here, now, rather than when the fallback is reached for.
# ===========================================================================

# The claim option W19b writes. A user option, like `@shellbox_incarnation`, and read
# back through the same `display-message` format path -- see check_s_claim for why the
# value's shape (digits and colons only) is what makes that safe.
PUBLISHER_OPTION = "@shellbox_publisher"

# The environment a `tmux attach` client is handed.
#
# `TERM` describes the FAR end -- the terminal the bytes are ultimately rendered on --
# not the process doing the bridging. A headless host has no tty, bash substitutes
# `TERM=dumb`, and `tmux attach` refuses a dumb terminal, so the viewer would render
# that refusal instead of the pane (transcribed decision 5, ADR-15,
# `ws_bridge.py:138-148`). S-ATTACH[TERM=dumb] measures the refusal rather than
# trusting the citation.
#
# `LC_CTYPE` is forced for the same reason `tmux.py` forces it (F15): a container has
# no locale to inherit.
ATTACH_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "TERM": "xterm-256color",
    "LC_CTYPE": "C.UTF-8",
}


def set_winsize(fd: int, cols: int, rows: int) -> None:
    """The `TIOCSWINSZ` W19 applies on a resize control frame, on a pty fd."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Attach:
    """One live `tmux attach` client on its own pty, torn down on exit.

    Two spawn mechanisms, because `ADR-16` lists the second as an alternative to the
    first and routes the deciding question here:

    * ``forkpty`` -- `os.forkpty()` then `os.execve`, omnigent's shape
      (`ws_bridge.py:151`). `execve` rather than `execvpe` so the child allocates
      nothing before `exec`; the binary is therefore resolved in the PARENT, which is
      also what `ADR-1`'s single-resolution-point rule wants. The child gets the slave
      as its CONTROLLING terminal, because that is what `forkpty` does.
    * ``popen`` -- `os.openpty()` plus `subprocess.Popen(start_new_session=True)`.
      Emits no `DeprecationWarning` (the fork and exec happen in C, with no Python
      executed in the child), but performs no `TIOCSCTTY`, so the slave is NOT the
      child's controlling terminal. **Whether `tmux attach` tolerates that is the open
      question `ADR-16` routes to S-ATTACH**, and it is measured, not assumed.

    Never a context manager that swallows failures: `close()` reaps the child AND
    closes the master fd, because `os.forkpty` hands back a raw int with no object
    whose collection would close it. An unclosed master plus an unreaped child is a
    live tmux client holding the window at the last viewer's size forever -- PM3's
    reflow made permanent, which is exactly the residual `W19b` exists to close.
    """

    def __init__(
        self,
        sock: str,
        name: str,
        *,
        cols: int = 120,
        rows: int = 40,
        mechanism: str = "forkpty",
        read_only: bool = False,
        term: str = "xterm-256color",
    ) -> None:
        argv = [TMUX, "-S", sock, "-f", "/dev/null", "attach"]
        if read_only:
            argv.append("-r")
        argv += ["-t", T(name)]
        self.argv = argv
        self.mechanism = mechanism
        self.env = {**ATTACH_ENV, "TERM": term}
        self.pid = -1
        self.master = -1
        self._slave = -1
        self._proc: subprocess.Popen[bytes] | None = None
        self.fork_warnings: list[str] = []

        binary = shutil.which(argv[0]) or argv[0]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            if mechanism == "forkpty":
                pid, master = os.forkpty()
                if pid == 0:  # pragma: no cover -- the child never returns
                    try:
                        os.execve(binary, argv, self.env)
                    finally:
                        os._exit(127)
                self.pid, self.master = pid, master
            else:
                master, slave = os.openpty()
                self.master, self._slave = master, slave
                set_winsize(slave, cols, rows)
                self._proc = subprocess.Popen(
                    argv,
                    executable=binary,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=self.env,
                    start_new_session=True,
                    close_fds=True,
                )
                self.pid = self._proc.pid
            self.fork_warnings = [str(w.message) for w in caught]
        # After the spawn either way: tmux may already have read the size, in which case
        # the ioctl delivers SIGWINCH and it re-reads. Polling is what makes that safe --
        # never a fixed sleep (§11.1).
        set_winsize(self.master, cols, rows)

    def drain(self, duration: float = 1.0) -> bytes:
        """Everything the pty master offers within `duration`. Never blocks past it."""
        buf = b""
        deadline = time.time() + duration
        while time.time() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if not readable:
                continue
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    def write(self, payload: bytes) -> int:
        return os.write(self.master, payload)

    def exit_status(self) -> int | None:
        """The child's status if it has already exited, else None."""
        if self._proc is not None:
            return self._proc.poll()
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return -1
        return status if pid else None

    def close(self) -> None:
        """SIGTERM, escalate to SIGKILL, reap, then close both fds. In that order."""
        for sig in (15, 9):
            try:
                os.kill(self.pid, sig)
            except OSError:
                break
            deadline = time.time() + 2.0
            reaped = False
            while time.time() < deadline:
                if self.exit_status() is not None:
                    reaped = True
                    break
                time.sleep(0.02)
            if reaped:
                break
        for fd in (self.master, self._slave):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self._slave = -1

    def __enter__(self) -> Attach:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False


def create_session(
    s: Server,
    name: str,
    *,
    cols: int = 80,
    rows: int = 24,
    window_size_manual: bool = False,
    command: tuple[str, ...] = ("sh",),
) -> dict:
    """`tmux.py`'s shipped create chain, optionally with `ADR-10`'s per-window option.

    Transcribed from `check_chain_verbatim` rather than invented, so a measurement here
    describes the chain that actually ships. The `-w` `set-option` goes AFTER
    `new-session` because the window it targets does not exist before it -- that is the
    create-time placement `ADR-10` calls its default.
    """
    inc = str(uuid.uuid4())
    # fmt: off
    chain = [
        "start-server",
        ";",
        "set-option", "-g", "history-limit", "20000",
        ";",
        "set-option", "-g", "status", "off",
        ";",
        "set-option", "-g", "default-terminal", "screen-256color",
        ";",
        "set-option", "-g", "remain-on-exit", "on",
        ";",
        "new-session", "-d", "-s", name,
        "-x", str(cols), "-y", str(rows), "-c", "/tmp",
        *command,
    ]
    if window_size_manual:
        chain += [";", "set-option", "-w", "-t", T(name), "window-size", "manual"]
    chain += [
        ";",
        "set-option", "-t", T(name), "@shellbox_incarnation", inc,
    ]
    # fmt: on
    return s.run(*chain)


def read_size(s: Server, name: str) -> tuple[int, int] | None:
    """`#{window_width}`/`#{window_height}` through the shipped `_display_numeric` shape.

    Leads with `#{session_name}` and treats an empty first field as unresolved, because
    F11 measured that a multi-field format against a missing target prints its literal
    separators at rc=0 -- so `if not stdout` is not a resolution check.
    """
    fmt = "#{session_name}\t#{window_width}\t#{window_height}"
    parts = s.run("display-message", "-p", "-t", T(name), fmt)["stdout_raw"]
    fields = parts.split("\n")[0].split("\t")
    if len(fields) != 3 or not fields[0]:
        return None
    try:
        return int(fields[1]), int(fields[2])
    except ValueError:
        return None


def read_attached(s: Server, name: str) -> int:
    """How many clients tmux currently counts as attached to this session.

    `#{session_attached}` rather than `list-clients`, deliberately: it is a format field
    the shipped `display-message` path already reads, so measuring it introduces no new
    command form -- and W15/W19 need none.
    """
    fmt = "#{session_name}\t#{session_attached}"
    fields = s.run("display-message", "-p", "-t", T(name), fmt)["stdout_raw"]
    parts = fields.split("\n")[0].split("\t")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def wait_attached(s: Server, name: str, *, want: int = 1, timeout: float = 8.0) -> int:
    """Poll until at least `want` clients are attached. Returns the count seen.

    Zero means the attach never took, and EVERY size measurement below asserts a
    non-zero count first. Without that guard a "the size held" result is indistinguishable
    from "nothing ever attached", which is the vacuous pass this whole gate turns on.
    """
    deadline = time.time() + timeout
    seen = 0
    while time.time() < deadline:
        seen = read_attached(s, name)
        if seen >= want:
            return seen
        time.sleep(0.05)
    return seen


def observe_sizes(s: Server, name: str, duration: float = 1.5) -> tuple[list[str], int]:
    """Every DISTINCT size seen over `duration`, in the order first seen, plus the sample count.

    Sustained observation rather than one sample, for two reasons. A reflow is not
    instantaneous, so a single read right after the attach can miss it and report a hold
    that is not there. And `ADR-10`'s attach-time placement is priced on an EXPOSURE
    WINDOW -- a transient reflow between the attach and the option taking effect -- which
    only a series can see at all.

    Honest limitation, stated because the finding must not overclaim: each sample is a
    subprocess round trip, so the sampling interval is milliseconds, not microseconds.
    A window shorter than that is bounded by this measurement, not excluded by it.
    """
    seen: list[str] = []
    samples = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        size = read_size(s, name)
        samples += 1
        label = "unresolved" if size is None else f"{size[0]}x{size[1]}"
        if label not in seen:
            seen.append(label)
    return seen, samples


def check_s_attach(trials: int = 15) -> None:
    """S-ATTACH -- the R24 gate. Does per-window `window-size manual` hold under a live client?

    Four placements, because `ADR-10` names two and the two failure directions are
    different:

    * `no_option` -- the control, and it must show the reflow. If a viewer at 120x40
      attaching to an 80x24 session does NOT reflow it on this tmux, then PM3 does not
      exist and neither does the mitigation, so this variant asserts the HAZARD.
    * `at_create` -- the option inside the create chain. `ADR-10`'s default, and the one
      with no window of exposure.
    * `before_attach` -- set on an existing window immediately before the client spawns.
      The publisher's natural placement; it freezes the agent create path, so if it holds
      it is the better choice.
    * `after_attach` -- set once the client is already live and the window already
      reflowed. This prices the exposure window: does the option RESTORE the original
      size, or does it freeze the window at the reflowed size until something resizes it?
    """
    for variant in ("no_option", "at_create", "before_attach", "after_attach"):
        with Server() as s:
            create_session(s, "build", window_size_manual=(variant == "at_create"))
            before = read_size(s, "build")
            if variant == "before_attach":
                s.run("set-option", "-w", "-t", T("build"), "window-size", "manual")

            attached = 0
            reflowed_before_option = None
            with Attach(s.sock, "build", cols=120, rows=40) as client:
                attached = wait_attached(s, "build")
                if variant == "after_attach":
                    # Let the reflow land first -- the point of this variant is to set the
                    # option AFTER the damage, not to race it.
                    deadline = time.time() + 2.0
                    while time.time() < deadline and read_size(s, "build") == before:
                        time.sleep(0.02)
                    reflowed_before_option = read_size(s, "build")
                    s.run("set-option", "-w", "-t", T("build"), "window-size", "manual")
                sizes, samples = observe_sizes(s, "build")
                during = read_size(s, "build")
                # Can the publisher still set a size while manual is in force? `shell_resize`
                # is a shipped tool and it issues exactly this, so an option that broke it
                # would be a regression the gate has to catch.
                resize = s.run("resize-window", "-t", T("build"), "-x", "90", "-y", "28")
                deadline = time.time() + 2.0
                while time.time() < deadline and read_size(s, "build") != (90, 28):
                    time.sleep(0.02)
                after_resize = read_size(s, "build")
                client_alive = client.exit_status() is None

            detached = wait_attached(s, "build", want=0, timeout=3.0)
            after_detach = read_size(s, "build")

            held = sizes == [f"{before[0]}x{before[1]}"] if before else False
            oks = [
                expect(
                    f"S-ATTACH[{variant}: a client really attached]",
                    attached >= 1,
                    "session_attached never reached 1 -- every size result below is vacuous",
                ),
                expect(
                    f"S-ATTACH[{variant}: the client was still live at measurement time]",
                    client_alive,
                    "the attach child exited before the size was read",
                ),
                expect(
                    f"S-ATTACH[{variant}: resize-window still works]",
                    resize["rc"] == 0 and after_resize == (90, 28),
                    f"rc={resize['rc']} {resize['stderr']!r} size={after_resize}",
                ),
            ]
            if variant == "no_option":
                # Assert the HAZARD, so the mitigation can never look unnecessary.
                oks.append(
                    expect(
                        "S-ATTACH[no_option: the attach DOES reflow the window (PM3 is real)]",
                        not held,
                        f"a 120x40 client left an {before} window unchanged; PM3 may not apply",
                    )
                )
            elif variant in ("at_create", "before_attach"):
                oks.append(
                    expect(
                        f"S-ATTACH[{variant}: the size holds under a live client]",
                        held,
                        f"sizes seen {sizes} over {samples} samples, wanted only {before}",
                    )
                )
            emit(
                "S-ATTACH",
                "does per-window `window-size manual` hold a window's size under a LIVE "
                "attached client?",
                variant=variant,
                clients_attached=attached,
                size_before_attach=before,
                sizes_seen_during_attach=sizes,
                samples=samples,
                size_during_attach=during,
                reflowed_before_option_was_set=reflowed_before_option,
                held=held,
                resize_window_rc=resize["rc"],
                size_after_resize_window=after_resize,
                clients_after_detach=detached,
                size_after_detach=after_detach,
                ok=all(oks),
            )

    # `attach -r`. A read-only client cannot send input -- so if it also does not resize
    # the window it would be a second, independent mitigation, and W19's argv would owe a
    # policy decision. Measured rather than reasoned about.
    with Server() as s:
        create_session(s, "build")
        before = read_size(s, "build")
        with Attach(s.sock, "build", cols=120, rows=40, read_only=True) as client:
            attached = wait_attached(s, "build")
            sizes, samples = observe_sizes(s, "build")
            payload = client.drain(1.0)
            alive = client.exit_status() is None
        emit(
            "S-ATTACH",
            "does `attach -r` succeed on this tmux, and does a read-only client still "
            "resize the window?",
            variant="read_only_attach_-r",
            clients_attached=attached,
            size_before_attach=before,
            sizes_seen_during_attach=sizes,
            samples=samples,
            bytes_received=len(payload),
            child_still_live=alive,
            ok=expect(
                "S-ATTACH[attach -r is accepted and delivers output]",
                attached >= 1 and len(payload) > 0,
                f"attached={attached} bytes={len(payload)}",
            ),
        )

    # Does the option PERSIST across a detach and a second attach at a different size?
    #
    # `ADR-10`'s attach-time placement rests on this and does not say so: if the option were
    # scoped to a client, or cleared when the last client left, then setting it once before
    # the first attach would protect only that attach and the SECOND viewer would reflow the
    # window -- which is the case "do not set it and size the PTY to the session" was already
    # rejected for. It is a window option, so it should persist; measured rather than assumed.
    with Server() as s:
        create_session(s, "build")
        before = read_size(s, "build")
        s.run("set-option", "-w", "-t", T("build"), "window-size", "manual")
        first = Attach(s.sock, "build", cols=120, rows=40)
        attached_first = wait_attached(s, "build")
        sizes_first, _ = observe_sizes(s, "build", duration=0.75)
        first.close()
        wait_attached(s, "build", want=0, timeout=3.0)
        # A DIFFERENT size, so a second viewer that could reflow would move it somewhere the
        # first viewer's size does not explain.
        second = Attach(s.sock, "build", cols=100, rows=50)
        attached_second = wait_attached(s, "build")
        sizes_second, samples_second = observe_sizes(s, "build", duration=0.75)
        second.close()
        want = [f"{before[0]}x{before[1]}"] if before else []
        emit(
            "S-ATTACH",
            "does a per-window `window-size manual` set once survive a detach and a SECOND "
            "attach at a different size?",
            variant="second_attach_after_detach",
            size_at_create=before,
            first_client="120x40",
            clients_attached_first=attached_first,
            sizes_during_first_attach=sizes_first,
            second_client="100x50",
            clients_attached_second=attached_second,
            sizes_during_second_attach=sizes_second,
            samples_second=samples_second,
            ok=expect(
                "S-ATTACH[the option set once holds for a SECOND viewer too]",
                attached_first >= 1 and attached_second >= 1
                and sizes_first == want and sizes_second == want,
                f"first={sizes_first} second={sizes_second} wanted {want} "
                f"(attached {attached_first}/{attached_second})",
            ),
        )

    # The option's OTHER half, re-measured with the option where W15 would put it. F9
    # measured the per-window form safe as a standalone call; this measures it inside the
    # create chain, which is the composition that ships. F1's whole lesson is that the
    # composition is where these defects live.
    crashes = 0
    first_err = ""
    for _ in range(trials):
        with Server() as s:
            create_session(s, "a", window_size_manual=True)
            second = create_session(s, "b", window_size_manual=True)
            if second["rc"] != 0:
                crashes += 1
                first_err = first_err or second["stderr"]
    emit(
        "S-ATTACH",
        "does the per-window `window-size manual` INSIDE the create chain survive a "
        "second create?",
        variant="per_window_in_create_chain",
        trials=trials,
        second_create_failures=crashes,
        first_error=first_err,
        ok=expect(
            "S-ATTACH[per-window option in the create chain: 0 second-create failures]",
            crashes == 0,
            f"{crashes}/{trials} second creates failed -- this is F1 with the safe scope",
        ),
    )


def check_s_attach_mechanism() -> None:
    """S-ATTACH -- the spawn mechanism, the `TIOCSCTTY` question, and the in-band repaint.

    Four things a citation currently stands in for, all of which `ADR-15`/`ADR-16` rest on.
    The fourth is the one that decides the mechanism, and it is not the one the ADR names:

    1. **`os.forkpty()` warns in a threaded process, and `Popen` does not.** `W19` must
       either silence that warning deliberately or adopt the `Popen` mechanism, and the
       choice needs the measurement rather than the changelog. A thread is started here on
       purpose -- `enroll.py` already runs two daemon threads, so shellbox's fork is
       unavoidably multithreaded in production.
    2. **Does `tmux attach` tolerate a slave that is not the child's controlling
       terminal?** `subprocess` performs no `TIOCSCTTY`. This is the open question
       `ADR-16` routes here -- and it is the WRONG question, because the answer is yes
       and the mechanism still loses. See 4.
    3. **Is the initial repaint really free, in-band, and ordered by tmux?** This is
       Decision A's strongest argument for an attached PTY over `pipe-pane` and it was
       argued from the ABSENCE of a `capture-pane` call in omnigent's bridge. Absence of a
       call is not presence of a repaint, so: write a sentinel into the pane, attach, and
       look for the sentinel in the master fd with no `capture-pane` issued at all.
    4. **Does a `TIOCSWINSZ` on the master actually reach tmux?** W19 applies exactly that
       for a resize control frame. Not on the ADR's list, and it is what settles the
       mechanism: a controlling terminal is not needed to ATTACH, but it is needed to be
       SIGWINCH'd, so the `Popen` route comes up fine and then silently ignores every
       resize. An attach that works and a resize that vanishes is a worse outcome than an
       attach that fails, which is why this had to be measured rather than inferred from
       whether the client came up.
    """
    idle = threading.Event()
    spectator = threading.Thread(target=idle.wait, name="spike-spectator", daemon=True)
    spectator.start()
    try:
        for mechanism in ("forkpty", "popen"):
            with Server() as s:
                # `conftest.Sentinel`'s invariant, borrowed rather than reinvented: the
                # needle must not be a substring of the command that produces it, or the
                # echoed command line alone satisfies the check.
                needle = f"sbx{uuid.uuid4().hex[:10]}"
                create_session(s, "build", command=("sh", "-c", f"printf %s\\\\n {needle}; sleep 300"))
                # The pane has printed the needle before any client exists, so anything the
                # master fd yields is tmux repainting the EXISTING screen to a new client.
                deadline = time.time() + 3.0
                painted = ""
                while time.time() < deadline:
                    painted = s.run("capture-pane", "-p", "-t", T("build"))["stdout"]
                    if needle in painted:
                        break
                    time.sleep(0.05)

                client = Attach(s.sock, "build", mechanism=mechanism)
                try:
                    attached = wait_attached(s, "build", timeout=5.0)
                    received = client.drain(1.5)
                    status = client.exit_status()
                    fork_warnings = client.fork_warnings
                    # Does a viewer resize actually PROPAGATE on this mechanism? W19 applies
                    # `TIOCSWINSZ` to the master for a resize control frame, and the kernel
                    # delivers the resulting SIGWINCH to the pty's FOREGROUND PROCESS GROUP --
                    # which a child with no controlling terminal is not a member of. So this is
                    # not a formality: it is the failure a missing `TIOCSCTTY` would actually
                    # produce, and it would present as a viewer whose resizes are silently
                    # ignored rather than as an attach that fails. No `window-size manual` is
                    # set here, so the window is free to follow the client.
                    set_winsize(client.master, 100, 30)
                    deadline = time.time() + 3.0
                    while time.time() < deadline and read_size(s, "build") != (100, 30):
                        time.sleep(0.02)
                    resized = read_size(s, "build")
                finally:
                    client.close()

                repainted = needle.encode() in received
                oks = [
                    expect(
                        f"S-ATTACH[{mechanism}: the attach client came up]",
                        attached >= 1,
                        f"session_attached={attached} child_status={status} "
                        f"first_bytes={received[:200]!r}",
                    ),
                    expect(
                        f"S-ATTACH[{mechanism}: tmux repaints the pane in-band, no capture-pane]",
                        repainted,
                        f"the sentinel was on screen but not in {len(received)} bytes of "
                        "attach output",
                    ),
                ]
                # The measured asymmetry, asserted in BOTH directions so neither half can
                # rot silently. `forkpty` propagates a resize; `popen` does NOT, because the
                # kernel delivers SIGWINCH to the pty's foreground process group and a child
                # with no controlling terminal is not in one. Asserting only the positive
                # half would let a future kernel or tmux quietly rehabilitate the `popen`
                # route without anyone noticing it had; asserting only the negative half
                # would let the mechanism W19 actually ships regress unnoticed.
                if mechanism == "forkpty":
                    oks.append(
                        expect(
                            "S-ATTACH[forkpty: TIOCSWINSZ on the master reaches tmux]",
                            resized == (100, 30),
                            f"the window read {resized} after the master was set to 100x30 -- "
                            "W19's whole resize path runs through this",
                        )
                    )
                else:
                    oks.append(
                        expect(
                            "S-ATTACH[popen: TIOCSWINSZ does NOT reach tmux -- no controlling tty]",
                            resized != (100, 30),
                            f"the popen route propagated a resize ({resized}); if that is now "
                            "reliable, ADR-16's Popen alternative is back on the table and the "
                            "forkpty DeprecationWarning no longer has to be lived with",
                        )
                    )
                if mechanism == "forkpty":
                    oks.append(
                        expect(
                            "S-ATTACH[forkpty in a threaded process warns]",
                            any("multi-threaded" in w for w in fork_warnings),
                            f"expected a DeprecationWarning, got {fork_warnings!r} -- if this "
                            "stops firing, W19's obligation to silence it is obsolete",
                        )
                    )
                else:
                    oks.append(
                        expect(
                            "S-ATTACH[popen route emits no fork warning]",
                            not fork_warnings,
                            str(fork_warnings),
                        )
                    )
                emit(
                    "S-ATTACH",
                    "which spawn mechanism can host the attach client, and does the initial "
                    "repaint arrive in-band?",
                    variant=f"mechanism_{mechanism}",
                    controlling_terminal="yes (forkpty)" if mechanism == "forkpty" else "NO (no TIOCSCTTY)",
                    clients_attached=attached,
                    child_status=status,
                    bytes_received=len(received),
                    sentinel_on_screen_before_attach=needle in painted,
                    sentinel_in_attach_stream=repainted,
                    size_after_TIOCSWINSZ_100x30=resized,
                    fork_warnings=fork_warnings,
                    ok=all(oks),
                )
    finally:
        idle.set()
        spectator.join(timeout=2.0)

    # TERM=dumb. ADR-15's decision 5, measured.
    with Server() as s:
        create_session(s, "build")
        client = Attach(s.sock, "build", term="dumb")
        try:
            output = client.drain(1.5)
            attached = read_attached(s, "build")
            deadline = time.time() + 2.0
            while time.time() < deadline and client.exit_status() is None:
                time.sleep(0.02)
            status = client.exit_status()
        finally:
            client.close()
        emit(
            "S-ATTACH",
            "does `tmux attach` refuse a dumb terminal, as ADR-15's decision 5 says?",
            variant="TERM=dumb",
            clients_attached=attached,
            child_status=status,
            output=repr(output[:300]),
            ok=expect(
                "S-ATTACH[TERM=dumb is refused]",
                attached == 0,
                f"a dumb terminal attached anyway (attached={attached}); the forced TERM in "
                "attach_argv would then be unjustified",
            ),
        )


def check_s_attach_input() -> None:
    """S-ATTACH -- input through the attach master, and H4 on that path.

    The load-bearing half is the second measurement. An earlier plan revision claimed an
    attached PTY ESCAPES H4; it does not, and the correction inverts into a hazard. H4 is
    the RECEIVING pane's tty in canonical mode (F5's table reads `raw (both) ok` at every
    length, so the loss belongs to the pane's line discipline), and tmux forwards an attach
    client's keystrokes to that same pty. `max_send_line_bytes` is a LOUD `LineTooLong`
    rejection at the tool boundary; a PTY input path has no ceiling at all, and F5's verdict
    is that truncation is the worse failure because "a truncated command is a different,
    still-executable command".

    So this measures the hazard on the attach path specifically. If it reproduces here,
    `W19`'s per-line ceiling is a measured requirement rather than an inferred one.
    """
    with Server() as s:
        out = os.path.join(tempfile.mkdtemp(prefix="sbx"), "out")
        create_session(s, "build", command=("sh", "-c", f"cat > {out}"))
        with Attach(s.sock, "build") as client:
            attached = wait_attached(s, "build")
            client.drain(0.5)  # discard the initial repaint

            short = b"hello-from-the-attach-pty\n"
            client.write(short)
            delivered_short = wait_for(out, len(short), timeout=3.0)

            long_line = (b"x" * 8192) + b"\n"
            client.write(long_line)
            # A ceiling-free path would deliver everything; Linux truncates at 4096 and
            # macOS discards. Wait for the full amount so a PASS cannot come from reading
            # too early, then report what actually arrived.
            total = wait_for(out, delivered_short + len(long_line), timeout=4.0)
            delivered_long = total - delivered_short
            alive = client.exit_status() is None

        lossless = delivered_long == len(long_line)
        oks = [
            expect(
                "S-ATTACH[input: a client attached and stayed live]",
                attached >= 1 and alive,
                f"attached={attached} alive={alive}",
            ),
            expect(
                "S-ATTACH[input: a short line reaches the pane byte-exact]",
                delivered_short == len(short),
                f"{delivered_short}/{len(short)} bytes",
            ),
            # Only the LOSS is asserted, not its shape: F5 measured macOS discarding and
            # Linux truncating, and the point here is that the attach path does not escape
            # either.
            expect(
                "S-ATTACH[input: an 8 KiB line through the attach pty is NOT delivered whole]",
                not lossless,
                "the attach path delivered 8193 bytes intact, which would mean H4 does not "
                "apply to it and W19's ceiling rests on nothing",
            ),
        ]
        emit(
            "S-ATTACH",
            "does input written to the attach master reach the pane, and does H4 apply to "
            "that path?",
            variant="input_and_h4",
            clients_attached=attached,
            short_line_bytes_sent=len(short),
            short_line_bytes_delivered=delivered_short,
            long_line_bytes_sent=len(long_line),
            long_line_bytes_delivered=delivered_long,
            long_line_lossless=lossless,
            platform=sys.platform,
            ok=all(oks),
        )


def check_s_pane_dead() -> None:
    """S-PANE-DEAD -- `#{pane_dead}` in BOTH directions, with a live client present.

    This validates an assumption already load-bearing in shipped code rather than gating
    new code: `tmux.py` derives `alive` from `#{pane_dead}` and calls it "the single source
    of truth for liveness". So it runs whether or not W19 proceeds.

    Read through the shipped `display-message` path, NOT `list-panes`. omnigent prefers
    `list-panes` because `display-message` can fall back to another pane -- but that cannot
    happen here: the format leads with `#{session_name}` and an empty first field reads as
    unresolved, and no shellbox code path ever issues `split-window` or `new-window`, so
    every session has one window and one pane.

    The dead direction is the one that matters and the one an earlier plan revision did not
    measure: it is what the 4404-terminal-gone / 4405-detached close-code split turns on,
    and a detach misread as terminal-gone would tear down the whole session.
    """
    fmt = "#{session_name}\t#{pane_dead}"

    # Direction 1: DETACH. The client goes away; the pane must still read alive.
    with Server() as s:
        create_session(s, "build", command=("sh", "-c", "sleep 300"))
        client = Attach(s.sock, "build")
        attached = wait_attached(s, "build")
        during = s.run("display-message", "-p", "-t", T("build"), fmt)["stdout_raw"]
        client.close()
        after_clients = wait_attached(s, "build", want=0, timeout=3.0)
        after = s.run("display-message", "-p", "-t", T("build"), fmt)["stdout_raw"]
        has_session = s.run("has-session", "-t", T("build"))
        during_dead = during.split("\n")[0].split("\t")
        after_dead = after.split("\n")[0].split("\t")
        oks = [
            expect("S-PANE-DEAD[detach: a client was attached first]", attached >= 1, str(attached)),
            expect(
                "S-PANE-DEAD[detach: pane_dead is 0 while attached]",
                during_dead[:2] == ["build", "0"],
                repr(during),
            ),
            expect(
                "S-PANE-DEAD[detach: pane_dead is STILL 0 after the client is killed]",
                after_dead[:2] == ["build", "0"],
                repr(after),
            ),
            expect(
                "S-PANE-DEAD[detach: the session survives the detach]",
                has_session["rc"] == 0,
                has_session["stderr"],
            ),
        ]
        emit(
            "S-PANE-DEAD",
            "does killing the attach client leave the pane alive?",
            direction="detach",
            clients_attached=attached,
            clients_after=after_clients,
            display_while_attached=repr(during),
            display_after_detach=repr(after),
            has_session_rc=has_session["rc"],
            ok=all(oks),
        )

    # Direction 2: the pane's PROCESS exits while a client is still attached. Under the
    # global `remain-on-exit on` the session must survive and the pane must read dead.
    with Server() as s:
        marker = os.path.join(tempfile.mkdtemp(prefix="sbx"), "done")
        create_session(
            s, "build", command=("sh", "-c", f"sleep 0.75; : > {marker}; exit 0")
        )
        client = Attach(s.sock, "build")
        try:
            attached = wait_attached(s, "build")
            wait_for(marker, 0, timeout=5.0)
            deadline = time.time() + 5.0
            observed = ""
            while time.time() < deadline:
                observed = s.run("display-message", "-p", "-t", T("build"), fmt)["stdout_raw"]
                if observed.split("\n")[0].split("\t")[1:2] == ["1"]:
                    break
                time.sleep(0.05)
            has_session = s.run("has-session", "-t", T("build"))
            still_attached = read_attached(s, "build")
            child_status = client.exit_status()
            fields = observed.split("\n")[0].split("\t")
        finally:
            client.close()
        oks = [
            expect(
                "S-PANE-DEAD[dead: a client was attached when the process exited]",
                attached >= 1,
                str(attached),
            ),
            expect(
                "S-PANE-DEAD[dead: pane_dead reads 1 after the pane's process exits]",
                fields[:2] == ["build", "1"],
                repr(observed),
            ),
            expect(
                "S-PANE-DEAD[dead: the session SURVIVES it (remain-on-exit on)]",
                has_session["rc"] == 0,
                has_session["stderr"],
            ),
        ]
        emit(
            "S-PANE-DEAD",
            "with a live attach client present, does a pane whose process exited read dead "
            "while the session survives?",
            direction="pane_process_exited",
            clients_attached=attached,
            clients_still_attached_after_death=still_attached,
            attach_child_status_after_pane_death=child_status,
            display_after_exit=repr(observed),
            has_session_rc=has_session["rc"],
            ok=all(oks),
        )


def check_s_pipe() -> None:
    """S-PIPE -- what does a SECOND `pipe-pane` on one pane do to the first?

    Prices Decision A's designated fallback, which is why it is measured now rather than
    when the fallback is reached for. Both spellings, because they are not the same
    question and the plan's A2 sketch uses the second:

    * without `-o`: does the new pipe REPLACE the first, silently stealing a viewer's
      stream? Multi-viewer is a D6 expectation.
    * with `-o`: tmux documents `-o` as "only open a new pipe if no previous pipe exists",
      which makes a second call a TOGGLE. If so, a second viewer would not steal the
      stream -- it would turn the first viewer's pipe OFF, which is a different and worse
      failure than replacement, and it is the form A2 was written with.
    """
    for flag in ("none", "-o"):
        with Server() as s:
            base = tempfile.mkdtemp(prefix="sbxpipe")
            first = os.path.join(base, "first")
            second = os.path.join(base, "second")
            create_session(s, "build", command=("sh", "-c", "sleep 300"))

            args = ["pipe-pane"] + (["-o"] if flag == "-o" else [])
            open_first = s.run(*args, "-t", T("build"), f"cat >> {first}")
            s.run("send-keys", "-t", T("build"), "-l", "alpha")
            s.run("send-keys", "-t", T("build"), "--", "Enter")
            first_before = wait_for(first, 1, timeout=3.0)

            open_second = s.run(*args, "-t", T("build"), f"cat >> {second}")
            s.run("send-keys", "-t", T("build"), "-l", "bravo")
            s.run("send-keys", "-t", T("build"), "--", "Enter")
            second_size = wait_for(second, 1, timeout=3.0)
            first_after = os.path.getsize(first) if os.path.exists(first) else 0

            if second_size > 0 and first_after == first_before:
                verdict = "the second pipe REPLACED the first"
            elif second_size > 0 and first_after > first_before:
                verdict = "both pipes received output"
            elif second_size == 0 and first_after == first_before:
                verdict = "the second call TOGGLED piping OFF -- neither sink received it"
            else:
                verdict = "the second call did not open a pipe; the first kept receiving"
            emit(
                "S-PIPE",
                "does a second `pipe-pane` on one pane replace, coexist with, or toggle off "
                "the first?",
                variant=f"pipe_pane_{flag}",
                first_open_rc=open_first["rc"],
                second_open_rc=open_second["rc"],
                first_sink_bytes_before_second_open=first_before,
                first_sink_bytes_after=first_after,
                second_sink_bytes=second_size,
                verdict=verdict,
                # No assertion: this measures a form shellbox does not ship, and the finding
                # IS the result. Asserting a guessed answer here would only encode the guess.
                ok=expect(
                    f"S-PIPE[{flag}: the first pipe-pane call itself works]",
                    open_first["rc"] == 0 and first_before > 0,
                    f"rc={open_first['rc']} bytes={first_before} {open_first['stderr']!r}",
                ),
            )
            shutil.rmtree(base, ignore_errors=True)


# Run by check_s_claim in TWO concurrent processes. It writes a claim and reads it back,
# which is the whole protocol -- so a race between two of these is a race between two
# publishers. The anchored target is passed IN rather than built here, so the child cannot
# introduce a target form this file's own SELF check does not cover.
_CLAIM_RACER = r"""
import os, subprocess, sys, time

tmux, sock, target, option, value, trigger = sys.argv[1:7]
env = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "TERM": "xterm-256color",
    "LC_CTYPE": "C.UTF-8",
}
base = [tmux, "-S", sock, "-f", "/dev/null"]
# Both racers spin on one trigger file so the write-then-read-back sequences overlap.
# Without it the two processes start milliseconds apart and the race never happens.
while not os.path.exists(trigger):
    time.sleep(0.0005)
subprocess.run(base + ["set-option", "-t", target, option, value], env=env, capture_output=True)
read = subprocess.run(
    base + ["display-message", "-p", "-t", target, "#{session_name}\t#{" + option + "}"],
    env=env,
    capture_output=True,
)
fields = read.stdout.decode(errors="replace").split("\n")[0].split("\t")
saw = fields[1] if len(fields) > 1 else ""
print("own" if saw == value else "foreign", saw, sep="\t")
"""


def tid_starttime(pid: int, tid: int) -> int | None:
    """Field 22 of `/proc/<pid>/task/<tid>/stat`, or None where `/proc` is not there.

    Field 2 (`comm`) is parenthesised and may itself contain spaces and a `)`, so the split
    is after the LAST `)`. Everything after it begins at field 3, which puts field 22 at
    index 19. Getting this wrong reads a neighbouring counter and the claim then compares
    two numbers that are not start times -- silently, since they are both plausible ints.
    """
    path = f"/proc/{pid}/task/{tid}/stat"
    try:
        with open(path, "rb") as handle:
            raw = handle.read().decode(errors="replace")
    except OSError:
        return None
    try:
        tail = raw[raw.rindex(")") + 1 :].split()
        return int(tail[19])
    except (ValueError, IndexError):
        return None


def check_s_claim(race_trials: int = 12) -> None:
    """S-CLAIM -- the four things `ADR-16`'s claim protocol rests on and has not measured.

    (a) The `set-option`/read-back round trip for a claim, including a read of a claim
        written by ANOTHER PROCESS, since the design's premise is 1-32 of them. Both read
        paths are measured -- the shipped `display-message` format and `show-options -v` --
        because "no claim" must be distinguishable from "the read failed", and the two
        paths do not report an absent option the same way.
    (b) The ORDERING of claim-then-read-back under two racing writers. This is what bounds
        `R33` to one interleaving, and the honest result is a RATE rather than an assertion:
        with writes at W1,R1,W2,R2 both racers read their own claim and both would proceed.
        That interleaving is the residual; measuring how often it occurs is the point.
    (c) That field 22 of `/proc/<pid>/task/<tid>/stat` is a PER-THREAD start time, and that
        the `/proc` entry DISAPPEARS when the thread dies while the process lives. The
        second property is what makes the claim self-clearing, which is what lets a design
        with no shutdown path be correct anyway. Both are documented Linux behaviour that
        this plan had not run.
    (d) A documented DEGRADED predicate for the non-`/proc` lane. This spike also runs on
        macOS, and silently behaving differently there is the failure mode; saying what is
        lost is the alternative.
    """
    claim = f"{os.getpid()}:{threading.get_native_id()}:1234567"

    # (a) round trip, cross-process read, and both read paths.
    with Server() as s:
        create_session(s, "build")
        wrote = s.run("set-option", "-t", T("build"), PUBLISHER_OPTION, claim)
        fmt = "#{session_name}\t#{" + PUBLISHER_OPTION + "}"
        via_display = s.run("display-message", "-p", "-t", T("build"), fmt)["stdout_raw"]
        via_show = s.run("show-options", "-t", T("build"), "-v", PUBLISHER_OPTION)
        # A DIFFERENT PROCESS reads it. `Server.run` already forks a tmux client, but the
        # reader that matters is a separate Python process -- that is the case the design
        # turns on, so it is the case that gets measured.
        reader = subprocess.run(
            [
                sys.executable,
                "-c",
                "import subprocess,sys;"
                "print(subprocess.run(sys.argv[1:],capture_output=True).stdout.decode(),end='')",
                TMUX,
                "-S",
                s.sock,
                "-f",
                "/dev/null",
                "display-message",
                "-p",
                "-t",
                T("build"),
                fmt,
            ],
            capture_output=True,
            env=ATTACH_ENV,
        )
        cross = reader.stdout.decode(errors="replace")

        # Absent, through both paths. `display-message` expands an unset user option to the
        # empty string at rc=0 (F11's shape); `show-options -v` is measured, not assumed.
        absent_display = s.run(
            "display-message", "-p", "-t", T("build"), "#{session_name}\t#{@shellbox_nosuch}"
        )
        absent_show = s.run("show-options", "-t", T("build"), "-v", "@shellbox_nosuch")

        # Release, which W19b implements only as an optimisation.
        unset = s.run("set-option", "-u", "-t", T("build"), PUBLISHER_OPTION)
        after_unset = s.run("display-message", "-p", "-t", T("build"), fmt)["stdout_raw"]

        display_fields = via_display.split("\n")[0].split("\t")
        cross_fields = cross.split("\n")[0].split("\t")
        oks = [
            expect("S-CLAIM[set-option writes the claim]", wrote["rc"] == 0, wrote["stderr"]),
            expect(
                "S-CLAIM[the claim round-trips through the shipped display-message path]",
                display_fields[:2] == ["build", claim],
                repr(via_display),
            ),
            expect(
                "S-CLAIM[another PROCESS reads the same claim]",
                cross_fields[:2] == ["build", claim],
                repr(cross),
            ),
            expect(
                "S-CLAIM[show-options -v returns the claim too]",
                via_show["rc"] == 0 and via_show["stdout"] == claim,
                f"rc={via_show['rc']} {via_show['stdout']!r} {via_show['stderr']!r}",
            ),
            expect(
                "S-CLAIM[an ABSENT option is an empty field, not an error, via display-message]",
                absent_display["rc"] == 0
                and absent_display["stdout_raw"].split("\n")[0].split("\t")[:2] == ["build", ""],
                f"rc={absent_display['rc']} {absent_display['stdout_raw']!r}",
            ),
            expect(
                "S-CLAIM[set-option -u clears the claim]",
                unset["rc"] == 0 and after_unset.split("\n")[0].split("\t")[1:2] == [""],
                f"rc={unset['rc']} {unset['stderr']!r} after={after_unset!r}",
            ),
        ]
        emit(
            "S-CLAIM",
            "does a publisher claim round-trip through a tmux session option, including "
            "across processes?",
            variant="round_trip",
            claim=claim,
            set_option_rc=wrote["rc"],
            via_display_message=repr(via_display),
            via_show_options_rc=via_show["rc"],
            via_show_options=repr(via_show["stdout"]),
            read_by_another_process=repr(cross),
            absent_via_display_message=repr(absent_display["stdout_raw"]),
            absent_via_show_options_rc=absent_show["rc"],
            absent_via_show_options_stderr=repr(absent_show["stderr"]),
            unset_rc=unset["rc"],
            after_unset=repr(after_unset),
            ok=all(oks),
        )

    # (b) two racing writers, claim-then-read-back, N trials.
    outcomes = {"own+own": 0, "one_own_one_foreign": 0, "other": 0}
    torn = 0
    details: list[str] = []
    with Server() as s:
        create_session(s, "build")
        for trial in range(race_trials):
            s.run("set-option", "-u", "-t", T("build"), PUBLISHER_OPTION)
            base = tempfile.mkdtemp(prefix="sbxclaim")
            trigger = os.path.join(base, "go")
            values = [f"9{trial}0:11{trial}:5550{trial}", f"9{trial}1:22{trial}:6660{trial}"]
            procs = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _CLAIM_RACER,
                        TMUX,
                        s.sock,
                        T("build"),
                        PUBLISHER_OPTION,
                        value,
                        trigger,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=ATTACH_ENV,
                )
                for value in values
            ]
            with open(trigger, "w") as handle:
                handle.write("go")
            said = []
            for proc in procs:
                out, _ = proc.communicate(timeout=30)
                said.append(out.decode(errors="replace").split("\n")[0].split("\t")[0])
            final = s.run("show-options", "-t", T("build"), "-v", PUBLISHER_OPTION)["stdout"]
            if final not in values:
                torn += 1
            if said == ["own", "own"]:
                outcomes["own+own"] += 1
            elif sorted(said) == ["foreign", "own"]:
                outcomes["one_own_one_foreign"] += 1
            else:
                outcomes["other"] += 1
            details.append(f"{said}->{'winner' if final in values else repr(final)}")
            shutil.rmtree(base, ignore_errors=True)
    emit(
        "S-CLAIM",
        "under two racing writers, what does claim-then-read-back actually report?",
        variant="racing_writers",
        trials=race_trials,
        outcomes=outcomes,
        torn_or_foreign_final_values=torn,
        per_trial=details,
        interpretation=(
            "`own+own` is R33 exactly: both publishers read their own claim and both would "
            "attach. The count is the residual's observed rate, not a bound on it -- the "
            "protocol is detection, not mutual exclusion, and tmux offers no compare-and-swap."
        ),
        ok=expect(
            "S-CLAIM[the stored claim is always ONE writer's value, never a torn one]",
            torn == 0,
            f"{torn}/{race_trials} trials left a value belonging to neither writer",
        ),
    )

    # (c)/(d) per-thread identity.
    pid = os.getpid()
    main_tid = threading.get_native_id()
    if sys.platform == "linux":
        stop = threading.Event()
        tids: dict[str, int] = {}

        def worker(label: str) -> None:
            tids[label] = threading.get_native_id()
            stop.wait()

        first = threading.Thread(target=worker, args=("first",), daemon=True)
        first.start()
        # More than one clock tick apart. `starttime` is in ticks (100 Hz on every Linux
        # shellbox targets), so two threads started inside one tick share a value and the
        # measurement would prove nothing.
        time.sleep(0.4)
        second = threading.Thread(target=worker, args=("second",), daemon=True)
        second.start()
        deadline = time.time() + 5.0
        while time.time() < deadline and len(tids) < 2:
            time.sleep(0.01)

        starts = {
            "main": tid_starttime(pid, main_tid),
            "first": tid_starttime(pid, tids.get("first", -1)),
            "second": tid_starttime(pid, tids.get("second", -1)),
        }
        distinct_tids = len({main_tid, *tids.values()}) == 3
        stop.set()
        first.join(timeout=5.0)
        second.join(timeout=5.0)
        # The self-clearing property: the entry goes away when the THREAD dies, while the
        # process -- and this very interpreter -- keeps running.
        gone_deadline = time.time() + 5.0
        first_path = f"/proc/{pid}/task/{tids.get('first', -1)}"
        while time.time() < gone_deadline and os.path.exists(first_path):
            time.sleep(0.02)
        entry_gone = not os.path.exists(first_path)
        process_still_live = os.path.exists(f"/proc/{pid}/task/{main_tid}")

        oks = [
            expect("S-CLAIM[three distinct native tids]", distinct_tids, str([main_tid, *tids])),
            expect(
                "S-CLAIM[field 22 is readable for every thread]",
                all(v is not None for v in starts.values()),
                str(starts),
            ),
            expect(
                "S-CLAIM[field 22 is PER-THREAD, not per-process]",
                starts["first"] is not None
                and starts["second"] is not None
                and starts["second"] > starts["first"],
                f"{starts} -- a thread started 0.4 s later must carry a later starttime",
            ),
            expect(
                "S-CLAIM[a dead thread's /proc entry disappears while the process lives]",
                entry_gone and process_still_live,
                f"entry_gone={entry_gone} process_live={process_still_live}",
            ),
        ]
        emit(
            "S-CLAIM",
            "is field 22 of /proc/<pid>/task/<tid>/stat a per-thread start time, and does "
            "the entry vanish when the thread dies?",
            variant="proc_per_thread_starttime",
            platform=sys.platform,
            pid=pid,
            tids={"main": main_tid, **tids},
            starttimes=starts,
            clock_ticks_per_second=os.sysconf("SC_CLK_TCK"),
            dead_thread_entry_gone=entry_gone,
            process_entry_still_present=process_still_live,
            predicate=(
                "a claim (pid, tid, tid_starttime) is STALE when /proc/<pid>/task/<tid> is "
                "absent or its field 22 differs; otherwise it is held"
            ),
            ok=all(oks),
        )
    else:
        # The degraded lane, documented rather than silently different.
        alive = True
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
        emit(
            "S-CLAIM",
            "what claim predicate is available where /proc does not exist?",
            variant="degraded_predicate_no_proc",
            platform=sys.platform,
            proc_task_dir_present=os.path.exists(f"/proc/{pid}/task"),
            pid_liveness_probe="os.kill(pid, 0)",
            pid_liveness_result=alive,
            predicate=(
                "PID LIVENESS ONLY: a claim is stale when os.kill(pid, 0) raises. The "
                "tid and tid_starttime are recorded but CANNOT be evaluated."
            ),
            limitation=(
                "This is strictly weaker and reinstates the hazard the tid was introduced "
                "for: a publisher thread that died inside a still-running process leaves a "
                "claim naming a LIVE pid, so no publisher serves that session again for the "
                "rest of that process's life. macOS is a developer lane only -- the sandbox "
                "is Ubuntu 24.04 -- so the degradation is acceptable there and would not be "
                "in production."
            ),
            ok=expect(
                "S-CLAIM[the non-/proc lane really has no /proc to fall back on]",
                not os.path.exists(f"/proc/{pid}/task"),
                "a /proc task directory exists on a platform this branch assumes lacks one",
            ),
        )


def check_self() -> None:
    """Enforce the plan's own rule on this file: no bare `=name` target anywhere.

    The earlier version of this spike used `-t '=build'` for kill-session and
    has-session -- the exact form §7.1 forbids -- which is almost certainly where
    the plan's §9.2 race table inherited it. A regression suite must not violate
    the rule it exists to guard.
    """
    # Scope matters: check_b2/check_b3 pass bare `=name` ON PURPOSE -- proving the
    # form is unsafe IS their job. Only the NORMATIVE checks, the ones the plan
    # transcribes §7 from, must be clean.
    import inspect

    normative = [
        check_chain_verbatim,
        check_composition,
        check_b4,
        check_h4,
        # W2's additions. They describe forms the adapter actually issues, so they are
        # normative and must obey the rule too.
        check_display_message_multifield,
        check_stderr_signatures,
        check_adapter_forms,
        check_locale_tab_dependence,
        # W14's additions. Normative for the same reason: W15's `attach_argv`, W15's
        # per-window `window-size manual`, W19's resync, and W19b's claim are all
        # transcribed FROM these, so a bare `=name` here would be copied into a module.
        check_s_attach,
        check_s_attach_mechanism,
        check_s_attach_input,
        check_s_pane_dead,
        check_s_pipe,
        check_s_claim,
        create_session,
        read_size,
        read_attached,
        # The attach argv itself, which W15 builds in `tmux.py`. omnigent's attach passes an
        # UNANCHORED `-t` (`ws_bridge.py:492`) -- precisely the form `target.py` forbids --
        # so this is the one place the rule is most likely to be broken by copying.
        Attach.__init__,
    ]
    offenders = {}
    for fn in normative:
        src = inspect.getsource(fn)
        bad = re.findall(r'"=[A-Za-z0-9_{}<>\[\]().-]*(?<!:)"', src)
        if bad:
            offenders[fn.__name__] = bad
    emit(
        "SELF",
        "do the NORMATIVE checks avoid the forbidden bare `=name` target form?",
        functions_scanned=[f.__name__ for f in normative],
        offenders=offenders,
        ok=expect("SELF[no bare =name in normative checks]", not offenders, str(offenders)),
    )


def main() -> int:
    emit(
        "ENV",
        "what is under test?",
        tmux_version=tmux_version(),
        platform=sys.platform,
        tmux_bin=TMUX,
    )
    check_self()
    check_b1()
    check_b2()
    check_b3()
    check_b4()
    check_h4()
    check_cwd_injection()
    check_chain_verbatim()
    check_composition()
    # W2's additions (F11-F14).
    check_display_message_multifield()
    check_stderr_signatures()
    check_socket_path_limit()
    check_adapter_forms()
    check_locale_tab_dependence()
    # W14 (Phase 3). S-ATTACH runs first because its first variant is the gate: a negative
    # result there flips Decision A to a `pipe-pane` sink and changes W15, W19, ADR-9 and
    # ADR-10, so it should be the first thing a reader of this output sees fail.
    check_s_attach()
    check_s_attach_mechanism()
    check_s_attach_input()
    check_s_pane_dead()
    check_s_pipe()
    check_s_claim()

    emit(
        "SUMMARY",
        "did every assertion hold?",
        failures=FAILURES,
        failure_count=len(FAILURES),
        ok=not FAILURES,
    )
    if FAILURES:
        print(
            f"\nFAILED: {len(FAILURES)} assertion(s)\n  " + "\n  ".join(FAILURES),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
