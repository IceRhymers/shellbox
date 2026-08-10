"""Serving `static/` -- the renderer's HTML, its stylesheet, its modules, and vendored xterm.js.

`W34`. One mount, and the decisions worth stating are all about WHERE it goes and what it is
allowed to reach.

## Why the page is at ``/ui/`` and not at ``/``

``GET /`` is the deploy's own smoke target and it returns JSON. `scripts/verify_app.py` asserts
``service == "shellbox-app"`` against it, `scripts/deploy.sh` calls it, and the rule that it
touches NO database is what makes it answerable when Lakebase is the thing that is broken --
see the module docstring of `packages/shellbox-app/src/shellbox_app/server.py`. Serving HTML
there instead, or content-negotiating on ``Accept``, would make the one check that must work
during an outage depend on which client asked. So the page gets its own prefix, and
`health_payload` carries `UI_PATH` as a field so a human who curls the root is told where to go.

## Why a mount and not a route per file

``StaticFiles`` is Starlette's own, it arrives with fastapi, and it resolves paths against the
directory with the traversal check already written. A hand-rolled ``FileResponse`` route taking
a filename parameter is the shape that ships a ``../../etc/passwd`` bug, and there is no reason
to write one.

``html=True`` is what makes ``/ui/`` serve ``index.html``. That trailing slash matters more than
it looks: every asset in `index.html` is referenced RELATIVELY, so without it the browser would
resolve ``app.css`` against ``/`` and fetch ``/app.css``, which is not mounted. Relative
references are themselves deliberate -- they are what keeps the page free of any absolute URL,
which `tests/unit/test_static_assets.py` asserts.

## Why ``/ui`` gets its own redirect instead of ``StaticFiles``'s

``StaticFiles`` already redirects a directory URL to its trailing-slash form, and **its redirect
is unusable behind the Apps edge**. MEASURED 2026-08-04 against the live `dev` App: a request to
``/ui`` answered ``307`` with ``location: https://localhost:8000/ui/``. A browser follows that to
its own loopback and fails.

The cause is that ``StaticFiles`` builds the target with ``URL(scope=scope)``, an ABSOLUTE url
taken from the ASGI scope -- and the edge terminates outside the container and proxies in, so the
App sees ``host: localhost:8000`` (the same value the Phase 1 probe recorded). Nothing the App
can configure fixes that from inside: uvicorn's proxy-header handling covers the client address
and the scheme, not a rewritten ``Host``.

So the redirect below is registered FIRST and answers ``/ui`` itself, with a ROOT-RELATIVE
``location``. A relative reference is resolved by the browser against the origin it actually
used, which is the one place the real hostname is reliably known. `StaticFiles` never sees the
bare path, so its own redirect is unreachable.

CRITICAL: `tests/unit/test_static_assets.py` asserts the location carries **no scheme**. The
earlier assertion was that it merely ENDED in ``/ui/``, which
``https://localhost:8000/ui/`` satisfies -- so it passed through `TestClient`, where the host
happens to be right, and shipped a page whose bare URL was broken in a browser. The live run in
`W38` is what caught it.

## What a mount does NOT do, and why that is fine here

A ``Mount`` is not an ``APIRoute``, so it does not appear in the route table that
`tests/unit/test_app_database.py` sweeps for "every database-touching route is a sync ``def``".
That sweep is not weakened by this: ``StaticFiles`` reads files from disk and never touches the
registry, so it is outside the rule rather than an exception to it. It does its file I/O in a
threadpool, so it does not block the event loop that relays every attached terminal either.

CRITICAL: **nothing under `static/` is authorization.** The Apps edge authenticates every
request that reaches this App, and under decision D6 the App is open to every workspace user.
The page therefore ships to anyone the edge lets through, exactly as the inventory routes do.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

__all__ = ["STATIC_ROOT", "UI_PATH", "mount_ui"]

# The directory this package ships. Resolved from `__file__` rather than through
# `importlib.resources`, because the deploy root is a plain directory tree that
# `scripts/deploy-app.sh` copies with `cp -R` -- there is no wheel, no editable install and no
# package metadata at runtime, so a resource API would be answering a question nobody asked.
STATIC_ROOT = Path(__file__).resolve().parent / "static"

UI_PATH = "/ui"


def mount_ui(api: FastAPI) -> None:
    """Mount `static/` at `UI_PATH`. Called once, by ``build_app``.

    NOTE: this raises if `STATIC_ROOT` is missing, and that is deliberate rather than an
    oversight. The directory is part of the package, so its absence means a partial or broken
    deploy root -- and the App failing to import is caught by `scripts/deploy.sh`'s last step,
    which is loud. The alternative, mounting conditionally, would serve a 404 page from an App
    that reported itself healthy, which is the class of failure this phase keeps eliminating.

    It is deliberately NOT the same rule the registry follows. A missing registry is an
    ENVIRONMENT failure that must degrade the inventory and never the relay; a missing `static/`
    is a BUILD failure, and there is nothing to degrade to.
    """

    @api.get(UI_PATH, include_in_schema=False)
    def ui_root() -> RedirectResponse:
        """Send the bare path to its trailing-slash form, ROOT-RELATIVELY.

        Registered before the mount so `StaticFiles` never sees this path -- see this module's
        docstring for the measurement that made this necessary. The value below must stay a
        relative reference: an absolute one would have to name a host, and the only host this
        process can see is the loopback the edge proxies into.
        """
        return RedirectResponse(f"{UI_PATH}/", status_code=307)

    api.mount(UI_PATH, StaticFiles(directory=STATIC_ROOT, html=True), name="ui")
