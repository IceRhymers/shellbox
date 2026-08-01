"""W3 -- input delivery against a REAL tmux, with the H4 oracle SPLIT (§11.3).

Two reader panes, testing two different things, and they must never be unified:

* **raw mode** (``stty -icanon``) -- the only oracle for *byte-exact delivery*, because it is
  the only mode in which full delivery of a long line is possible at all.
* **canonical mode** (plain ``cat``) -- the oracle for *the guard*: a line at or over
  ``SHELLBOX_MAX_SEND_LINE_BYTES`` must be refused before tmux is invoked, so the bytes the
  line discipline would destroy never reach the pty.

WARNING: The tempting "fix" if a byte-exactness assertion ever fails is to add ``stty -icanon`` to
the canonical case. That deletes the second property, keeps CI green, and leaves production
dropping bytes on macOS and TRUNCATING them on Linux -- rc=0 both ways.
``test_the_h4_hazard_is_real_in_this_lane`` is the assertion that keeps the reason executable.

Byte-exactness is read from the FILE the pane process wrote, never from ``capture-pane``: the
pane renders, wraps and normalises. Synchronization is always sentinel-and-poll, never sleep.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import (
    TmuxServer,
    await_file_bytes,
    canonical_reader,
    fail_verb,
    raw_reader,
    requires_tmux,
)
from shellbox_mcp.errors import LineTooLong, TmuxError

pytestmark = requires_tmux

# A single line comfortably over H3's ~16 KB client-to-server command cap. Delivering it
# proves two things at once: the payload travels on load-buffer's STDIN (an argv this long
# would be refused), and raw mode really is lossless at a length canonical mode destroys.
BIG_LINE_BYTES = 20_000

# The raw-mode cases need the guard out of the way -- 20 KB on one line is `line_too_long`
# under the shipping default of 1000, which is the correct production behaviour.
#
# CRITICAL: Raising the limit is legitimate ONLY together with a raw-mode reader, and the two must
# be changed as a pair. A raised limit pointed at a canonical-mode pane is not a test setting: it
# is the production hazard, reproduced below and measured in both lanes as spike F5.
RAW_MODE_LIMIT = 1 << 20


BYTE_EXACT_CASES: dict[str, str] = {
    # H1, the whole reason `text` does not use `send-keys -l`: a lone `;` returns rc=0 there
    # and NEVER ARRIVES (tmux eats it as its command separator; `--` does not help).
    "lone_semicolon": ";",
    "three_semicolons": ";;;",
    # H2: text beginning with `-` parses as a flag. Through a buffer it is just bytes.
    "leading_dash_n": "-n not a flag\n",
    # H4's raw-mode half: over 16 KB on ONE line, byte-exact.
    "over_16kb_single_line": "x" * BIG_LINE_BYTES + "\n",
    # Multi-byte UTF-8 -- the payload the forced LC_CTYPE exists for. Combining marks and a
    # 4-byte astral character included: a per-character path would mangle these.
    "multibyte_utf8": "héllo → 日本語 🎉 é ü\n",
    # Control bytes, including the ones a naive escaping layer would rewrite. NUL is here
    # deliberately: it is the byte a C-string-shaped implementation truncates at.
    "control_bytes": "\x01\x02\x07\x0b\x0c\x1b[31mred\x1b[0m\x7f\x00after-nul\n",
}


@pytest.mark.parametrize("case", sorted(BYTE_EXACT_CASES), ids=sorted(BYTE_EXACT_CASES))
def test_send_text_is_byte_exact_at_a_raw_mode_pane(
    tmux_server: TmuxServer, tmp_path, case: str
) -> None:
    """The full W3 matrix, compared against the bytes the pane PROCESS received.

    ``stty -icanon`` and nothing more, deliberately: ``paste-buffer`` translates LF to CR
    unless given ``-r``, and it is the pane's ``icrnl`` that turns it back into the LF the
    payload contained (M25). ``stty raw`` would clear ``icrnl`` too and every newline would
    arrive as ``\\r`` -- a failure with nothing to do with shellbox.
    """
    text = BYTE_EXACT_CASES[case]
    expected = text.encode("utf-8")
    out = str(tmp_path / "delivered")
    adapter = tmux_server.adapter(max_send_line_bytes=RAW_MODE_LIMIT)
    adapter.create("build", cwd=str(tmp_path), command=raw_reader(out))

    sent = adapter.send("build", text=text)
    assert sent.submitted_bytes == len(expected)
    # `submitted_bytes` counts bytes handed to paste-buffer. Per H4 the bytes reaching the
    # pane process are not knowable to shellbox, which is what this test measures INSTEAD --
    # from outside the adapter, so the adapter's own belief is never the oracle.
    assert sent.delivery == "unverified"

    delivered = await_file_bytes(out, len(expected), timeout=15.0)
    assert delivered == expected, f"{case}: delivered {len(delivered)} of {len(expected)} bytes" + (
        " -- the `;` was swallowed" if delivered == b"" and text.startswith(";") else ""
    )


def test_the_lone_semicolon_arrives_and_no_send_keys_dash_l_was_used(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The two halves of H1 in one test: the character arrives, and ``-l`` was not involved.

    ``send-keys -l ';'`` is rc=0 with nothing delivered, so "it arrived" is the only
    observation that distinguishes the buffer path from the path issue #2 originally specified.
    Asserting the absence of ``-l`` at the same time ties the outcome to the mechanism -- the
    structural version, over every branch, is in ``tests/unit/test_send_input_delivery.py``.
    """
    out = str(tmp_path / "delivered")
    adapter, spy = tmux_server.spied_adapter()
    adapter.create("build", cwd=str(tmp_path), command=raw_reader(out))
    adapter.send("build", text=";", keys=["Enter"])

    assert await_file_bytes(out, 2) == b";\n"
    for argv in spy.argvs:
        assert "-l" not in argv, f"send-keys -l reached tmux: {argv}"
    assert any("load-buffer" in argv for argv in spy.argvs)
    send_keys = next(argv for argv in spy.argvs if "send-keys" in argv)
    assert send_keys[-2:] == ("--", "Enter"), "keys carry names only"


# --------------------------------------------------------------------------------------
# The canonical-mode half: the guard, never delivery
# --------------------------------------------------------------------------------------


def test_an_over_long_line_at_a_canonical_pane_is_refused_without_invoking_tmux(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """``line_too_long``, zero tmux invocations, and nothing at the pane.

    The negative ("nothing was delivered") is proved by a SENTINEL rather than by waiting:
    a legal payload is sent afterwards and awaited, and ordering is guaranteed (M18), so the
    sentinel's arrival means the rejected payload would already have arrived if it had been
    sent at all. No sleep, and no "we waited 2 seconds and hoped".
    """
    out = str(tmp_path / "canonical")
    tmux_server.adapter().create("build", cwd=str(tmp_path), command=canonical_reader(out))

    # A separate, spied adapter so `calls == []` is a statement about the send alone.
    adapter, spy = tmux_server.spied_adapter()
    with pytest.raises(LineTooLong) as excinfo:
        adapter.send("build", text="x" * 1000 + "\n")
    assert spy.calls == [], f"tmux was invoked for a rejected line: {spy.argvs}"
    assert "1000 bytes" in excinfo.value.message
    assert excinfo.value.code == "line_too_long"

    sentinel = f"SENTINEL-{uuid.uuid4().hex[:8]}\n"
    adapter.send("build", text=sentinel)
    delivered = await_file_bytes(out, len(sentinel.encode()))
    assert delivered == sentinel.encode(), (
        "the canonical pane received something other than the sentinel: "
        f"{delivered[:80]!r} -- the rejected line must never have been submitted"
    )


def test_a_line_just_under_the_limit_is_delivered_even_in_canonical_mode(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The limit sits BELOW the hazard, not on top of it.

    999 bytes plus LF is under macOS's 1023 and Linux's 4096 MAX_CANON, so it survives even
    the line discipline that destroys 1024. Without this, a limit set to 1 would satisfy every
    other assertion in this file -- and refuse every real command.
    """
    out = str(tmp_path / "canonical")
    adapter = tmux_server.adapter()
    adapter.create("build", cwd=str(tmp_path), command=canonical_reader(out))
    payload = "u" * 999 + "\n"
    adapter.send("build", text=payload)
    assert await_file_bytes(out, 1000) == payload.encode()


def test_the_h4_hazard_is_real_in_this_lane(tmux_server: TmuxServer, tmp_path) -> None:
    """Prove the guard is necessary by reproducing, with raw tmux, what it prevents.

    Without this the guard looks like defensive padding and the next person raises the limit.
    Raw tmux is used deliberately: the adapter would (correctly) refuse this payload.

    The loss is asserted; its SHAPE is asserted against both measured possibilities, because
    it differs by platform and both are bad -- macOS discards the entire line (0 bytes), Linux
    (the sandbox) silently truncates to 4096, which is the worse of the two because a
    truncated command is a different, still-executable command.

    WARNING: **The one assertion in this suite that needs a deadline**, because it is a claim that
    something never arrives, and no condition can be polled for that. It fails in the safe
    direction: a timeout waiting for completeness IS the property holding, and the loss is
    deterministic in both lanes (spike F5, which is the oracle for it). The ordinary
    sentinel-and-poll model still supplies the *positive* half -- the pane is proved to be
    delivering before the over-long line is pasted.

    Also measured here, and NOT in F5 (which pasted one line into a fresh pane): on macOS the
    pane accepts **nothing further** after the overflow -- a sentinel pasted afterwards never
    arrives either. So an over-long line does not merely lose itself; it can wedge the pane's
    input path. On Linux the pane keeps working after truncating.
    """
    out = str(tmp_path / "canonical")
    tmux_server.adapter().create("build", cwd=str(tmp_path), command=canonical_reader(out))

    def paste(payload: bytes) -> None:
        buffer_name = f"probe-{uuid.uuid4().hex[:8]}"
        assert tmux_server.raw("load-buffer", "-b", buffer_name, "-", stdin=payload).rc == 0
        assert tmux_server.raw("paste-buffer", "-d", "-b", buffer_name, "-t", "=build:").rc == 0

    # The positive half, polled: the pane is alive and delivering BEFORE the hazard is applied,
    # so a subsequent non-delivery cannot be explained by a pane that never started.
    alive = b"ALIVE\n"
    paste(alive)
    await_file_bytes(out, len(alive))

    long_line = b"L" * 8192 + b"\n"
    paste(long_line)
    complete = len(alive) + len(long_line)
    deadline = time.monotonic() + 2.0
    delivered = b""
    while time.monotonic() < deadline:
        with open(out, "rb") as handle:
            delivered = handle.read()
        if len(delivered) >= complete:
            break
        time.sleep(0.05)

    corrupted = delivered[len(alive) :]
    assert corrupted != long_line, (
        "canonical mode delivered an 8192-byte line intact; H4 no longer reproduces here, so "
        "the line-length guard's rationale needs re-measuring (spike F5 is the oracle) -- do "
        "NOT respond by raising SHELLBOX_MAX_SEND_LINE_BYTES"
    )
    assert len(corrupted) in (0, 4096), (
        f"expected a drop (0 bytes, macOS) or a truncation to 4096 (Linux), got {len(corrupted)}"
    )


# --------------------------------------------------------------------------------------
# Concurrency: 32 agents, one server, one session
# --------------------------------------------------------------------------------------


def test_concurrent_sends_use_distinct_buffers_and_no_payload_is_lost(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The pool case: 32 concurrent sends into ONE session must not clobber one another.

    A shared buffer name is the failure this guards: agent A's ``load-buffer`` would overwrite
    the payload agent B had just loaded and not yet pasted, so B's paste would deliver A's
    input into B's pane -- rc=0, no error anywhere, and a confidentiality breach as well as a
    correctness one. Names are per-call UUIDs, and this is the assertion that they are.

    Line ORDER across threads is not asserted (nothing guarantees it); that each line arrives
    exactly once and unmixed is (M29: concurrent pastes do not interleave).
    """
    out = str(tmp_path / "delivered")
    adapter, spy = tmux_server.spied_adapter()
    adapter.create("build", cwd=str(tmp_path), command=raw_reader(out))

    agents = 32
    lines = [f"agent-{index:02d}-{'p' * 20}\n" for index in range(agents)]
    with ThreadPoolExecutor(max_workers=agents) as pool:
        submitted = list(pool.map(lambda line: adapter.send("build", text=line), lines))
    assert [s.submitted_bytes for s in submitted] == [len(line.encode()) for line in lines]

    expected_bytes = sum(len(line.encode()) for line in lines)
    delivered = await_file_bytes(out, expected_bytes, timeout=20.0)
    assert sorted(delivered.decode().splitlines(keepends=True)) == sorted(lines), (
        "a concurrent send lost, duplicated or interleaved a payload"
    )

    buffer_names = spy.values_after("-b", "load-buffer")
    assert len(buffer_names) == agents
    assert len(set(buffer_names)) == agents, (
        f"buffer names collided under concurrency: {buffer_names}"
    )
    # `buffer-limit` is 50 server-wide across every pooled agent, so 32 concurrent sends must
    # leave nothing behind or they evict each other's -- and each other's is arbitrary input.
    assert tmux_server.raw("list-buffers").stdout_raw.strip() == ""


# --------------------------------------------------------------------------------------
# Fault injection on a live server
# --------------------------------------------------------------------------------------


def test_a_failed_paste_leaves_no_buffer_behind_on_a_real_server(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """``paste-buffer -d`` deletes only on SUCCESS, so the failure path must delete it itself.

    Faulted at the runner, after a REAL ``load-buffer`` has really created a buffer -- which is
    the only way to observe the leak. It is not hygiene: ``buffer-limit`` defaults to 50
    server-wide across all pooled agents, so a leaked buffer both evicts other agents' buffers
    and retains arbitrary agent input, the exact confidentiality concern ``-d`` exists for.
    """
    out = str(tmp_path / "delivered")
    tmux_server.adapter().create("build", cwd=str(tmp_path), command=raw_reader(out))

    adapter, spy = tmux_server.spied_adapter(fault=fail_verb("paste-buffer", "paste-buffer: boom"))
    with pytest.raises(TmuxError) as excinfo:
        adapter.send("build", text="secret-payload\n")
    assert "paste-buffer: boom" in excinfo.value.message
    # No SendResult exists to report bytes: nothing on this path may claim a submission.
    assert excinfo.value.code == "tmux_error"

    loaded = spy.values_after("-b", "load-buffer")
    deleted = spy.values_after("-b", "delete-buffer")
    assert loaded == deleted, f"loaded {loaded} but deleted {deleted}"
    assert tmux_server.raw("list-buffers").stdout_raw.strip() == "", (
        "the buffer survived a failed paste: it now holds agent input and counts against "
        "the server-wide buffer-limit of 50"
    )

    # And the server is still usable afterwards -- a cleanup path that wedged the session
    # would trade a leaked buffer for a dead agent.
    healthy, _ = tmux_server.spied_adapter()
    healthy.send("build", text="ok\n")
    assert await_file_bytes(out, 3) == b"ok\n", "the faulted send must not have delivered"


def test_a_failed_load_buffer_creates_nothing_to_clean_up(
    tmux_server: TmuxServer, tmp_path
) -> None:
    """The other half of the two-operation send path: fail at step one.

    Asserted because the cleanup is conditional. If ``load-buffer`` fails there is no buffer,
    and issuing ``delete-buffer`` anyway would log a spurious warning about someone else's
    missing buffer on every such failure.
    """
    out = str(tmp_path / "delivered")
    tmux_server.adapter().create("build", cwd=str(tmp_path), command=raw_reader(out))

    adapter, spy = tmux_server.spied_adapter(fault=fail_verb("load-buffer", "load-buffer: boom"))
    with pytest.raises(TmuxError):
        adapter.send("build", text="never-loaded\n")
    assert all("paste-buffer" not in argv for argv in spy.argvs), "nothing may be pasted"
    assert all("delete-buffer" not in argv for argv in spy.argvs), "nothing to delete"
    assert tmux_server.raw("list-buffers").stdout_raw.strip() == ""
