"""
mediacast — phone-to-projector URL relay.

Phone (HTTP Shortcuts in Android Firefox's share menu) POSTs
    {"url": "https://..."}
to POST /cast with `Authorization: Bearer <MEDIACAST_TOKEN>`.

This service:
  - checks the token (constant-time compare)
  - sanity-checks the URL (http/https only, parses cleanly)
  - forwards to the host helper at MEDIACAST_HOST_URL, which owns the
    X11/Firefox side and runs outside the container.

The host helper sits on 127.0.0.1 (only the container can reach it via
host.docker.internal), so it doesn't need its own auth.
"""

import hmac
import logging
import os
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("mediacast")

TOKEN = os.environ.get("MEDIACAST_TOKEN", "")
HOST_URL = os.environ.get("MEDIACAST_HOST_URL", "http://host.docker.internal:8766/open")
HOST_TIMEOUT = float(os.environ.get("MEDIACAST_HOST_TIMEOUT", "5.0"))

if not TOKEN:
    raise RuntimeError("MEDIACAST_TOKEN is unset — refusing to start with an open endpoint")

app = FastAPI(title="mediacast", docs_url=None, redoc_url=None)


class CastRequest(BaseModel):
    url: str


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    # Constant-time compare so a remote attacker can't time-attack the secret.
    if not hmac.compare_digest(presented, TOKEN):
        raise HTTPException(status_code=401, detail="bad token")


def _check_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(status_code=400, detail="unparseable url")
    if parsed.scheme not in ("http", "https"):
        # Block file://, javascript:, data:, etc. — those are remote-code
        # execution against the auto-logged-in projector browser.
        raise HTTPException(status_code=400, detail="only http(s) urls allowed")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="url missing host")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/cast")
async def cast(req: CastRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    _check_auth(authorization)
    _check_url(req.url)

    logger.info("forwarding cast for url host=%s", urlparse(req.url).netloc)
    try:
        async with httpx.AsyncClient(timeout=HOST_TIMEOUT) as client:
            # Forward with the same bearer token — host helper binds
            # 0.0.0.0 and re-validates the token as its trust boundary.
            r = await client.post(
                HOST_URL,
                json={"url": req.url},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("host helper unreachable: %s", exc)
        raise HTTPException(status_code=502, detail=f"host helper unreachable: {exc}")

    if r.status_code >= 400:
        logger.warning("host helper returned %s: %s", r.status_code, r.text)
        raise HTTPException(status_code=502, detail=f"host helper error: {r.text}")

    return {"status": "ok"}
