"""The App's logging configuration, and why the prober does not work without it.

## The measurement this file exists because of

Before this module the App configured no logging at all. Measured 2026-08-03 against the live
`dev` deploy: `databricks apps logs` carried **neither** the ``opened the Lakebase registry at
...`` INFO line nor the ``could not open the Lakebase registry`` WARNING that
`packages/shellbox-app/src/shellbox_app/database.py` emits. So nobody could tell whether the
App's database wiring worked at all.

Python's reason is not subtle. With no handler on the root logger, a record below WARNING is
dropped, and a record at WARNING or above reaches stderr only through the **last-resort**
handler in the standard library. That handler is an implementation detail with no format, no
timestamp, and no promise that it stays.

## Why this is not optional for the prober

`packages/shellbox-app/src/shellbox_app/ready.py` states that the prober's failure path **is** a
WARN log line, and that the line is the whole notification mechanism. Leaning on the last-resort
handler for the only signal that design has is exactly the undeclared-default dependency this
repo refuses elsewhere -- ``pool_timeout``, ``ws_ping_interval`` and ``ws_ping_timeout`` are all
passed explicitly for the same reason. So the App declares its logging.

## What this configures, and what it deliberately leaves alone

It adds one handler to the **root** logger, at INFO, writing to stdout.

Root rather than the ``shellbox_app`` logger alone, because `shellbox_registry` logs the
credential path and that is the other half of the same diagnostic.

It does not fight uvicorn. Read from the installed uvicorn 0.51.0
(``uvicorn.config.LOGGING_CONFIG``): that dict configures the ``uvicorn``, ``uvicorn.error`` and
``uvicorn.access`` loggers, sets ``disable_existing_loggers: false``, declares **no root
logger**, and sets ``propagate: false`` on ``uvicorn`` and ``uvicorn.access``. Both halves of
that reading matter:

* No root entry and ``disable_existing_loggers: false`` mean ``uvicorn.run`` leaves the handler
  below in place, whichever order the two run in.
* ``propagate: false`` means uvicorn's own records never reach the root handler, so nothing is
  logged twice.

NOTE: stdout rather than stderr, which is where uvicorn's default handler writes. The Apps
runtime collects both, and keeping the two streams distinct means an operator reading a log can
tell an App line from a server line without parsing the format.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

__all__ = ["LOG_FORMAT", "configure_logging"]

# A timestamp, a level, and the logger name. The name is what tells an operator whether a line
# came from the relay, the registry or the prober, and the last-resort handler this replaces
# carried none of the three.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Marks the handler this module installed, so a second call finds it instead of adding a
# duplicate. There are two call sites, on purpose -- see `configure_logging`.
_INSTALLED = "_shellbox_app_handler"


def configure_logging(stream: TextIO | None = None) -> logging.Handler:
    """Send this process's logs to stdout at INFO, and return the handler.

    CRITICAL: it is called from `packages/shellbox-app/src/shellbox_app/__main__.py` **before**
    that module imports the server, and the ordering is the whole point. `shellbox_app.server`
    builds the deployed ``app`` object at module scope, and building it calls `open_registry`,
    which logs whether the App resolved a Lakebase endpoint and what happened when it opened
    one. Configuring logging after that import loses the two lines an operator most wants:
    measured on a live run, ``GET /ready`` reported ``no_database`` while the log carried no
    trace of the App having looked for an endpoint at all.

    It is NOT called at import of this package. Every test in the App lane imports
    `shellbox_app.server`, and a package that reconfigures logging on import takes over the test
    runner's own capture.

    IDEMPOTENT, because there are two call sites: `__main__` for the import-time lines, and
    `main` in `packages/shellbox-app/src/shellbox_app/server.py` so that calling it directly --
    which is what `app.yaml` would reach through any other entrypoint -- still configures
    logging. A second call returns the handler the first one installed.

    ``stream`` is a seam for the test that asserts a WARNING actually arrives. The handler is
    returned so a caller can remove it again; neither production call site does.
    """
    root = logging.getLogger()
    for existing in root.handlers:
        if getattr(existing, _INSTALLED, False):
            return existing

    handler = logging.StreamHandler(sys.stdout if stream is None else stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    setattr(handler, _INSTALLED, True)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler
