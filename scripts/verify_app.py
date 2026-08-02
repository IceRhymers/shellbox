"""Assert a deployed `shellbox-app` actually relays a frame through the Apps edge.

`W24`'s first half: the App is deployed, reachable, and both socket roles work in production.
The second half -- a sandbox publisher holding a real pty dialling in -- needs the credential
chain and stays manual.

CRITICAL: **Every assertion here is on response CONTENT, not on a status code.** The Phase 1
probe measured an unauthenticated POST to the Apps edge returning **HTTP 200 with an HTML login
body**, so a status-only check reads an auth failure as success. That is `probe/FINDINGS.md`
constraint 6, and it is why the health check below parses JSON and the socket checks decode a
frame rather than trusting the 101.

Run it through `scripts/deploy-app.sh --verify`, which supplies both variables.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request

from shellbox_transport import Frame, Stream
from shellbox_transport.codec import decode_frame, encode_frame

APP_URL = os.environ["SHELLBOX_APP_URL"].rstrip("/")
TOKEN = os.environ["SHELLBOX_EDGE_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Colon-bearing on purpose: this is the global `<host_id>:<tmux_name>` shape from
# `naming.session_id`, and `W23` made it the wire identity. Verifying it here is what proves
# the edge's path routing carries it -- a `:` in a URL path segment is legal but not universally
# handled, and finding that out during the live run would be finding it out too late.
SESSION_ID = "livecheck-host:build"


def check_health() -> None:
    request = urllib.request.Request(f"{APP_URL}/", headers=HEADERS)  # noqa: S310 - https only
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        body = response.read().decode()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise AssertionError(
            f"GET / returned non-JSON, which is what the edge's login page looks like: {body[:200]!r}"
        ) from None
    assert payload.get("service") == "shellbox-app", f"unexpected health payload: {payload}"
    print(f"health   : {payload}")


async def check_sockets() -> None:
    from websockets.asyncio.client import connect

    started = time.monotonic()
    async with connect(f"{APP_URL.replace('https://', 'wss://')}/subscribe/{SESSION_ID}",
                       additional_headers=HEADERS) as subscriber:
        upgrade_ms = (time.monotonic() - started) * 1000
        identity = subscriber.response.headers.get("gap-auth")
        hello = decode_frame(await asyncio.wait_for(subscriber.recv(), timeout=30))
        assert json.loads(hello.data)["kind"] == "hello", f"first frame was not a hello: {hello}"
        print(f"subscribe: 101 in {upgrade_ms:.0f} ms, gap-auth={identity}")

        async with connect(f"{APP_URL.replace('https://', 'wss://')}/publish/{SESSION_ID}",
                           additional_headers=HEADERS) as publisher:
            greeting = decode_frame(await asyncio.wait_for(publisher.recv(), timeout=30))
            assert json.loads(greeting.data)["kind"] == "hello"
            print("publish  : 101, hello received")

            relayed = time.monotonic()
            payload = b"hello from the edge"
            await publisher.send(
                encode_frame(
                    Frame(session_id=SESSION_ID, seq=1, t=time.time(),
                          stream=Stream.STDOUT, data=payload)
                )
            )
            got = decode_frame(await asyncio.wait_for(subscriber.recv(), timeout=30))
            assert got.data == payload, f"relayed {got.data!r}, expected {payload!r}"
            assert got.session_id == SESSION_ID, "the relayed frame lost its session identity"
            print(f"relay    : {got.data!r} seq={got.seq} in {(time.monotonic()-relayed)*1000:.0f} ms")


def main() -> int:
    try:
        check_health()
        asyncio.run(check_sockets())
    except AssertionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    print("\nthe deployed App relays a frame end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
