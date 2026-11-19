"""
Public deployment of Trio Hub (e.g. on a Hugging Face Space), protected by a secret key.

The key is never stored here: only its SHA-256 fingerprint, read from the
TRIO_TOKEN_SHA256 env var or the token.sha256 file. Clients send the key in
any one of these ways:
  - URL path:      https://HOST/k/<KEY>/mcp     (works in every client, incl. claude.ai connectors)
  - query string:  https://HOST/mcp?key=<KEY>
  - header:        Authorization: Bearer <KEY>
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
if "TRIO_DB" not in os.environ:
    if os.environ.get("VERCEL"):
        os.environ["TRIO_DB"] = "/tmp/hub.db"  # only writable dir on Vercel; set DATABASE_URL for real storage
    elif os.access("/data", os.W_OK):
        os.environ["TRIO_DB"] = "/data/hub.db"
    else:
        os.environ["TRIO_DB"] = str(HERE / "hub.db")

from server import PG, mcp  # noqa: E402  (TRIO_DB must be set first)

_hash_file = HERE / "token.sha256"
TOKEN_SHA256 = (os.environ.get("TRIO_TOKEN_SHA256") or (_hash_file.read_text() if _hash_file.exists() else "")).strip().lower()
if not TOKEN_SHA256:
    raise SystemExit("Set TRIO_TOKEN_SHA256 (or token.sha256) to the sha256 hex digest of your secret key.")



async def inner(scope, receive, send):
    """Serve one MCP request with a fresh stateless handler.

    Serverless hosts (Vercel) may not run ASGI lifespan events, and the MCP
    session manager can only be started once, so build and start one per request.
    """
    sub = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True, json_response=True)
    async with sub.router.lifespan_context(sub):
        await sub(scope, receive, send)


def _ok(key: str | None) -> bool:
    if not key:
        return False
    return hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), TOKEN_SHA256)


async def _send_text(send, status: int, text: str):
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":  # nothing to start up; MCP handlers are per request
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return

    path = scope.get("path", "/")
    if path in ("/", "/health"):
        store = "postgres" if PG else ("sqlite (TEMPORARY on serverless!)" if os.environ.get("VERCEL") else "sqlite")
        return await _send_text(send, 200, f"Trio Hub is running. Storage: {store}. MCP endpoint: /k/<KEY>/mcp\n")

    key = None
    if path.startswith("/k/"):
        parts = path.split("/", 3)  # ['', 'k', KEY, rest]
        key = parts[2] if len(parts) > 2 else None
        path = "/" + (parts[3] if len(parts) > 3 else "")
        scope = dict(scope, path=path, raw_path=path.encode())
    if key is None:
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if key is None:
        key = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [None])[0]

    if not _ok(key):
        return await _send_text(send, 401, "Missing or wrong key.\n")
    if path.rstrip("/") == "/mcp":
        scope = dict(scope, path="/mcp", raw_path=b"/mcp")
    return await inner(scope, receive, send)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), proxy_headers=True, forwarded_allow_ips="*", access_log=False)
