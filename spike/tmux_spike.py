#!/usr/bin/env python3
"""Executable spike for shellbox Phase 2 (issue #2), section 7 — the tmux adapter.

Purpose: settle B1-B4 from the iteration-2 architect review by RUNNING the command
compositions the plan prescribes, rather than reasoning about them. The plan's
measurement appendix tests fragments; every defect found so far lived in the
composition. This runs compositions.

Emits one JSON object per check to stdout (JSONL). Run under two tmux versions:

    python3 spike/tmux_spike.py                     # local (3.6b)
    docker run --rm -v "$PWD:/w" -w /w ubuntu:24.04 \
        sh -c 'apt-get update -qq && apt-get install -y -qq tmux python3 \
               && python3 spike/tmux_spike.py'      # 3.4

Section 7 of the plan should then be transcribed FROM this output.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

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
