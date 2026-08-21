"""The mutation test for ``scripts/check_pex_lock.py`` -- both provenance assertions must fail on a
broken pex lock, and the clean control must pass.

This exists for the reason ``tests/unit/test_check_lockfile.py`` exists, and it is the same reason
``scripts/check_pex_lock.py``'s own header states at length: a provenance guard is only worth the
line it occupies if it can be shown to redden. The obvious shape of the host check -- ``grep`` for
the mirror, fail if it matched -- PASSES on a missing file and on an empty one, because ``grep``
exits non-zero when it matches nothing. So each case here MUTATES a lock and asserts the checker
rejects it, and the clean case proves the mutated cases fail for the reason claimed rather than
because the checker rejects everything.

Deliberately synthetic, and never pex-built. ``scripts/check_pex_lock.py``'s header records why the
real lock cannot be produced on this machine (pex runs its vendored pip ``--isolated``, ``pypi.org``
is blackholed in ``/etc/hosts``), so a test that shelled out to ``pex3 lock create`` would be
unrunnable here and in CI's lint lane both. A hand-built JSON document exercises the checker's two
assertions without needing a resolver, exactly as ``test_check_lockfile.py`` hand-builds TOML.

The checker is run as a SUBPROCESS rather than called in-process, because the thing the build
depends on is its exit status.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_pex_lock.py"

# The allowed host and the floor are read out of the script, not restated. Restating them would let
# the two drift apart, and this file would then test a value the build does not use.
_spec = importlib.util.spec_from_file_location("check_pex_lock", _SCRIPT)
assert _spec is not None and _spec.loader is not None, f"cannot load {_SCRIPT}"
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)

ALLOWED_HOST: str = _checker.ALLOWED_HOST
MIN_ARTIFACT_COUNT: int = _checker.MIN_ARTIFACT_COUNT

MIRROR_HOST = "pypi-proxy.dev.databricks.com"


def _artifact(index: int, host: str) -> dict[str, Any]:
    """One locked artifact: a url on ``host`` and a per-artifact hash.

    The key names (``url``, ``hash``) are the ones the checker's schema-independent walk collects;
    the nesting mimics a ``pex3 lock``'s ``locked_resolves[].locked_requirements[].artifacts[]``
    without depending on that exact shape.
    """
    return {
        "url": f"https://{host}/packages/aa/bb/pkg{index}-1.0.0-py3-none-any.whl",
        "hash": f"sha256:{index:064x}",
        "algorithm": "sha256",
    }


def _lock(artifact_count: int, *, host: str = ALLOWED_HOST) -> dict[str, Any]:
    """A pex-lock-shaped document holding exactly ``artifact_count`` artifacts on ``host``."""
    return {
        "pex_version": "2.100.4",
        "style": "universal",
        "locked_resolves": [
            {
                "locked_requirements": [
                    {
                        "project_name": f"pkg{index}",
                        "version": "1.0.0",
                        "artifacts": [_artifact(index, host)],
                    }
                    for index in range(artifact_count)
                ]
            }
        ],
    }


def _write(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document, indent=2))
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
    )


# --- the floor is real, not a formality -------------------------------------------------------


def test_the_floor_and_host_are_the_ones_the_script_uses() -> None:
    """Read from the script so the two cannot drift. The floor is a non-vacuity floor and the
    script's header explains why it is 1 pending a CI measurement; assert only that it is a floor
    (>= 1) and that the host is the public one."""
    assert MIN_ARTIFACT_COUNT >= 1
    assert ALLOWED_HOST == "files.pythonhosted.org"


# --- --assert-hosts ---------------------------------------------------------------------------


def test_a_clean_lock_passes_hosts(tmp_path: Path) -> None:
    """The control. Without it, every host case below could be passing for the wrong reason."""
    path = _write(tmp_path / "shellbox.lock", _lock(max(MIN_ARTIFACT_COUNT, 3)))
    result = _run("--assert-hosts", str(path))
    assert result.returncode == 0, result.stderr
    assert f"all on {ALLOWED_HOST}" in result.stdout


def test_a_missing_lock_fails_hosts(tmp_path: Path) -> None:
    """The defect this file exists for: a check must not report success on no artifact."""
    result = _run("--assert-hosts", str(tmp_path / "absent.lock"))
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_an_empty_lock_fails_hosts(tmp_path: Path) -> None:
    path = tmp_path / "shellbox.lock"
    path.write_text("")
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "is empty" in result.stderr


def test_a_non_json_lock_fails_hosts(tmp_path: Path) -> None:
    """A truncated lock usually stops being valid JSON; that is a check, not input validation."""
    path = tmp_path / "shellbox.lock"
    path.write_text("{ this is not json")
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "not valid JSON" in result.stderr


def test_a_lock_with_no_urls_fails_hosts(tmp_path: Path) -> None:
    """Valid JSON, plausible shape, no artifacts. The host check must not pass vacuously."""
    path = _write(tmp_path / "shellbox.lock", {"pex_version": "2.100.4", "locked_resolves": []})
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "no 'url' entries" in result.stderr


def test_a_single_mirror_url_among_many_good_ones_fails_hosts(tmp_path: Path) -> None:
    """The failure the rewrite exists to prevent, and the hardest to catch: one leaked host.

    Every other url is correct and the count is above the floor, so nothing but the host allowlist
    can reject this.
    """
    document = _lock(max(MIN_ARTIFACT_COUNT, 3))
    # Append one requirement whose sole artifact still points at the mirror -- a url the rewrite
    # missed.
    document["locked_resolves"][0]["locked_requirements"].append(
        {"project_name": "leaked", "version": "1.0.0", "artifacts": [_artifact(999, MIRROR_HOST)]}
    )
    path = _write(tmp_path / "shellbox.lock", document)
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert MIRROR_HOST in result.stderr
    assert "1 of" in result.stderr


def test_an_entirely_mirror_resolved_lock_fails_hosts(tmp_path: Path) -> None:
    """What ``pex3 lock create`` really produces behind the mirror: every url on the mirror. This
    is the state the rewrite step must convert, and building from it unrewritten must be refused."""
    path = _write(tmp_path / "shellbox.lock", _lock(max(MIN_ARTIFACT_COUNT, 3), host=MIRROR_HOST))
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert MIRROR_HOST in result.stderr


def test_a_host_that_merely_contains_the_allowed_host_fails(tmp_path: Path) -> None:
    """The host rule is an EXACT comparison, not a substring or suffix test:
    ``files.pythonhosted.org.example`` contains the allowed host and is a different server."""
    path = _write(
        tmp_path / "shellbox.lock",
        _lock(max(MIN_ARTIFACT_COUNT, 3), host="files.pythonhosted.org.example"),
    )
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "files.pythonhosted.org.example" in result.stderr


def test_a_count_below_the_floor_fails_hosts(tmp_path: Path) -> None:
    """The non-vacuity floor. When the floor is 1, an empty-but-valid resolve is what this catches;
    the case is written to bite at whatever the script's floor is, so raising the floor after a CI
    measurement keeps a real assertion here rather than a vacuous one."""
    # A lock with strictly fewer artifacts than the floor. When the floor is 1 this is zero
    # artifacts, which the no-urls case already covers; guard so the assertion is meaningful only
    # when the floor leaves room for it.
    if MIN_ARTIFACT_COUNT <= 1:
        return
    path = _write(tmp_path / "shellbox.lock", _lock(MIN_ARTIFACT_COUNT - 1))
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert f"floor of {MIN_ARTIFACT_COUNT}" in result.stderr


def test_a_first_party_file_url_is_allowed(tmp_path: Path) -> None:
    """OQ-7, measured: the workspace wheels R9 builds locally are recorded as `file://` URLs with
    no host. They are first-party and carry no external provenance, so `--assert-hosts` allows them
    by SCHEME while still requiring every remote URL to be on the one public host."""
    document = _lock(max(MIN_ARTIFACT_COUNT, 3))
    document["locked_resolves"][0]["locked_requirements"].append(
        {
            "project_name": "shellbox-mcp",
            "version": "0.1.0",
            "artifacts": [
                {
                    "url": "file:///tmp/build/wheels/shellbox_mcp-0.1.0-py3-none-any.whl",
                    "hash": "sha256:" + "a" * 64,
                    "algorithm": "sha256",
                }
            ],
        }
    )
    path = _write(tmp_path / "shellbox.lock", document)
    result = _run("--assert-hosts", str(path))
    assert result.returncode == 0, result.stderr
    assert "first-party" in result.stdout


def test_a_lock_of_only_file_urls_fails_the_floor(tmp_path: Path) -> None:
    """The floor counts the THIRD-PARTY set: a lock holding only first-party file:// wheels and no
    remote dependencies is a broken resolve, and the file:// wheels must not paper over it."""
    document = {
        "pex_version": "2.100.4",
        "locked_resolves": [
            {
                "locked_requirements": [
                    {
                        "project_name": "shellbox-mcp",
                        "version": "0.1.0",
                        "artifacts": [
                            {
                                "url": "file:///tmp/build/wheels/shellbox_mcp-0.1.0-py3-none-any.whl",
                                "hash": "sha256:" + "a" * 64,
                                "algorithm": "sha256",
                            }
                        ],
                    }
                ]
            }
        ],
    }
    path = _write(tmp_path / "shellbox.lock", document)
    result = _run("--assert-hosts", str(path))
    assert result.returncode != 0
    assert "floor" in result.stderr


# --- --assert-hashes-unchanged ----------------------------------------------------------------


def test_an_unchanged_rewrite_passes(tmp_path: Path) -> None:
    """The control for the hash mode: a host-only rewrite leaves every hash intact.

    ``before`` is on the mirror, ``after`` is the same document with only the hosts rewritten to
    public. The hashes are identical, so the mode passes.
    """
    before = _lock(max(MIN_ARTIFACT_COUNT, 3), host=MIRROR_HOST)
    after = _lock(max(MIN_ARTIFACT_COUNT, 3), host=ALLOWED_HOST)
    baseline = _write(tmp_path / "shellbox.lock.bak", before)
    lock = _write(tmp_path / "shellbox.lock", after)
    result = _run("--assert-hashes-unchanged", str(lock), "--baseline", str(baseline))
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_a_changed_hash_fails(tmp_path: Path) -> None:
    """A rewrite that altered an artifact, not only a host. A host substitution cannot do this, so
    the checker must refuse to build from it."""
    before = _lock(max(MIN_ARTIFACT_COUNT, 3), host=MIRROR_HOST)
    after = _lock(max(MIN_ARTIFACT_COUNT, 3), host=ALLOWED_HOST)
    # Change one artifact's hash in the rewritten file.
    after["locked_resolves"][0]["locked_requirements"][0]["artifacts"][0]["hash"] = (
        "sha256:" + "f" * 64
    )
    baseline = _write(tmp_path / "shellbox.lock.bak", before)
    lock = _write(tmp_path / "shellbox.lock", after)
    result = _run("--assert-hashes-unchanged", str(lock), "--baseline", str(baseline))
    assert result.returncode != 0
    assert "changed the set of pinned hashes" in result.stderr


def test_hashes_unchanged_requires_a_baseline(tmp_path: Path) -> None:
    """The mode is meaningless without something to compare against, so a missing --baseline is a
    usage error, not a silent pass."""
    lock = _write(tmp_path / "shellbox.lock", _lock(max(MIN_ARTIFACT_COUNT, 3)))
    result = _run("--assert-hashes-unchanged", str(lock))
    assert result.returncode != 0
    assert "--baseline" in result.stderr


def test_a_missing_baseline_fails(tmp_path: Path) -> None:
    """The pre-rewrite ``.bak`` must exist, or the rewrite step never ran."""
    lock = _write(tmp_path / "shellbox.lock", _lock(max(MIN_ARTIFACT_COUNT, 3)))
    result = _run(
        "--assert-hashes-unchanged", str(lock), "--baseline", str(tmp_path / "absent.bak")
    )
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_a_baseline_with_no_hashes_fails(tmp_path: Path) -> None:
    """The witness: two files that both hold no hashes compare equal, and would report success on a
    rewrite of nothing. The baseline must carry hashes or the comparison is meaningless."""
    before = {"pex_version": "2.100.4", "locked_resolves": []}
    after = _lock(max(MIN_ARTIFACT_COUNT, 3))
    baseline = _write(tmp_path / "shellbox.lock.bak", before)
    lock = _write(tmp_path / "shellbox.lock", after)
    result = _run("--assert-hashes-unchanged", str(lock), "--baseline", str(baseline))
    assert result.returncode != 0
    assert "no 'hash' values" in result.stderr


# --- argument handling ------------------------------------------------------------------------


def test_a_mode_is_required(tmp_path: Path) -> None:
    """Neither mode is the default. A bare path must not silently do the weaker check."""
    lock = _write(tmp_path / "shellbox.lock", _lock(max(MIN_ARTIFACT_COUNT, 3)))
    result = _run(str(lock))
    assert result.returncode != 0
