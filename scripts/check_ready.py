"""Assert a deployed `shellbox-app` can read its registry AS ITSELF.

The last step of `scripts/deploy.sh`, and the one thing the steps before it cannot prove.
`make grant` reads its own grant back with ``has_table_privilege``, which proves the catalog
agrees. It does not prove the App's own credential path works: the App mints a Lakebase token
as its service principal and connects with it, and only the App can exercise that.

A deploy that reported success while the App could connect but not read is the exact state this
check exists to fail. Without it a broken grant is invisible until a human opens the inventory
page, because ``GET /`` is zero-database by design -- see
`packages/shellbox-app/src/shellbox_app/ready.py`.

CRITICAL: **the assertion is on the response CONTENT, not on a status code.** The Phase 1 probe
measured an unauthenticated request to the Apps edge returning **HTTP 200 with an HTML login
body** (`probe/FINDINGS.md` constraint 6), so a status check reads an authentication failure as
success. `scripts/verify_app.py` is built the same way and for the same reason. This parses the
body and asserts ``ready`` is exactly ``True``.

Standard library only, so it runs with `python3` and needs no `uv` environment. That matches
`scripts/check_lockfile.py`, whose Makefile comment states the rule.

Usage:  SHELLBOX_APP_URL=... SHELLBOX_EDGE_TOKEN=... python3 scripts/check_ready.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# The App wakes a suspended Lakebase endpoint on this call. The measured cold connect is 1.4 s
# and the App's own `pool_timeout` is 5 s, so this bounds the HTTP request generously above
# both rather than racing either.
TIMEOUT_SECONDS = 60


def check_ready(app_url: str, token: str) -> None:
    request = urllib.request.Request(  # noqa: S310 -- https, from `databricks apps get`
        f"{app_url.rstrip('/')}/ready",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body = response.read().decode()
    except urllib.error.HTTPError as error:
        # Read the body anyway. The route answers 200 even when it is not ready, so an HTTP
        # error here is the edge or the platform, and its body is the diagnostic.
        raise AssertionError(
            f"GET /ready returned HTTP {error.code}, which is the edge or the App failing to "
            f"serve at all rather than a readiness answer: {error.read().decode()[:400]!r}"
        ) from None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise AssertionError(
            "GET /ready returned non-JSON, which is what the edge's login page looks like. "
            f"The token was refused, so the request never reached the App: {body[:400]!r}"
        ) from None

    if payload.get("ready") is not True:
        raise AssertionError(
            f"the App is not ready: {payload}.\n"
            "  The App is running and answering, and it cannot read its registry as itself.\n"
            "  reason=no_database  means the App resolved no Lakebase endpoint from its\n"
            "    environment. Check the `env:` block scripts/deploy-app.sh stamped into\n"
            "    app.yaml.\n"
            "  reason=query_failed means the read raised. The service principal's SELECT\n"
            "    grant is the first suspect; see docs/deploy.md section 4.\n"
            "  Either way the App's own log carries the driver error, which this body\n"
            "  deliberately does not: `databricks apps logs <app> --profile <profile>`."
        )

    print(f"ready    : {payload}")


def main() -> int:
    try:
        check_ready(os.environ["SHELLBOX_APP_URL"], os.environ["SHELLBOX_EDGE_TOKEN"])
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
