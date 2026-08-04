"""`W34`: what `static/` ships, what it must never reference, and that the App serves it.

Three groups of assertion, and they exist for three different failures:

1. **No absolute URL anywhere under `static/`.** The Apps edge authenticates the request that
   fetched the page; a CDN reference is an unauthenticated third-party dependency on the load
   path of an authenticated page, and it fails closed in the environments that matter most.
   Vendoring is the decision, and this is the assertion that keeps it one.
2. **The vendored bundles are the bytes `vendor/README.md` says they are.** There is no
   lockfile for browser assets and no build step, so the recorded hash IS the integrity check.
3. **The mount answers.** `GET /` must stay JSON -- it is the deploy's smoke target -- and the
   page must be reachable with its assets resolving, which is what the trailing-slash redirect
   is for.

NOTE: none of this executes any JavaScript. See `tests/unit/test_client_parity.py` for what is
and is not covered above the protocol layer.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shellbox_app.database import AppDatabase
from shellbox_app.server import build_app
from shellbox_app.ui import STATIC_ROOT, UI_PATH
from shellbox_registry import NullRegistry

REPO = Path(__file__).resolve().parents[2]
VENDOR = STATIC_ROOT / "vendor"

# The files the page loads, all of them same-origin and relative.
AUTHORED = ("index.html", "app.css", "codec.js", "protocol.js", "terminal.js")

# `https://x`, `http://x`, and the protocol-relative `//x` that is easy to miss. Deliberately
# NOT matched inside this test file's own strings -- it reads the shipped files.
_ABSOLUTE_URL = re.compile(r"""(?:https?:)?//[a-z0-9][a-z0-9.-]*\.[a-z]{2,}""", re.IGNORECASE)

# A JavaScript line comment, which is where a URL legitimately appears (a doc reference). The
# check below strips these first, so prose may cite a URL and markup may not use one.
_JS_COMMENT = re.compile(r"^\s*(//|\*|/\*).*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _recorded_hashes() -> dict[str, str]:
    """The sha256 column of `vendor/README.md`'s table, keyed by filename.

    Parsed from the document rather than restated here, so the table a human reads and the
    values a test enforces cannot become two different claims.
    """
    rows = re.findall(
        r"^\|\s*`([^`]+)`\s*\|.*\|\s*`([0-9a-f]{64})`\s*\|$",
        (VENDOR / "README.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert len(rows) >= 4, (
        "vendor/README.md's hash table did not parse. It is the only integrity record for "
        f"assets that have no lockfile; parsed rows: {rows}"
    )
    return dict(rows)


def test_the_static_root_ships_with_the_package() -> None:
    """It is inside `shellbox_app/`, which is what makes `deploy-app.sh`'s `cp -R` carry it."""
    assert STATIC_ROOT.is_dir()
    assert STATIC_ROOT.parent.name == "shellbox_app"
    assert (STATIC_ROOT / "index.html").is_file()


def test_the_deploy_script_copies_the_package_that_holds_it() -> None:
    """No separate staging step exists, and this is why none is needed.

    `scripts/deploy-app.sh` copies `shellbox_app/` whole. If `static/` ever moved out of the
    package it would stop being deployed, and the App would 404 its own page behind a green
    deploy -- the exact shape of failure this phase keeps removing.
    """
    script = (REPO / "scripts" / "deploy-app.sh").read_text(encoding="utf-8")
    assert "packages/shellbox-app/src/shellbox_app" in script


@pytest.mark.parametrize("name", AUTHORED)
def test_no_authored_asset_names_an_absolute_url(name: str) -> None:
    """Vendored means vendored. A CDN reference fails here rather than in somebody's browser."""
    source = (STATIC_ROOT / name).read_text(encoding="utf-8")
    stripped = _HTML_COMMENT.sub("", source)
    stripped = _JS_COMMENT.sub("", stripped)
    found = _ABSOLUTE_URL.findall(stripped)
    assert found == [], (
        f"static/{name} references {found}. Every asset must be same-origin and vendored: the "
        "Apps edge authenticates the request that fetched the page, and a third-party host is "
        "an unauthenticated dependency on its load path. See static/vendor/README.md."
    )


def test_the_url_check_can_actually_fail() -> None:
    """Non-vacuity. A regex broken by an edit would pass every case above in silence."""
    assert _ABSOLUTE_URL.findall('<script src="https://unpkg.com/x.js">')
    assert _ABSOLUTE_URL.findall('<link href="//cdn.jsdelivr.net/x.css">')
    assert _ABSOLUTE_URL.findall("@import url(http://evil.example.com/a.css);")
    assert _ABSOLUTE_URL.findall('src="vendor/xterm.js"') == []


@pytest.mark.parametrize("name", ["xterm.js", "xterm.css", "addon-fit.js", "LICENSE"])
def test_a_vendored_asset_is_the_bytes_the_readme_records(name: str) -> None:
    path = VENDOR / name
    assert path.is_file(), f"static/vendor/{name} is missing; see static/vendor/README.md"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == _recorded_hashes()[name], (
        f"static/vendor/{name} is {digest}, and vendor/README.md records "
        f"{_recorded_hashes()[name]}. Browser assets have no lockfile, so that table is the "
        "only integrity record they have -- refresh the asset and the row together."
    )


def test_the_vendored_bundles_define_what_the_page_expects() -> None:
    """`index.html` loads these as plain scripts and reads two globals off `window`."""
    assert "FitAddon" in (VENDOR / "addon-fit.js").read_text(encoding="utf-8")
    assert "Terminal" in (VENDOR / "xterm.js").read_text(encoding="utf-8")


def test_the_renderer_never_builds_markup_from_a_string() -> None:
    """Inventory rows carry text a SANDBOX wrote -- `owner_email`, `cwd`, `tmux_name`.

    They are rendered through `textContent` and `createElement`, which cannot execute markup.
    One `innerHTML` with a template literal is how that stops being true, and it would not look
    like a security change when it was written.
    """
    for name in ("terminal.js", "protocol.js", "codec.js"):
        source = (STATIC_ROOT / name).read_text(encoding="utf-8")
        stripped = _JS_COMMENT.sub("", source)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            assert banned not in stripped, f"static/{name} uses {banned}"


def _client() -> TestClient:
    return TestClient(build_app(database=AppDatabase(registry=NullRegistry())))


def test_the_page_is_served_with_its_assets() -> None:
    with _client() as client:
        page = client.get(f"{UI_PATH}/")
        assert page.status_code == 200
        assert "<title>shellbox</title>" in page.text

        for asset in ("app.css", "terminal.js", "protocol.js", "codec.js", "vendor/xterm.js"):
            served = client.get(f"{UI_PATH}/{asset}")
            assert served.status_code == 200, asset
            assert served.content, asset


def test_the_bare_prefix_redirects_to_the_trailing_slash() -> None:
    """Not cosmetic. Every asset in `index.html` is referenced RELATIVELY, so served at `/ui`
    the browser would resolve `app.css` against `/` and fetch a path that is not mounted."""
    with _client() as client:
        answer = client.get(UI_PATH, follow_redirects=False)
        assert answer.status_code in (301, 307, 308)
        assert answer.headers["location"] == f"{UI_PATH}/"


def test_the_redirect_names_no_host_of_its_own() -> None:
    """The assertion this file was missing, and the live run is what found it missing.

    `StaticFiles` redirects a directory URL using ``URL(scope=scope)``, an ABSOLUTE url built
    from the ASGI scope. The Apps edge terminates outside the container and proxies in, so the
    App sees ``host: localhost:8000`` -- and MEASURED 2026-08-04 against the live `dev` App, the
    bare ``/ui`` answered ``307`` with ``location: https://localhost:8000/ui/``, which a browser
    follows to its own loopback.

    The previous assertion here was ``endswith("/ui/")``, which that broken value satisfies. It
    passed under `TestClient`, where the host happens to be correct, and shipped anyway. A
    relative reference cannot carry a wrong host because it carries no host at all.
    """
    with _client() as client:
        location = client.get(UI_PATH, follow_redirects=False).headers["location"]
        assert "://" not in location, (
            f"the redirect names a host ({location}). Behind the Apps edge the only host this "
            "process can see is the loopback it is proxied into, so an absolute location sends "
            "the browser to itself."
        )
        assert location.startswith("/")


def test_the_health_route_is_still_json_and_names_the_page() -> None:
    """`GET /` is the deploy's smoke target. It must not become HTML, and it must not gain a
    database call -- `scripts/verify_app.py` asserts on this payload."""
    with _client() as client:
        answer = client.get("/")
        assert answer.status_code == 200
        payload = answer.json()
        assert payload["service"] == "shellbox-app"
        assert payload["ui"] == f"{UI_PATH}/"


def test_the_mount_did_not_shadow_a_route() -> None:
    """A mount matches by PREFIX, so one registered too early would swallow its neighbours."""
    with _client() as client:
        assert client.get("/ready").json()["ready"] is False
        assert "hosts" in client.get("/api/hosts").json()
