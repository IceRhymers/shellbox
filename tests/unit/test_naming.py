"""Boundary validation, including the socket-path limit measured against the real platform."""

from __future__ import annotations

import os
import socket
import sys

import pytest
from shellbox_mcp import naming
from shellbox_mcp.errors import BadCwd, InvalidDimensions, InvalidName, SocketPathTooLong


@pytest.mark.parametrize("name", ["build", "b", "a" * 64, "a.b_c-1", "0build", "A1"])
def test_valid_session_names(name: str) -> None:
    assert naming.validate_session_name(name) == name


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("=bui", "an anchored target is not a name -- §7.1's adapter-level assertion"),
        ("=build:", "nor is a fully anchored one"),
        ("build:1", "`:` is tmux's session:window.pane separator"),
        (".hidden", "must start alphanumeric"),
        ("-flag", "would parse as a tmux flag"),
        ("", "empty"),
        ("a" * 65, "too long"),
        ("build session", "space"),
        ("build\tsession", "TAB would corrupt the -F record"),
        ("build\nsession", "LF would split the -F record"),
        ("env*", "fnmatch metacharacter -- tmux would resolve it against other sessions"),
    ],
)
def test_invalid_session_names(name: str, why: str) -> None:
    with pytest.raises(InvalidName):
        naming.validate_session_name(name)


def test_anchored_name_is_invalid_name_not_not_found() -> None:
    """§7.1's two-level split, adapter side.

    A caller-supplied ``=bui`` is ``invalid_name`` and dies here, before tmux is invoked.
    It is NOT ``not_found``: that distinction is the reason the criterion is split across
    two levels, since at the raw-tmux level ``resize-window -t '=bui'`` is *accepted*.
    """
    with pytest.raises(InvalidName) as excinfo:
        naming.validate_session_name("=bui")
    assert excinfo.value.code == "invalid_name"


def test_validate_cwd_canonicalises(tmp_path) -> None:
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target_dir)
    assert naming.validate_cwd(str(link)) == os.path.realpath(str(target_dir))


@pytest.mark.parametrize("char", ["\t", "\r", "\n"])
def test_validate_cwd_rejects_record_breaking_characters(tmp_path, char: str) -> None:
    """TAB/CR/LF in a cwd are ``bad_cwd``, rejected before tmux (spike S11).

    Measured, and this is why it is enforced rather than tolerated: a TAB in
    ``@shellbox_cwd`` makes the ``-F`` record 9 fields, so it is dropped -- and a dropped
    record means orphan reconciliation marks a LIVE session ``orphaned``. An LF is worse:
    the record stays 8 fields with a silently truncated cwd, so it PASSES the field-count
    check carrying wrong data, plus a spurious second record.
    """
    directory = tmp_path / f"a{char}b"
    directory.mkdir()
    assert directory.is_dir()  # the path really exists; only the character is the problem
    with pytest.raises(BadCwd) as excinfo:
        naming.validate_cwd(str(directory))
    assert excinfo.value.code == "bad_cwd"


def test_validate_cwd_rejects_a_symlink_that_resolves_into_a_tab_directory(tmp_path) -> None:
    """The character check runs again on the RESOLVED path, not only the input.

    Otherwise a clean-looking argument pointing at a TAB-containing directory would sail
    through and stamp the broken value into ``@shellbox_cwd``.
    """
    tabbed = tmp_path / "a\tb"
    tabbed.mkdir()
    link = tmp_path / "clean"
    link.symlink_to(tabbed)
    with pytest.raises(BadCwd):
        naming.validate_cwd(str(link))


def test_validate_cwd_rejects_non_directories(tmp_path) -> None:
    file_path = tmp_path / "afile"
    file_path.write_text("x")
    with pytest.raises(BadCwd):
        naming.validate_cwd(str(file_path))
    with pytest.raises(BadCwd):
        naming.validate_cwd(str(tmp_path / "missing"))


def test_validate_env_passes_lf_and_semicolon_values_through() -> None:
    """Measured (M21, re-run in the verbatim chain): ``-e`` carries LF and ``;`` intact."""
    assert naming.validate_env({"FOO": "a\nb", "BAR": "x;y"}) == [
        "-e",
        "FOO=a\nb",
        "-e",
        "BAR=x;y",
    ]
    assert naming.validate_env(None) == []
    assert naming.validate_env({}) == []


@pytest.mark.parametrize("key", ["1FOO", "FOO=BAR", "foo bar", "", "FOO-BAR"])
def test_validate_env_rejects_bad_keys(key: str) -> None:
    with pytest.raises(InvalidName):
        naming.validate_env({key: "v"})


def test_validate_env_rejects_nul() -> None:
    with pytest.raises(InvalidName):
        naming.validate_env({"FOO": "a\0b"})


@pytest.mark.parametrize(("cols", "rows"), [(0, 24), (80, 0), (-1, 24), (80, 10_001)])
def test_validate_dimensions_rejects_out_of_range(cols: int, rows: int) -> None:
    with pytest.raises(InvalidDimensions):
        naming.validate_dimensions(cols, rows)


def test_validate_dimensions_rejects_bools() -> None:
    # `True` is an int in Python, and `-x True` is not a window width.
    with pytest.raises(InvalidDimensions):
        naming.validate_dimensions(True, 24)


def _measure_max_bindable_path_bytes() -> int:
    """Bind real AF_UNIX sockets at increasing lengths and return the longest that binds."""
    longest = 0
    for length in range(90, 130):
        path = "/tmp/" + "s" * (length - len("/tmp/"))
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
    return longest


def test_sun_path_limit_matches_the_platform() -> None:
    """The platform limit is MEASURED here, not asserted from a constant.

    ``sun_path`` is 104 bytes on macOS/BSD and 108 on Linux, so a single hardcoded number is
    wrong on one of the two platforms shellbox runs on -- and the symptom is tmux's
    ``File name too long`` at every call, with nothing naming the cause. This binds real
    sockets until the kernel refuses, and fails loudly if ``naming``'s table disagrees with
    the kernel actually running the test.
    """
    measured = _measure_max_bindable_path_bytes()
    assert measured > 0, "could not measure the AF_UNIX path limit"
    assert measured == naming.max_socket_path_bytes(), (
        f"kernel accepts paths up to {measured} bytes but naming.max_socket_path_bytes() "
        f"says {naming.max_socket_path_bytes()} (sun_path_limit={naming.sun_path_limit()} "
        f"on {sys.platform})"
    )
    # The limit includes the NUL terminator, hence the off-by-one between the two.
    assert naming.sun_path_limit() == measured + 1
    assert naming.sun_path_limit() in {104, 108}


def test_validate_socket_path_fails_loudly_at_one_byte_over() -> None:
    limit = naming.max_socket_path_bytes()
    ok = "/tmp/" + "s" * (limit - len("/tmp/"))
    assert naming.validate_socket_path(ok) == ok
    with pytest.raises(SocketPathTooLong) as excinfo:
        naming.validate_socket_path(ok + "x")
    # tmux_unavailable, and the message names the limit, the platform and the override.
    assert excinfo.value.code == "tmux_unavailable"
    assert str(limit) in excinfo.value.message
    assert "SHELLBOX_TMUX_SOCKET" in excinfo.value.message


def test_validate_socket_path_counts_bytes_not_characters() -> None:
    """A multi-byte path is measured in bytes, because ``sun_path`` is a byte array."""
    limit = naming.max_socket_path_bytes()
    multibyte = "/tmp/" + "é" * ((limit - len("/tmp/")) // 2 + 1)
    assert len(multibyte) <= limit  # fits by characters
    assert len(os.fsencode(multibyte)) > limit  # does not fit by bytes
    with pytest.raises(SocketPathTooLong):
        naming.validate_socket_path(multibyte)


def test_session_id_is_deterministic() -> None:
    assert naming.session_id("host-1", "build") == "host-1:build"
    assert naming.session_id("host-1", "build") == naming.session_id("host-1", "build")


# --- Regressions found in code review of PR #11 -------------------------------------------


def test_a_trailing_newline_in_a_session_name_is_rejected() -> None:
    """``$`` also matches BEFORE a trailing newline in Python; ``\\Z`` does not.

    This is not a cosmetic validation nit. ``new-session -s "build\\n"`` SUCCEEDS, and then
    every ``set-option -t '=build\\n:'`` in the create chain fails, so the chain returns rc=1
    and the caller sees ``not_found`` -- while a session with no incarnation is left behind on
    the tmux server that every pooled agent shares. It can never be reached through
    ``target()``, ``shell_kill`` refuses it for having no incarnation, and it appears in
    ``shell_list`` as ``foreign`` forever.
    """
    for name in ("build\n", "build\r\n", "build\n\n"):
        with pytest.raises(InvalidName):
            naming.validate_session_name(name)


def test_a_nul_in_cwd_is_bad_cwd_and_not_a_raw_valueerror() -> None:
    """NUL must be rejected at the boundary, like TAB/CR/LF.

    ``os.path.realpath`` raises ``ValueError("embedded null character")`` before any of the
    record-corruption checks run, so without NUL in the forbidden set the caller got
    ``tmux_error`` wrapping a raw ValueError instead of ``bad_cwd``. An agent branching on
    ``bad_cwd`` to retry somewhere else would read a bad argument as an infrastructure
    failure. ``validate_env`` already rejected NUL; this keeps the two consistent.
    """
    with pytest.raises(BadCwd):
        naming.validate_cwd("/tmp/a" + chr(0) + "b")


def test_the_length_boundary_still_holds_after_the_anchor_change() -> None:
    """Guard against ``\\Z`` having moved the 1-64 boundary as a side effect."""
    naming.validate_session_name("a" * 64)
    with pytest.raises(InvalidName):
        naming.validate_session_name("a" * 65)
