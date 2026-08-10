"""``python -m shellbox_app`` -- the command ``app.yaml`` runs.

Deliberately the entrypoint rather than ``uvicorn shellbox_app.server:app`` on the command
line, which is the shape `probe/app.yaml` used. The reason is the port.

The Apps runtime supplies ``DATABRICKS_APP_PORT``, and ``uvicorn --port`` takes a literal.
Writing ``--port "${DATABRICKS_APP_PORT:-8000}"`` in ``app.yaml`` assumes the Apps runtime
expands shell syntax inside ``command``, and **that is not verified** -- nothing in this repo
has measured it. `probe/app.yaml` sidestepped the question by hardcoding 8000, which worked
because the probe measured the edge proxying to ``localhost:8000``, but a hardcoded port is a
guess about the runtime rather than a reading of it.

So the resolution happens here, in ``os.environ``, where the semantics are ones this repo can
test. 8000 is the fallback because that is the port the probe measured in use, not because it
is a safe-looking default.

CRITICAL: the two statements below are in this order deliberately. See the import comment.
"""

from __future__ import annotations

from shellbox_app.logs import configure_logging

# BEFORE the server import, and this is not stylistic. `shellbox_app.server` builds the deployed
# `app` object at module scope, and building it opens the registry -- which logs whether an
# endpoint was resolved and what happened when it was opened. Those two lines are the answer to
# "does the App's database wiring work", and configuring logging after this import drops them.
# `packages/shellbox-app/src/shellbox_app/logs.py` records the live measurement.
#
# NOTE: ruff's import-order rule is suppressed rather than satisfied. Moving this call above the
# import is the behaviour being asked for, so a sorted import block would be the bug.
configure_logging()

from shellbox_app.server import main  # noqa: E402 -- see above

if __name__ == "__main__":
    main()
