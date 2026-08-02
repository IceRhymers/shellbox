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
"""

from __future__ import annotations

from shellbox_app.server import main

if __name__ == "__main__":
    main()
