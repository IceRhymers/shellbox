"""W3 -- input-delivery correctness, in the lane where "tmux was never called" is assertable.

Three properties live here, and none of them can be established against a real server:

1. **The ``line_too_long`` boundary is bytes since the last newline** -- not total payload
   size, not characters. A 1 MB payload of short lines is fine; one 1100-byte line is not.
2. **A rejected line reaches tmux not at all.** Against a real server the closest observable
   is "the pane's file stayed empty", which a *delivered-then-destroyed* line also satisfies
   -- and destruction is precisely H4's failure mode (dropped on macOS, TRUNCATED on Linux,
   rc=0 everywhere). Only a recording runner can tell the two apart.
3. **``text`` never goes anywhere near ``send-keys -l``**, over every branch of the module
   rather than the branches these tests happen to execute -- hence the AST assertions.

The delivery half of the matrix (byte-exactness) is in ``tests/tmux/test_send_delivery.py``,
against a raw-mode pane, because bytes are the only oracle for bytes.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from conftest import RecordingRunner, result
from shellbox_mcp import tmux as tmux_module
from shellbox_mcp.errors import LineTooLong, TooLarge, UnencodableText
from shellbox_mcp.tmux import TmuxAdapter, TmuxConfig

SOCKET = "/tmp/sbx-send.sock"

# One resolve reply that satisfies `_resolve_owned`: session present, incarnation non-empty.
RESOLVED = result(rc=0, stdout="build\tINC-1\n")

TMUX_SOURCE = Path(inspect.getsourcefile(tmux_module) or "").read_text()
TMUX_TREE = ast.parse(TMUX_SOURCE)


def adapter(runner: RecordingRunner, **overrides: object) -> TmuxAdapter:
    settings: dict[str, object] = {"socket_path": SOCKET}
    settings.update(overrides)
    return TmuxAdapter(TmuxConfig(**settings), runner=runner)  # type: ignore[arg-type]


def sending(**overrides: object) -> tuple[TmuxAdapter, RecordingRunner]:
    runner = RecordingRunner(default=RESOLVED)
    return adapter(runner, **overrides), runner


# --------------------------------------------------------------------------------------
# The default, and why it is 1000
# --------------------------------------------------------------------------------------


def test_the_default_line_limit_is_below_both_platforms_thresholds() -> None:
    """1000, and the number is not arbitrary (§8's H4 table, spike F5).

    Measured: macOS discards an entire line over 1023 bytes; Linux -- the sandbox --
    silently truncates to 4096. A default under the STRICTER threshold is what makes a
    developer's Mac and the sandbox behave identically, so neither failure mode can fire and
    neither can be discovered only in production. 4000 would be safe on the sandbox and
    silently lossy on a Mac.
    """
    assert TmuxConfig(socket_path=SOCKET).max_send_line_bytes == 1000
    assert TmuxConfig(socket_path=SOCKET).max_send_line_bytes < 1024, "macOS MAX_CANON"
    assert TmuxConfig(socket_path=SOCKET).max_send_line_bytes < 4096, "Linux MAX_CANON"
    # The total-bytes guard is a tmux-server memory guard and is orders of magnitude larger:
    # if the two were ever comparable, `line_too_long` would be unreachable behind `too_large`.
    assert TmuxConfig(socket_path=SOCKET).max_send_bytes == 1 << 20


# --------------------------------------------------------------------------------------
# The boundary: bytes since the last newline
# --------------------------------------------------------------------------------------


def test_the_limit_is_the_first_rejected_length() -> None:
    """``>=``, not ``>``: the limit is specified as the first REJECTED length (§11.3).

    Both sides asserted, because an off-by-one here is invisible -- it does not fail loudly,
    it lets exactly one length through into a line discipline measured to destroy it.
    """
    ok_adapter, ok_runner = sending()
    ok_adapter.send("build", text="x" * 999)
    assert any("paste-buffer" in argv for argv in ok_runner.argvs), "999 must be delivered"

    over_adapter, over_runner = sending()
    with pytest.raises(LineTooLong):
        over_adapter.send("build", text="x" * 1000)
    assert over_runner.calls == [], "1000 must not reach tmux at all"


def test_a_megabyte_of_short_lines_is_delivered() -> None:
    """The distinction the boundary rests on, from the permissive side.

    Total size is NOT the correctness quantity: MAX_CANON applies per line, and the pane's
    reader consumes each line as it is terminated. Guarding total bytes instead would reject
    every legitimate large paste while still admitting the one 1100-byte line that corrupts.
    """
    ad, runner = sending()
    payload = ("y" * 99 + "\n") * 10_000  # 1,000,000 bytes; every line 99 + LF
    assert len(payload.encode()) == 1_000_000
    sent = ad.send("build", text=payload)
    assert sent.submitted_bytes == 1_000_000
    load = next(stdin for argv, stdin in runner.calls if "load-buffer" in argv)
    assert load == payload.encode(), "the whole payload goes to load-buffer's stdin"


def test_one_over_long_line_inside_a_short_payload_is_rejected() -> None:
    """...and the same distinction from the strict side: 1100 bytes total, all one line.

    Paired with the test above on purpose. A guard on total bytes would accept this and
    reject that one -- exactly backwards.
    """
    ad, runner = sending()
    with pytest.raises(LineTooLong) as excinfo:
        ad.send("build", text="short\n" + "z" * 1100 + "\nshort\n")
    assert "1100 bytes" in excinfo.value.message
    assert runner.calls == []


def test_an_unterminated_final_line_still_counts() -> None:
    """The last segment has no trailing LF, and it is still a line the pty will assemble.

    ``text`` without a trailing newline is the normal case (``text`` + ``keys=["Enter"]``), so
    a guard that only measured *terminated* lines would miss the single most common shape.
    """
    ad, runner = sending()
    with pytest.raises(LineTooLong):
        ad.send("build", text="a\nb\n" + "q" * 5000)
    assert runner.calls == []


def test_the_limit_counts_BYTES_not_characters() -> None:
    """Multi-byte UTF-8: MAX_CANON is a byte count, so the guard must be one too.

    400 U+00E9 is 400 characters and 800 bytes -- under. 600 is 1200 bytes -- over. A
    character-counting guard would pass the second straight into the line discipline.
    """
    ok_adapter, ok_runner = sending()
    ok_adapter.send("build", text="é" * 400)
    assert any("paste-buffer" in argv for argv in ok_runner.argvs)

    over_adapter, over_runner = sending()
    with pytest.raises(LineTooLong) as excinfo:
        over_adapter.send("build", text="é" * 600)
    assert "1200 bytes" in excinfo.value.message
    assert over_runner.calls == []


def test_the_limit_is_configurable_and_the_guard_uses_it() -> None:
    """The tests above pin the default; this pins that the value is actually consulted.

    ``SHELLBOX_MAX_SEND_LINE_BYTES`` resolves into this field, so a limit read from the
    environment and then ignored is a failure mode worth one assertion.
    """
    strict_adapter, strict_runner = sending(max_send_line_bytes=10)
    with pytest.raises(LineTooLong):
        strict_adapter.send("build", text="0123456789")
    assert strict_runner.calls == []

    loose_adapter, loose_runner = sending(max_send_line_bytes=1 << 16)
    loose_adapter.send("build", text="x" * 20_000)
    assert any("paste-buffer" in argv for argv in loose_runner.argvs)


def test_too_large_is_the_total_guard_and_line_too_long_is_the_delivery_guard() -> None:
    """Two guards, two quantities, two codes -- and the codes must not be swapped.

    ``too_large`` protects the tmux server's memory; it says nothing about delivery.
    ``line_too_long`` is the correctness boundary. A caller that sees ``too_large`` should
    split its payload; one that sees ``line_too_long`` must add newlines, which is different
    advice, so conflating them would mislead every retry.
    """
    big_adapter, big_runner = sending(max_send_bytes=500)
    with pytest.raises(TooLarge):
        big_adapter.send("build", text="w\n" * 400)  # 800 bytes, every line 1 + LF
    assert big_runner.calls == []

    long_adapter, _ = sending(max_send_bytes=1 << 20)
    with pytest.raises(LineTooLong):
        long_adapter.send("build", text="w" * 1000)


# --------------------------------------------------------------------------------------
# The guard the path was missing: text that has no UTF-8 encoding
# --------------------------------------------------------------------------------------


def test_unencodable_text_is_rejected_in_the_taxonomy_before_tmux() -> None:
    """A lone surrogate reaches the adapter as a ``str`` with no UTF-8 encoding.

    An MCP client can produce one: ``json.loads('"\\ud800"')`` is exactly this string. Before
    the guard, ``text.encode("utf-8")`` raised a bare ``UnicodeEncodeError`` -- outside the §6
    taxonomy, from a tool whose documented failures are all structured payloads.

    Rejection rather than coercion is the point, and it is the same rule as H4: never deliver
    bytes the caller did not send. ``errors="replace"`` would paste U+FFFD; ``"surrogatepass"``
    would paste bytes that are not UTF-8 at all.
    """
    ad, runner = sending()
    with pytest.raises(UnencodableText) as excinfo:
        ad.send("build", text="ls \ud800\n")
    assert runner.calls == [], "unencodable text must not reach tmux"
    # Inside the closed set (§6 fixes shell_send's codes), so W4's error taxonomy is unchanged.
    assert excinfo.value.code == "tmux_error"
    assert "not encodable as UTF-8" in excinfo.value.message
    assert excinfo.value.session == "build"


def test_well_formed_multibyte_and_control_bytes_are_not_disturbed_by_that_guard() -> None:
    """The guard rejects only what has no encoding: everything else passes through verbatim."""
    ad, runner = sending()
    payload = "héllo → 日本語 🎉 \x01\x07\x1b[31m\x7f\n"
    ad.send("build", text=payload)
    load = next(stdin for argv, stdin in runner.calls if "load-buffer" in argv)
    assert load == payload.encode("utf-8")


# --------------------------------------------------------------------------------------
# text goes through the buffer, NEVER through `send-keys -l`
# --------------------------------------------------------------------------------------


def test_no_send_keys_invocation_in_tmux_py_carries_dash_l() -> None:
    """Asserted over the AST, so it holds for branches these tests never execute.

    ``send-keys -l ';'`` returns **rc=0 and the character never arrives** (H1/M1: tmux
    consumes a standalone ``;`` as its command separator, and ``--`` does not help), and text
    beginning with ``-`` parses as a flag (H2). Both failures are silent at the argv layer,
    which is why the rule is structural rather than a behavioral spot-check.
    """
    send_keys_sequences = 0
    for node in ast.walk(TMUX_TREE):
        elements: list[ast.expr] = []
        if isinstance(node, ast.List | ast.Tuple):
            elements = list(node.elts)
        elif isinstance(node, ast.Call):
            elements = list(node.args)
        if not any(isinstance(e, ast.Constant) and e.value == "send-keys" for e in elements):
            continue
        send_keys_sequences += 1
        literals = [e.value for e in elements if isinstance(e, ast.Constant)]
        assert "-l" not in literals, f"tmux.py line {node.lineno}: send-keys built with -l"
        # `--` instead: it fixes H2 for key NAMES, which is all send-keys carries here.
        assert "--" in literals, f"tmux.py line {node.lineno}: send-keys without --"

    assert send_keys_sequences == 1, (
        f"expected exactly one send-keys argv in tmux.py, found {send_keys_sequences}; "
        "a second one is a new delivery path that this rule has not been applied to"
    )


def test_dash_l_is_not_a_string_constant_anywhere_in_tmux_py() -> None:
    """The same rule again, immune to how the argv is assembled.

    ``-l`` has exactly one meaning to tmux in this module's vocabulary: ``send-keys``'s
    literal-text flag. Nothing here needs it, so its presence at all is the defect.
    """
    offenders = [
        node.lineno
        for node in ast.walk(TMUX_TREE)
        if isinstance(node, ast.Constant) and node.value == "-l"
    ]
    assert offenders == [], f"tmux.py uses the send-keys literal flag at lines {offenders}"


def test_the_payload_reaches_tmux_only_as_stdin_never_as_an_argument() -> None:
    """Runtime half: with text AND keys sent together, no argv contains the payload.

    Three properties in one assertion. The payload is not in any argv, so it cannot be read
    out of the process table (it is agent input, potentially credentials) and it cannot hit
    H3's ~16 KB client-to-server command cap; and ``send-keys`` carries key names only.
    """
    ad, runner = sending()
    payload = "echo secret-abc ; -n"
    ad.send("build", text=payload, keys=["Enter"])

    for argv, _ in runner.calls:
        assert all("secret-abc" not in item for item in argv), f"payload leaked into argv: {argv}"
        assert "-l" not in argv

    load_argv, load_stdin = next(c for c in runner.calls if "load-buffer" in c[0])
    assert load_argv[-1] == "-", "load-buffer must read the payload from stdin"
    assert load_stdin == payload.encode()
    send_keys = next(argv for argv in runner.argvs if "send-keys" in argv)
    assert send_keys[-2:] == ("--", "Enter")


def test_keys_only_sends_never_touch_the_buffer_path() -> None:
    """And the converse: a keys-only send loads no buffer, so it cannot leak one."""
    ad, runner = sending()
    ad.send("build", keys=["C-c"])
    assert all("load-buffer" not in argv for argv in runner.argvs)
    assert all("paste-buffer" not in argv for argv in runner.argvs)


# --------------------------------------------------------------------------------------
# Per-call buffer names (the concurrent proof is in tests/tmux/)
# --------------------------------------------------------------------------------------


def test_every_send_uses_a_fresh_buffer_name_across_adapter_instances() -> None:
    """Required, not stylistic, and the requirement is about SEPARATE processes.

    Up to 32 pooled agents share one tmux server, each in its own MCP process, so uniqueness
    must not come from anything an instance remembers -- a per-adapter counter would collide
    across processes the moment two of them sent their first payload. The names are UUIDs,
    which is why fresh adapters are used here rather than one.
    """
    names: list[str] = []
    for _ in range(20):
        ad, runner = sending()
        ad.send("build", text="payload\n")
        argv = next(argv for argv in runner.argvs if "load-buffer" in argv)
        names.append(argv[argv.index("-b") + 1])

    assert len(set(names)) == 20, f"buffer names collided: {names}"
    assert all(name.startswith("shellbox-") for name in names), names
    # And the paste and the delete must name the SAME buffer as the load, or a "cleanup"
    # would delete some other agent's buffer while leaking its own.
    ad, runner = sending()
    ad.send("build", text="payload\n")
    load = next(argv for argv in runner.argvs if "load-buffer" in argv)
    paste = next(argv for argv in runner.argvs if "paste-buffer" in argv)
    assert load[load.index("-b") + 1] == paste[paste.index("-b") + 1]
