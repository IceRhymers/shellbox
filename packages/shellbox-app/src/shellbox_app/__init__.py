"""The App half of the shellbox transport -- a deployable, not a library.

This package holds the server end of the WebSocket the sandbox dials out to. The client end
lives in ``shellbox_mcp.transport``; the frame protocol both ends share lives in
``shellbox_transport``, which is pure and has no runtime dependencies. ``server.py`` documents
the accept path and the frame contract. This file records the two package-level facts a reader
needs before touching either.

WARNING: **Phase 4 inherits this package as source to write, not as a module to import.** That
is a stated and accepted cost, not an oversight. The transport is split on **purity** rather
than on client-versus-server, so the shared package stays free of I/O, of tmux, and of any web
framework -- which is what stops the App installing the MCP SDK, the Databricks SDK, and the
tmux adapter to get a WebSocket handler. The price is that the server half has no library
home, so it ships inside the thing that serves it. Phase 4 grows this package rather than
depending on it, and Phase 4's issue must record that. If you are reading this from Phase 4 and
the issue does not say so, the amendment was missed.

**This App holds no capability over any sandbox, and cannot be given one.** Three measured
facts, all recorded in `probe/FINDINGS.md`:

* The user token the Apps edge injects as ``x-forwarded-access-token`` carries scope
  ``default``, so a workspace REST call returns ``403 Invalid scope, required scopes:
  all-apis``.
* This App's own service principal is caller-scoped. It sees only sandboxes it created itself,
  never the caller's. The probe proved that in both directions with a create, list and delete
  round trip.
* Outbound TCP from an App container to a sandbox gateway fails with ``[Errno 113] No route to
  host`` in 1 ms.

So the App cannot list, start, or stop the caller's sandbox with any identity the Apps runtime
provides, and the third fact is why the sandbox dials out rather than being dialled. The
direction is forced, not chosen -- `docs/architecture.md` states it and the probe measured it.
Every design where the App reaches into the sandbox is dead. Do not add one.

CRITICAL: **``X-Forwarded-Email`` is for identity DISPLAY only. It is never authorization.**
That is decision D5 of the epic, https://github.com/IceRhymers/shellbox/issues/9. The edge
injects the header and it survives the upgrade -- the probe measured ``gap-auth`` on the 101
response itself -- which makes it usable in a ``hello`` frame so a viewer can see whose session
they are reading. It does not make it a permission check. An authorization rule here needs a
credential from outside the App, and the rider section of `probe/FINDINGS.md` explains why.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
