"""Phase 1 transport probe — see IceRhymers/shellbox#1.

Throwaway app. Answers three questions about the Databricks Apps edge:
  1. Does an inbound WebSocket upgrade reach user code (101), or get 302'd/400'd?
  2. Does an SSE stream survive, and do frames arrive incrementally or buffered?
  3. Can the App container open outbound TCP to a sandbox gateway?

Every response carries server-side timestamps so the caller can compute per-frame
latency and detect buffering (staggered sent_at + simultaneous arrival == buffered).
"""

import asyncio
import json
import os
import socket
import time

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

BOOT = time.time()


@app.get("/")
def root():
    """Liveness + what the edge injected, so we can see the auth shape."""
    return {
        "probe": "shellbox-phase1",
        "boot_epoch": BOOT,
        "uptime_s": round(time.time() - BOOT, 1),
        "port_env": os.getenv("DATABRICKS_APP_PORT"),
    }


@app.get("/whoami")
def whoami(request: Request):
    """Echo the headers the Apps edge adds. Confirms which token reached us."""
    interesting = {
        k: (v[:12] + "...(%d chars)" % len(v) if "token" in k.lower() else v)
        for k, v in request.headers.items()
    }
    return {"headers": interesting}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Echo. The question is whether the handshake ever gets here at all."""
    await websocket.accept()
    seq = 0
    try:
        await websocket.send_text(
            json.dumps({"seq": seq, "sent_at": time.time(), "note": "server-hello"})
        )
        while True:
            msg = await websocket.receive_text()
            seq += 1
            await websocket.send_text(
                json.dumps({"seq": seq, "sent_at": time.time(), "echo": msg})
            )
    except WebSocketDisconnect:
        pass


@app.get("/sse")
async def sse(
    minutes: float = Query(10, description="how long to tick"),
    interval: float = Query(2, description="seconds between frames"),
    nobuf: int = Query(1, description="1 = send X-Accel-Buffering: no"),
):
    """Tick until `minutes` elapses. Frames are timestamped at send time.

    nobuf is a query param on purpose: we need to know whether SSE works only
    with the anti-buffering hint, or survives without it too.
    """
    deadline = time.time() + minutes * 60

    async def gen():
        seq = 0
        # Padding on the first frame defeats any fixed-size buffer at the edge
        # that would otherwise hold the stream until it fills.
        yield "event: open\ndata: %s\n\n" % json.dumps(
            {"sent_at": time.time(), "pad": "x" * 2048}
        )
        while time.time() < deadline:
            yield "data: %s\n\n" % json.dumps(
                {"seq": seq, "sent_at": time.time(), "elapsed_s": round(time.time() - BOOT, 1)}
            )
            seq += 1
            await asyncio.sleep(interval)
        yield "event: done\ndata: %s\n\n" % json.dumps({"seq": seq, "sent_at": time.time()})

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    if nobuf:
        headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.post("/in")
async def post_in(request: Request):
    """Plain POST baseline — the control for the other two."""
    body = await request.body()
    return {"received_at": time.time(), "bytes": len(body), "body": body.decode()[:500]}


def _lakebox_list(token, host):
    """GET /api/2.0/lakebox/sandboxes with a given bearer token."""
    import urllib.error
    import urllib.request

    url = host.rstrip("/") + "/api/2.0/lakebox/sandboxes"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
        boxes = body.get("sandboxes", []) or []
        return {
            "status": 200,
            "count": len(boxes),
            "sandbox_ids": sorted(b.get("sandboxId") for b in boxes),
            # The whole point of O2: does either principal see an owner field?
            "key_union": sorted({k for b in boxes for k in b.keys()}),
            "top_level_keys": sorted(body.keys()),
            "raw": body,
        }
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode()[:300]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:300]}


@app.get("/o2")
def o2(request: Request):
    """Rider O2: call the lakebox list endpoint as TWO different principals.

    Principal 1: the calling user, via the OBO token the Apps edge injects.
    Principal 2: this app's own service principal, via client-credentials M2M
                 using the DATABRICKS_CLIENT_ID/SECRET the Apps runtime provides.

    No identity needs to be created -- the app IS a second principal. Diffing the
    two result sets answers whether the endpoint is caller-scoped.
    """
    import base64
    import urllib.parse
    import urllib.request

    host = os.getenv("DATABRICKS_HOST", "")
    if host and not host.startswith("http"):
        host = "https://" + host
    cid = os.getenv("DATABRICKS_CLIENT_ID", "")
    csec = os.getenv("DATABRICKS_CLIENT_SECRET", "")

    out = {
        "host": host,
        "app_client_id": cid,  # not a secret; the secret is never returned
        "has_client_secret": bool(csec),
        "env_names": sorted(
            k for k in os.environ
            if any(s in k.upper() for s in ("DATABRICKS", "CLIENT", "TOKEN"))
        ),
    }

    # --- principal 1: the calling user (on-behalf-of token from the edge) ---
    obo = request.headers.get("x-forwarded-access-token")
    out["user"] = (
        {"identity": request.headers.get("x-forwarded-email"),
         **_lakebox_list(obo, host)}
        if obo else {"error": "no x-forwarded-access-token header"}
    )

    # --- principal 2: this app's service principal (M2M client credentials) ---
    if cid and csec and host:
        try:
            basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
            tok_req = urllib.request.Request(
                host.rstrip("/") + "/oidc/v1/token",
                data=urllib.parse.urlencode(
                    {"grant_type": "client_credentials", "scope": "all-apis"}
                ).encode(),
                headers={
                    "Authorization": "Basic " + basic,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(tok_req, timeout=30) as r:
                sp_token = json.loads(r.read().decode())["access_token"]
            out["app_sp"] = {"identity": "sp:" + cid, **_lakebox_list(sp_token, host)}
        except Exception as e:
            out["app_sp"] = {"error": type(e).__name__, "detail": str(e)[:300]}
    else:
        out["app_sp"] = {"error": "missing DATABRICKS_CLIENT_ID/SECRET/HOST in app env"}

    # --- the diff that actually answers O2 ---
    u, s = out["user"], out["app_sp"]
    if "sandbox_ids" in u and "sandbox_ids" in s:
        us, ss = set(u["sandbox_ids"]), set(s["sandbox_ids"])
        out["diff"] = {
            "user_only": sorted(us - ss),
            "sp_only": sorted(ss - us),
            "shared": sorted(us & ss),
            "identical_result_sets": us == ss,
            "key_sets_identical": u["key_union"] == s["key_union"],
            "owner_field_present": any(
                "owner" in k.lower() or "creator" in k.lower() or "principal" in k.lower()
                for k in set(u["key_union"]) | set(s["key_union"])
            ),
            "verdict": (
                "CALLER-SCOPED: disjoint result sets per principal"
                if us and ss and not (us & ss)
                else "NOT caller-scoped: both principals see the same sandboxes"
                if us and us == ss
                else "INCONCLUSIVE: see per-principal results"
            ),
        }
    return out


def _sp_token():
    """Mint an M2M token for this app's own service principal."""
    import base64
    import urllib.parse
    import urllib.request

    host = os.getenv("DATABRICKS_HOST", "")
    if host and not host.startswith("http"):
        host = "https://" + host
    cid, csec = os.getenv("DATABRICKS_CLIENT_ID"), os.getenv("DATABRICKS_CLIENT_SECRET")
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    req = urllib.request.Request(
        host.rstrip("/") + "/oidc/v1/token",
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "all-apis"}
        ).encode(),
        headers={"Authorization": "Basic " + basic,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"], host


def _api(method, path, token, host, body=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        host.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode()
            return {"status": r.status, "body": json.loads(txt) if txt.strip() else {}}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.read().decode()[:300]}
    except Exception as e:
        return {"error": type(e).__name__, "detail": str(e)[:300]}


@app.post("/o2-decisive")
def o2_decisive():
    """Settle O2: does the SP see a sandbox IT owns, and does that leak to others?

    Sequence, all as the app's own service principal:
      1. list  -> expect empty (established)
      2. create a sandbox
      3. list  -> if the new one appears, the SP HAS lakebox entitlement, which
                  means step 1's empty result was caller-scoping, not a 403-in-disguise
      4. delete it -> no orphan left behind

    The caller then re-lists as themselves to confirm the SP's sandbox is invisible.
    """
    steps = {}
    try:
        tok, host = _sp_token()
    except Exception as e:
        return {"error": "sp token failed", "detail": str(e)[:200]}

    steps["1_list_before"] = _api("GET", "/api/2.0/lakebox/sandboxes", tok, host)
    steps["2_create"] = _api(
        "POST", "/api/2.0/lakebox/sandboxes", tok, host,
        {"sandbox": {"name": "o2-sp-probe"}},
    )
    steps["3_list_after"] = _api("GET", "/api/2.0/lakebox/sandboxes", tok, host)

    # Clean up whatever we just made, by whatever id the create returned.
    sid = (steps["2_create"].get("body") or {}).get("sandboxId")
    steps["4_delete"] = (
        _api("DELETE", f"/api/2.0/lakebox/sandboxes/{sid}", tok, host)
        if sid else {"skipped": "no sandboxId returned by create"}
    )
    steps["5_list_final"] = _api("GET", "/api/2.0/lakebox/sandboxes", tok, host)

    before = (steps["1_list_before"].get("body") or {}).get("sandboxes", []) or []
    after = (steps["3_list_after"].get("body") or {}).get("sandboxes", []) or []
    steps["verdict"] = {
        "sp_sandbox_id": sid,
        "count_before": len(before),
        "count_after": len(after),
        "sp_has_lakebox_entitlement": steps["2_create"].get("status") in (200, 201),
        "key_union_after": sorted({k for b in after for k in b.keys()}),
        "conclusion": (
            "CALLER-SCOPED: SP can use lakebox and sees only its own sandbox, "
            "never the caller's"
            if len(after) == len(before) + 1 and len(before) == 0
            else "see raw steps"
        ),
    }
    return steps


@app.get("/egress")
def egress(host: str, port: int = 22, timeout: float = 5.0):
    """Attempt outbound TCP from inside the App container.

    Used to test App -> sandbox gateway reachability, which decides whether an
    App->SSH transport is even possible.
    """
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            peer = s.getpeername()
            # Grab any greeting banner (SSH sends one unprompted).
            s.settimeout(2.0)
            try:
                banner = s.recv(256).decode(errors="replace")
            except (socket.timeout, OSError):
                banner = ""
        return {
            "ok": True,
            "peer": peer,
            "banner": banner.strip(),
            "elapsed_s": round(time.time() - t0, 3),
        }
    except Exception as e:
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "error": type(e).__name__,
                "detail": str(e),
                "elapsed_s": round(time.time() - t0, 3),
            },
        )
