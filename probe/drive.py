"""Drive the Phase 1 probe app. See IceRhymers/shellbox#1.

Run this from INSIDE a Lakebox sandbox (that's the point — the sandbox's egress
and token situation is under test), with a workspace OAuth token.

  python3 drive.py --base https://<app>.databricksapps.com --token "$TOKEN" \
      [--sse-minutes 11] [--ws] [--sse] [--post] [--egress HOST:PORT]

With no lane flags it runs ws + sse + post.

Writes newline-delimited JSON of every observation to --out (default probe-results.jsonl)
so a long SSE run can be inspected while still in flight.
"""

import argparse
import base64
import json
import os
import ssl
import socket
import sys
import time
import urllib.error
import urllib.request

OUT = None


def rec(kind, **fields):
    """Append one observation, flush immediately so long runs are inspectable."""
    row = {"kind": kind, "at": time.time(), **fields}
    line = json.dumps(row)
    print(line, flush=True)
    if OUT:
        OUT.write(line + "\n")
        OUT.flush()
    return row


def auth_headers(token):
    return {"Authorization": "Bearer " + token}


# ---------------------------------------------------------------- POST baseline

def lane_post(base, token):
    url = base.rstrip("/") + "/in"
    body = json.dumps({"hello": "probe", "sent_at": time.time()}).encode()
    req = urllib.request.Request(
        url, data=body, headers={**auth_headers(token), "Content-Type": "application/json"}
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read().decode()
            rec(
                "post",
                ok=True,
                status=r.status,
                rtt_s=round(time.time() - t0, 3),
                body=payload[:400],
            )
    except urllib.error.HTTPError as e:
        rec("post", ok=False, status=e.code, body=e.read().decode()[:400],
            location=e.headers.get("Location"))
    except Exception as e:
        rec("post", ok=False, error=type(e).__name__, detail=str(e))


# ------------------------------------------------------- WS: raw upgrade attempt

def lane_ws_raw(base, token):
    """Hand-rolled HTTP Upgrade over TLS so we see the true status line.

    Deliberately not using a WS library here: libraries mask the difference
    between a 302 from the edge and a 400 from the app.
    """
    from urllib.parse import urlparse

    u = urlparse(base)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    key = base64.b64encode(os.urandom(16)).decode()

    req = (
        "GET /ws HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {token}\r\n"
        "\r\n"
    )

    t0 = time.time()
    try:
        raw = socket.create_connection((host, port), timeout=20)
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.sendall(req.encode())
        raw.settimeout(20)
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = raw.recv(4096)
            if not chunk:
                break
            buf += chunk
        raw.close()
        head = buf.decode(errors="replace").split("\r\n\r\n")[0]
        status_line = head.split("\r\n")[0] if head else ""
        headers = {}
        for ln in head.split("\r\n")[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        rec(
            "ws_raw",
            status_line=status_line,
            status=int(status_line.split()[1]) if len(status_line.split()) > 1 else None,
            upgrade=headers.get("upgrade"),
            location=headers.get("location"),
            server=headers.get("server"),
            rtt_s=round(time.time() - t0, 3),
            head=head[:900],
        )
    except Exception as e:
        rec("ws_raw", ok=False, error=type(e).__name__, detail=str(e),
            rtt_s=round(time.time() - t0, 3))


# ------------------------------------------------- WS: real client, if it upgraded

def lane_ws_client(base, token, frames=5):
    try:
        from websockets.sync.client import connect
    except Exception as e:
        rec("ws_client", skipped=True, reason="websockets not installed: %s" % e)
        return

    url = base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    try:
        with connect(url, additional_headers=auth_headers(token), open_timeout=20) as c:
            hello = c.recv()
            rec("ws_client", event="open", hello=hello[:300])
            for i in range(frames):
                sent = time.time()
                c.send(json.dumps({"i": i, "sent_at": sent}))
                reply = c.recv()
                now = time.time()
                srv = json.loads(reply).get("sent_at")
                rec(
                    "ws_frame",
                    i=i,
                    rtt_ms=round((now - sent) * 1000, 1),
                    server_to_client_ms=round((now - srv) * 1000, 1) if srv else None,
                )
                time.sleep(1)
            rec("ws_client", event="closed_ok", frames=frames)
    except Exception as e:
        detail = str(e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        rec("ws_client", ok=False, error=type(e).__name__, status=status, detail=detail[:400])


# ----------------------------------------------------------------------- SSE lane

def lane_ws_hold(base, token, minutes, interval=20):
    """Hold one WS open for `minutes`, pinging periodically.

    The point is token expiry: the OAuth token used for the handshake dies at
    ~1h, and Phase 3 needs to know whether the edge tears the socket down when
    it does, or whether the connection outlives the credential that opened it.
    """
    try:
        from websockets.sync.client import connect
    except Exception as e:
        rec("ws_hold", skipped=True, reason=str(e))
        return

    url = base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    deadline = time.time() + minutes * 60
    t0 = time.time()
    i = 0
    try:
        with connect(url, additional_headers=auth_headers(token), open_timeout=30) as c:
            c.recv()  # server-hello
            rec("ws_hold", event="open", hold_minutes=minutes)
            while time.time() < deadline:
                sent = time.time()
                c.send(json.dumps({"i": i, "sent_at": sent}))
                reply = c.recv(timeout=interval + 30)
                now = time.time()
                rec(
                    "ws_hold_frame",
                    i=i,
                    rtt_ms=round((now - sent) * 1000, 1),
                    held_s=round(now - t0, 1),
                    held_min=round((now - t0) / 60, 2),
                    ok=bool(reply),
                )
                i += 1
                time.sleep(interval)
        rec("ws_hold", event="closed_clean", held_min=round((time.time() - t0) / 60, 2), frames=i)
    except Exception as e:
        rec(
            "ws_hold",
            ok=False,
            error=type(e).__name__,
            detail=str(e)[:300],
            died_at_min=round((time.time() - t0) / 60, 2),
            frames=i,
        )


def lane_sse(base, token, minutes, interval=2, nobuf=1):
    """Read the stream frame by frame, recording arrival vs send time.

    Buffering shows up as several frames arriving together with sent_at values
    spread across the interval — that gap is the whole signal.
    """
    url = base.rstrip("/") + f"/sse?minutes={minutes}&interval={interval}&nobuf={nobuf}"
    req = urllib.request.Request(url, headers={**auth_headers(token), "Accept": "text/event-stream"})
    t0 = time.time()
    frames = 0
    last_arrival = None
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            rec("sse", event="open", status=r.status, nobuf=nobuf,
                content_type=r.headers.get("Content-Type"),
                xab=r.headers.get("X-Accel-Buffering"),
                ttfb_s=round(time.time() - t0, 3))
            for raw_line in r:
                line = raw_line.decode(errors="replace").rstrip("\n")
                if not line.startswith("data:"):
                    continue
                arrival = time.time()
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                srv = d.get("sent_at")
                frames += 1
                rec(
                    "sse_frame",
                    seq=d.get("seq"),
                    n=frames,
                    lag_ms=round((arrival - srv) * 1000, 1) if srv else None,
                    since_prev_s=round(arrival - last_arrival, 3) if last_arrival else None,
                    elapsed_s=round(arrival - t0, 1),
                )
                last_arrival = arrival
        rec("sse", event="closed", frames=frames, total_s=round(time.time() - t0, 1))
    except urllib.error.HTTPError as e:
        rec("sse", ok=False, status=e.code, location=e.headers.get("Location"),
            body=e.read().decode()[:300])
    except Exception as e:
        rec("sse", ok=False, error=type(e).__name__, detail=str(e)[:300],
            frames=frames, survived_s=round(time.time() - t0, 1))


# -------------------------------------------------------------------- egress lane

def lane_egress(base, token, target):
    host, _, port = target.partition(":")
    url = base.rstrip("/") + f"/egress?host={host}&port={port or 22}"
    req = urllib.request.Request(url, headers=auth_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            rec("egress", target=target, result=json.loads(r.read().decode()))
    except Exception as e:
        rec("egress", target=target, ok=False, error=type(e).__name__, detail=str(e)[:300])


def main():
    global OUT
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--token", default=os.getenv("DATABRICKS_TOKEN", ""))
    p.add_argument("--out", default="probe-results.jsonl")
    p.add_argument("--sse-minutes", type=float, default=11)
    p.add_argument("--sse-interval", type=float, default=2)
    p.add_argument("--sse-nobuf", type=int, default=1)
    p.add_argument("--ws", action="store_true")
    p.add_argument("--sse", action="store_true")
    p.add_argument("--post", action="store_true")
    p.add_argument("--egress")
    p.add_argument("--ws-hold-minutes", type=float, default=0,
                   help="hold one WS open this long, to test token-expiry teardown")
    p.add_argument("--ws-hold-interval", type=float, default=20)
    a = p.parse_args()

    if not a.token:
        sys.exit("need --token or DATABRICKS_TOKEN (workspace OAuth token; PATs are rejected)")

    lanes = {"ws": a.ws, "sse": a.sse, "post": a.post}
    if not any(lanes.values()) and not a.egress and not a.ws_hold_minutes:
        lanes = {"ws": True, "sse": True, "post": True}

    OUT = open(a.out, "a")
    rec("run", base=a.base, lanes=[k for k, v in lanes.items() if v],
        egress=a.egress, sse_minutes=a.sse_minutes,
        host=socket.gethostname(), python=sys.version.split()[0])

    if lanes.get("post"):
        lane_post(a.base, a.token)
    if lanes.get("ws"):
        lane_ws_raw(a.base, a.token)
        lane_ws_client(a.base, a.token)
    if a.egress:
        lane_egress(a.base, a.token, a.egress)
    if a.ws_hold_minutes:
        lane_ws_hold(a.base, a.token, a.ws_hold_minutes, a.ws_hold_interval)
    if lanes.get("sse"):
        lane_sse(a.base, a.token, a.sse_minutes, a.sse_interval, a.sse_nobuf)

    rec("run", event="done")


if __name__ == "__main__":
    main()
