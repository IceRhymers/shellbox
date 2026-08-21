#!/usr/bin/env bash
# Build the single-file, sha256-pinned release artifact for `shellbox-mcp` (shellbox#21).
#
# Output: `dist/shellbox` (mode 0755, `#!/usr/bin/env python3`, extensionless) + `dist/shellbox.sha256`.
# `scripts/check_artifact.py` asserts every property of the result; `.github/workflows/` runs both.
#
# ============================================================================================
# CI-ONLY, in the sense `scripts/check_lockfile.py`'s header means it. This build cannot run on a
# developer workstation configured against the internal mirror, and the reason is measured, not
# assumed:
#
#   * pex runs its vendored pip with `--isolated` (MEASURED 2026-08-19: the pip subcommand pex
#     spawns carries `--isolated`), so neither `~/.pip/pip.conf` nor `PIP_INDEX_URL` reaches it.
#     The index must be given to pex explicitly, via `pex3 lock create --index`.
#   * `pypi.org` is blackholed to `127.0.0.1` on the author's box (`/etc/hosts:1059-1060`,
#     MEASURED 2026-08-19), so a bare `--index https://pypi.org/simple` is connection-refused.
#     `files.pythonhosted.org` and the mirror both answer 200.
#
# So the mirror-then-rewrite path below is REQUIRED, not optional: resolve through the mirror that
# answers here, then rewrite the recorded hosts to public ones and PROVE the rewrite changed only
# hosts. This is `Makefile:224-234`'s `app-lock` argument, transposed from `uv.lock` to a pex lock.
# `ci.yml:78-81` already records the same "no PyPI reachable in the local container" fact for the
# integration suite. Local verification of a BUILT artifact is `check_artifact.py --assert-*`
# against a file downloaded from a CI run, never a local `make artifact`.
#
# OQ-7 RESOLVED 2026-08-19: a `pex3 lock`'s artifact URLs sit under `url` keys with PyPI's
# `/packages/<hash-path>/<file>` layout, which the mirror preserves 1:1, and no index/source URL
# naming `pypi.org` lives under a `url` key -- so the host `sed` below is exact and
# `check_pex_lock.py`'s allowlist-of-one is correct.
#
# STEP 0 DEPENDENCY (shellbox#21 blocking item 4): this reads two files the live-sandbox probe
# produces -- `probe/complete-platform-<arch>.json` (what pex resolves FOR) and
# `probe/artifact-platform.json` (the build tag + measured glibc baked into the arch check). It
# fails with a clear message if they are absent, rather than guessing a platform.
# ============================================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# The asset name is the model-visible tool prefix (`buzz-acp` derives the MCP server name from the
# file stem), so it MUST equal `server.py`'s SERVER_NAME. `tests/unit/test_artifact_naming.py`
# reads both this value and that constant and asserts they agree, so a change here that drifts from
# the code reddens a test rather than silently renaming every tool the model sees.
ARTIFACT_NAME="shellbox"
DIST_DIR="$REPO_ROOT/dist"
SIDECAR="$REPO_ROOT/probe/artifact-platform.json"

# ---- Step 0 inputs must exist -------------------------------------------------------------
if [[ ! -f "$SIDECAR" ]]; then
  echo "::error::$SIDECAR does not exist. Step 0's live-sandbox probe must land first" \
       "(probe/probe_identity.py 'architecture' lane) so the build knows which platform to" \
       "resolve for and which glibc ceiling to bake into the arch check. See shellbox#21 Step 0." >&2
  exit 1
fi

# Read the build tag, the measured glibc and the machine from the committed sidecar. `python3` and
# not `jq`, because the repo's other scripts do not assume `jq` is installed.
read -r MACHINE BUILD_TAG GLIBC COMPLETE_PLATFORM < <(python3 - "$SIDECAR" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["machine"], d["build_platform_tag"], d["glibc"], d.get("complete_platform_file", ""))
PY
)
COMPLETE_PLATFORM_FILE="$REPO_ROOT/${COMPLETE_PLATFORM:-probe/complete-platform-$MACHINE.json}"
if [[ ! -f "$COMPLETE_PLATFORM_FILE" ]]; then
  echo "::error::$COMPLETE_PLATFORM_FILE does not exist. Step 0 emits it (pex3 interpreter" \
       "inspect --markers --tags) from the live sandbox; the build resolves FOR it so the" \
       "artifact matches the sandbox rather than the CI runner." >&2
  exit 1
fi

VERSION="$(python3 - <<'PY'
import tomllib
print(tomllib.load(open("packages/shellbox-mcp/pyproject.toml", "rb"))["project"]["version"])
PY
)"
GIT_SHA="$(git rev-parse HEAD)"

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
mkdir -p "$DIST_DIR"

echo ">> building $ARTIFACT_NAME $VERSION for $BUILD_TAG (glibc ceiling $GLIBC) @ $GIT_SHA"

# ---- 1. Workspace wheels first (R9) -------------------------------------------------------
# `shellbox-mcp` declares `shellbox-registry` and `shellbox-transport` via
# `[tool.uv.sources] { workspace = true }`, and neither is published to any index, so pex's pip
# cannot resolve them by name. All three are pure-Python hatchling builds, so we build local
# `py3-none-any` wheels and hand them to pex as `--find-links`. A foreign-platform pex resolve
# refuses to build sdists, which is the other reason these must be pre-built wheels.
WHEELS_DIR="$BUILD_DIR/wheels"
mkdir -p "$WHEELS_DIR"
for pkg in shellbox-transport shellbox-registry shellbox-mcp; do
  uv build --wheel "packages/$pkg" --out-dir "$WHEELS_DIR"
done

# ---- 2. Lock, rewrite, prove (the app-lock shape) -----------------------------------------
# THE REWRITE IS REQUIRED, NOT DEFENSIVE -- see the header and `Makefile:224-234`. `pex3 lock`
# records the mirror hosts it resolved through; the artifact must resolve for everyone, so the
# hosts are rewritten to public and the rewrite is proven to have touched only hosts.
#
# `--no-build` (wheels only, never compile) and a STRICT single-platform resolve against the Step 0
# `--complete-platform` are the load-bearing pair here, and both are proven by Step 0's own
# measurement: `uv pip install --python-platform x86_64-manylinux2014 --only-binary :all:` resolved
# all 45 dists (19 .so, 81 MB) at the manylinux_2_17 floor. Without `--no-build` pex resolved
# greenlet to its sdist and tried to compile it (`x86_64-linux-gnu-g++` not found -- there is no C++
# toolchain in the build container, nor should there be): a release artifact must ship the exact
# manylinux wheels, and a genuinely missing wheel must be a loud error, not a silent local compile.
# `--style universal` was leftover fat-multi-platform thinking: this artifact ships ONE platform, so
# a strict resolve for the single complete-platform is both correct and what the Step 0 measurement
# proved satisfiable -- a universal lock would instead try to satisfy every platform and can pull an
# sdist for one we never ship.
LOCK="$BUILD_DIR/shellbox.lock"
INDEX="${SHELLBOX_BUILD_INDEX:-https://pypi-proxy.dev.databricks.com/simple}"

pex3 lock create \
  --index "$INDEX" \
  --find-links "$WHEELS_DIR" \
  --complete-platform "$COMPLETE_PLATFORM_FILE" \
  --no-build \
  shellbox-mcp \
  --indent 2 \
  -o "$LOCK"

# Rewrite mirror hosts to public. `sed -i.bak` leaves the pre-rewrite bytes in `<lock>.bak`, which
# is exactly what `--assert-hashes-unchanged` reads -- the input the rewrite saw, regenerated by
# nothing. Removed only after both checks pass, so a failure leaves it for reading.
sed -i.bak \
  -e 's#https://pypi-proxy\.dev\.databricks\.com/simple/#https://pypi.org/simple/#g' \
  -e 's#https://pypi-proxy\.dev\.databricks\.com/packages/#https://files.pythonhosted.org/packages/#g' \
  "$LOCK"

python3 scripts/check_pex_lock.py --assert-hashes-unchanged "$LOCK" --baseline "$LOCK.bak"
python3 scripts/check_pex_lock.py --assert-hosts "$LOCK"
rm -f "$LOCK.bak"

# ---- 3. The arch-check entry-point module (P1 / blocking item 2) --------------------------
# pex has `--interpreter-constraint` for the Python floor but NO architecture constraint. Without
# this, a wrong-arch artifact fails at `import pydantic_core` -- the illegible failure P1 exists to
# prevent. This tiny module runs AFTER pex extraction but BEFORE any extension import (because
# `cli.py` defers `server`, hence `pydantic_core`, out of module scope), checks the baked machine
# and glibc FLOOR, writes ONE line to STDERR and exits non-zero on mismatch, then delegates to
# `shellbox_mcp.cli:main`. stdout stays untouched -- it is the JSON-RPC wire.
#
# $GLIBC is the artifact's contract FLOOR (2.17 for the manylinux_2_17 build), read from the Step 0
# sidecar's `glibc` -- NOT the build sandbox's own glibc (2.39, recorded as `measured_sandbox_glibc`
# headroom). The artifact's wheels require glibc >= the floor, so the host check refuses anything
# below the floor. Baking the sandbox's 2.39 here would wrongly refuse a glibc-2.30 host that the
# 2.17 wheels run on perfectly.
ENTRY_DIR="$BUILD_DIR/entry"
mkdir -p "$ENTRY_DIR"
cat > "$ENTRY_DIR/_shellbox_entry.py" <<PY
"""Generated by scripts/build_artifact.sh -- the artifact's entry point. Do not edit by hand."""
import platform
import sys

_EXPECTED_MACHINE = "$MACHINE"
_GLIBC_FLOOR = tuple(int(p) for p in "$GLIBC".split(".")[:2])


def main() -> None:
    machine = platform.machine()
    if machine != _EXPECTED_MACHINE:
        # stderr, never stdout: stdout is the JSON-RPC wire.
        print(
            f"shellbox: this artifact was built for {_EXPECTED_MACHINE}, but the host is "
            f"{machine}. Install the {machine} build. (shellbox#29 tracks multi-arch.)",
            file=sys.stderr,
        )
        raise SystemExit(70)
    _, libc = platform.libc_ver()
    have = tuple(int(p) for p in libc.split(".")[:2]) if libc else ()
    if have and have < _GLIBC_FLOOR:
        print(
            f"shellbox: this artifact needs glibc >= "
            f"{_GLIBC_FLOOR[0]}.{_GLIBC_FLOOR[1]}, but the host reports {libc}.",
            file=sys.stderr,
        )
        raise SystemExit(70)
    from shellbox_mcp.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
PY

# ---- 4. Build the pex ---------------------------------------------------------------------
# `--no-emit-warnings` keeps pex's own diagnostics off every stream at startup (Principle 4).
#
# NO `--runtime-pex-root`. pex bakes that value LITERALLY -- it does not expand `$HOME` or env vars
# -- so a baked `$HOME/.cache/...` created a directory literally named `$HOME` under the process
# cwd (MEASURED by the spike's q_reexec: the venv python resolved under `.../shellbox/$HOME/.cache/
# shellbox-pex/...`). That defeats the whole "extract once into persistent $HOME" intent. pex's
# DEFAULT root is `~/.pex`, which pex resolves with `os.path.expanduser` at RUNTIME to the real
# home -- persistent across stop/start (`docs/sandbox-environment.md`), per-version isolated because
# pex keys the cache by the pex's own content hash, and requiring no PEX_*/SHELLBOX_* variable set
# (AC-5). So the correct choice is to set no root and let the default apply.
RAW_PEX="$BUILD_DIR/shellbox.raw"
pex \
  --lock "$LOCK" \
  --find-links "$WHEELS_DIR" \
  --complete-platform "$COMPLETE_PLATFORM_FILE" \
  --no-build \
  --sources-directory "$ENTRY_DIR" \
  --entry-point _shellbox_entry:main \
  --python-shebang "#!/usr/bin/env python3" \
  --no-emit-warnings \
  --venv prepend \
  -o "$RAW_PEX"

# ---- 5. Embed SHELLBOX-MANIFEST.json ------------------------------------------------------
# A pex is a zip, so the manifest is injected as a top-level member after the build. It records
# what `check_artifact.py` reads: the resolved distributions (name/version/tag/hash/url from the
# rewritten lock), the bundled `.so` list (AC-17's source of truth), the pinned websockets, the
# build tag and glibc ceiling, and the release-notes fields. This is why "which websockets did we
# ship" is answerable from the artifact alone, in the field, with no network.
python3 - "$RAW_PEX" "$LOCK" "$SIDECAR" "$VERSION" "$GIT_SHA" "$BUILD_TAG" <<'PY'
import hashlib, json, sys, zipfile
from datetime import datetime, timezone

raw_pex, lock_path, sidecar_path, version, git_sha, build_tag = sys.argv[1:7]
lock = json.load(open(lock_path))
sidecar = json.load(open(sidecar_path))

# Collect distributions from the lock: name, version, the single artifact url + hash, and the tag
# parsed from the wheel filename. Schema-independent walk, matching check_pex_lock.py.
def walk_reqs(node):
    if isinstance(node, dict):
        if "project_name" in node and "artifacts" in node:
            yield node
        for v in node.values():
            yield from walk_reqs(v)
    elif isinstance(node, list):
        for item in node:
            yield from walk_reqs(item)

def tag_of(url):
    # ".../<name>-<ver>-<py>-<abi>-<plat>.whl" -> "<py>-<abi>-<plat>"; sdist -> "sdist".
    fn = url.rsplit("/", 1)[-1]
    if fn.endswith(".whl"):
        parts = fn[:-4].split("-")
        return "-".join(parts[-3:]) if len(parts) >= 5 else "-".join(parts[2:])
    return "sdist"

dists = []
websockets_version = ""
for req in walk_reqs(lock):
    arts = req.get("artifacts") or []
    if not arts:
        continue
    art = arts[0]
    url = art.get("url", "")
    name = req["project_name"]
    ver = req.get("version", "")
    dists.append({"name": name, "version": ver, "tag": tag_of(url),
                  "hash": art.get("hash", ""), "url": url})
    if name.lower() == "websockets":
        websockets_version = ver

with zipfile.ZipFile(raw_pex) as z:
    so_files = sorted(n for n in z.namelist() if n.endswith(".so"))

python_floor = "3.12"
manifest = {
    "schema": 1,
    "name": "shellbox",
    "server_name": "shellbox",
    "version": version,
    "git_sha": git_sha,
    "build_date": datetime.now(timezone.utc).isoformat(),
    "python_floor": python_floor,
    "build_platform_tag": build_tag,
    "websockets": websockets_version,
    "distributions": dists,
    "extension_modules": so_files,
    "release_notes": {
        "python_floor": python_floor,
        "websockets": websockets_version,
        "platform_tag": build_tag,
        "git_sha": git_sha,
        "glibc_ceiling": sidecar.get("glibc", ""),
    },
}
with zipfile.ZipFile(raw_pex, "a") as z:
    z.writestr("SHELLBOX-MANIFEST.json", json.dumps(manifest, indent=2))
PY

# ---- 6. Finalize: name, mode, checksum ----------------------------------------------------
ARTIFACT="$DIST_DIR/$ARTIFACT_NAME"
cp "$RAW_PEX" "$ARTIFACT"
chmod 0755 "$ARTIFACT"

# `<hash>␣␣<filename>`, the `sha256sum -c` format, with the bare filename so `-c` works from within
# dist/. Written beside the asset and published as a sibling release asset.
( cd "$DIST_DIR" && python3 -c "
import hashlib, sys
name = '$ARTIFACT_NAME'
digest = hashlib.sha256(open(name, 'rb').read()).hexdigest()
open(name + '.sha256', 'w').write(f'{digest}  {name}\n')
print(f'{digest}  {name}')
" )

echo ">> built $ARTIFACT ($(wc -c < "$ARTIFACT") bytes)"
