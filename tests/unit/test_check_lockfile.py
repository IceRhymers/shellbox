"""The mutation test for ``scripts/check_lockfile.py`` -- every assertion must fail on a broken
lockfile.

A CI check is also a claim. This repo has had checks that passed against an artifact that did not
exist, because the obvious shape of such a check -- ``grep`` for a bad value, fail if it matched --
exits 0 on a missing file and on an empty one. ``scripts/check_bundle_statics.py`` carries a
warning about the same defect, found four separate times in checks written by people who had just
read about it.

So each case here MUTATES a lockfile and asserts the checker rejects it. The clean case exists to
prove the mutated cases fail for the reason claimed and not because the checker rejects everything.

Deliberately synthetic. Nothing here reads the real ``uv.lock``: that file is gitignored, it is
absent on a fresh clone, and on a machine configured against an internal package mirror its URLs
legitimately name that mirror. A test that read it would fail on some developer machines and skip
on others, which is the opposite of what a mutation test is for.

The checker is run as a SUBPROCESS rather than called in-process, because the thing CI depends on
is its exit status.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_lockfile.py"

# The floor and the allowed host are read out of the script rather than restated here. Restating
# them would let the two drift apart, and this file would then be testing a number the CI job
# does not use.
_spec = importlib.util.spec_from_file_location("check_lockfile", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)

ALLOWED_HOST: str = _checker.ALLOWED_HOST
MIN_URL_COUNT: int = _checker.MIN_URL_COUNT

MIRROR_HOST = "pypi-proxy.dev.databricks.com"


def _package(index: int, host: str) -> str:
    """One ``[[package]]`` block holding exactly one artifact URL."""
    return (
        "[[package]]\n"
        f'name = "pkg{index}"\n'
        'version = "1.0.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f'sdist = {{ url = "https://{host}/packages/aa/bb/pkg{index}-1.0.0.tar.gz", '
        'hash = "sha256:0000", size = 1 }\n'
    )


def _lockfile(url_count: int, *, host: str = ALLOWED_HOST) -> str:
    """A lockfile-shaped TOML document with exactly ``url_count`` artifact URLs."""
    header = 'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
    return header + "\n".join(_package(index, host) for index in range(url_count))


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "uv.lock"
    path.write_text(text)
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_the_floor_is_not_vacuous() -> None:
    """A floor of 0 would make the count check pass on an empty lockfile.

    The number itself is measured and documented in the script. This only asserts it is a floor
    and not a formality, so that lowering it to silence a failure is a visible change.
    """
    assert MIN_URL_COUNT >= 100
    assert ALLOWED_HOST == "files.pythonhosted.org"


def test_a_clean_lockfile_passes(tmp_path: Path) -> None:
    """The control. Without it, every case below could be passing for the wrong reason."""
    path = _write(tmp_path, _lockfile(MIN_URL_COUNT))
    result = _run("--assert-hosts", str(path))
    assert result.returncode == 0, result.stderr
    assert f"{MIN_URL_COUNT} artifact URLs" in result.stdout


def test_a_missing_lockfile_fails(tmp_path: Path) -> None:
    """The defect that motivates this file: a check that reports success on no artifact."""
    result = _run("--assert-hosts", str(tmp_path / "uv.lock"))
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_an_empty_lockfile_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "is empty" in result.stderr


def test_a_lockfile_with_no_urls_fails(tmp_path: Path) -> None:
    """Valid TOML, plausible shape, no artifacts. A count check alone would report 0 and a host
    check alone would find nothing to reject."""
    path = _write(tmp_path, 'version = 1\nrequires-python = ">=3.12"\n')
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "no artifact URLs" in result.stderr


def test_a_truncated_lockfile_fails(tmp_path: Path) -> None:
    """A half-written file, the shape a killed or failed emit step leaves behind."""
    full = _lockfile(MIN_URL_COUNT)
    path = _write(tmp_path, full[: len(full) // 2])
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0


def test_one_url_below_the_floor_fails(tmp_path: Path) -> None:
    """The boundary. A floor that only rejected zero would pass a mostly-empty lockfile."""
    path = _write(tmp_path, _lockfile(MIN_URL_COUNT - 1))
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert f"fewer than the floor of {MIN_URL_COUNT}" in result.stderr


def test_a_single_mirror_url_among_many_good_ones_fails(tmp_path: Path) -> None:
    """The failure this job exists for, and the hardest one to catch: one leaked host.

    The count is above the floor and every other URL is correct, so nothing but the host
    allowlist can reject this.
    """
    good = _lockfile(MIN_URL_COUNT)
    leaked = good + "\n" + _package(MIN_URL_COUNT, MIRROR_HOST)
    path = _write(tmp_path, leaked)
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert MIRROR_HOST in result.stderr
    assert "1 of" in result.stderr


def test_an_entirely_mirror_resolved_lockfile_fails(tmp_path: Path) -> None:
    """What ``uv lock`` really produces on a mirror-configured machine: every URL rewritten."""
    path = _write(tmp_path, _lockfile(MIN_URL_COUNT, host=MIRROR_HOST))
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert MIRROR_HOST in result.stderr


@pytest.mark.parametrize(
    "host",
    [
        # Contains the allowed host as a suffix. A substring or `endswith` test would accept it.
        "evil.files.pythonhosted.org.example",
        # Contains it as a prefix, which an `in` test would accept too.
        "files.pythonhosted.org.example",
        # A near-miss on the real thing.
        "files.pythonhosted.org.co",
    ],
)
def test_a_host_that_merely_contains_the_allowed_host_fails(tmp_path: Path, host: str) -> None:
    """The host rule is an EXACT comparison, not a substring or suffix test."""
    path = _write(tmp_path, _lockfile(MIN_URL_COUNT, host=host))
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert host in result.stderr


def test_a_url_with_no_host_fails(tmp_path: Path) -> None:
    """A relative or scheme-less URL has no host to compare, so it must not pass by default."""
    body = _lockfile(MIN_URL_COUNT).replace(
        f'url = "https://{ALLOWED_HOST}/packages/aa/bb/pkg0-1.0.0.tar.gz"',
        'url = "packages/aa/bb/pkg0-1.0.0.tar.gz"',
    )
    path = _write(tmp_path, body)
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "no host" in result.stderr


def test_assert_absent_fails_when_the_lockfile_is_committed(tmp_path: Path) -> None:
    """The guard on criterion one: an emitted artifact, not a committed one."""
    path = _write(tmp_path, _lockfile(MIN_URL_COUNT))
    result = _run("--assert-absent", str(path))
    assert result.returncode != 0
    assert "is committed" in result.stderr


def test_assert_absent_passes_on_a_fresh_checkout(tmp_path: Path) -> None:
    result = _run("--assert-absent", str(tmp_path / "uv.lock"))
    assert result.returncode == 0, result.stderr
    assert "is absent" in result.stdout


def test_assert_absent_fails_on_an_empty_lockfile(tmp_path: Path) -> None:
    """An empty file still means the path is tracked. Testing size instead of existence would
    let a zero-byte committed lockfile through."""
    path = _write(tmp_path, "")
    result = _run("--assert-absent", str(path))
    assert result.returncode != 0


def test_a_mode_is_required() -> None:
    """Neither mode is the default. A bare path must not silently do the weaker check."""
    result = _run("uv.lock")
    assert result.returncode != 0
