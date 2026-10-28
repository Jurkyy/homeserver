"""
mediacast — phone-to-projector relay + media portal API.

Surfaces (all funnel into the host helper at MEDIACAST_HOST_URL, which
owns the X11 / Firefox / mpv side and runs outside the container):

  - /api/v1/*        the JSON API (contract: docs/api-v1.md). Bearer
                     MEDIACAST_TOKEN on everything except health and
                     images. Used by the Android app and the web portal.
  - GET /            the web portal (static/), token injected into the
                     page; it sends it as an Authorization header, which
                     forces a CORS preflight we never answer → no CSRF.
  - POST /cast, /control, GET /health
                     legacy token routes kept for Home Assistant /
                     OpenClaw / HTTP Shortcuts.

The old token-less /ui-* routes are gone: they let any web page on the
internet a LAN browser visited fire casts, screen-off and power-off.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import random
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

VERSION = "2.0.0"

logger = logging.getLogger("mediacast")
# uvicorn's dictConfig only wires up its own "uvicorn.*" loggers, so
# without this our logger.info() calls would be silently dropped.
logging.basicConfig(
    level=os.environ.get("MEDIACAST_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# httpx logs every request at INFO (the host long-poll, image proxying,
# Jellyfin progress reports) — pure noise.
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.environ.get("MEDIACAST_TOKEN", "")
HOST_URL = os.environ.get("MEDIACAST_HOST_URL", "http://host.docker.internal:8766/open")
HOST_BASE = HOST_URL.rsplit("/", 1)[0]
# librespot's control socket (homeserver patch, services/librespot/control.rs):
# "host:port" reachable from this container; blank disables Spotify control.
SPOTIFY_CONTROL_ADDR = os.environ.get("LIBRESPOT_CONTROL_ADDR", "").strip()
SPOTIFY_CONTROL_TIMEOUT = float(os.environ.get("LIBRESPOT_CONTROL_TIMEOUT", "6.0"))
HOST_TIMEOUT = float(os.environ.get("MEDIACAST_HOST_TIMEOUT", "5.0"))
# Casting a YouTube URL can make the host helper resolve the stream with
# yt-dlp before it answers; give casts their own roomy budget.
CAST_TIMEOUT = float(os.environ.get("MEDIACAST_CAST_TIMEOUT", "60.0"))
# Base URLs offered in the portal's "Pair phone" QR (comma-separated).
PAIR_URLS = [u.strip().rstrip("/") for u in
             os.environ.get("MEDIACAST_PAIR_URLS", "http://homeserver.local").split(",") if u.strip()]
STATIC_DIR = Path(__file__).with_name("static")

# YouTube subscriptions feed. Optional: if neither the account scrape nor
# the channel-list file yields channels, the endpoint 404s. We poll each
# channel's PUBLIC RSS feed (no account, no API key, no quota), merge by
# publish date, and serve the newest uploads.
YT_CHANNELS_FILE = os.environ.get("YT_CHANNELS_FILE", "/config/yt-channels.txt")
# Auto-populate the channel list from the logged-in YouTube account (the
# host helper scrapes it with yt-dlp + the projector Firefox's cookies).
YT_SUBS_FROM_ACCOUNT = os.environ.get("YT_SUBS_FROM_ACCOUNT", "1") not in ("0", "false", "")
YT_SUBS_TTL = float(os.environ.get("YT_SUBS_TTL", "21600"))    # re-scrape every 6h
YT_SUBS_FETCH_TIMEOUT = float(os.environ.get("YT_SUBS_FETCH_TIMEOUT", "100"))
YT_FEED_LIMIT = int(os.environ.get("YT_FEED_LIMIT", "150"))    # cards cached
YT_PER_CHANNEL = int(os.environ.get("YT_PER_CHANNEL", "15"))   # recent per channel (RSS max)
YT_FETCH_CONCURRENCY = int(os.environ.get("YT_FETCH_CONCURRENCY", "12"))
# Background refresher: rebuild the feed on this cadence whether or not a
# client is looking, and push `event: feed` over SSE when it changed.
YT_FEED_REFRESH_MIN = float(os.environ.get("YT_FEED_REFRESH_MIN", "5") or 5)
# Channel RSS answers `Cache-Control: max-age=900` (and no ETag/Last-
# Modified, so no conditional GET). We honour max-age (jittered, capped
# here), so each cycle only re-fetches the channels whose copy expired.
YT_RSS_MAX_AGE_CAP = float(os.environ.get("YT_RSS_MAX_AGE_CAP", "900"))
_YT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
_UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")
_VID_RE = re.compile(r"[0-9A-Za-z_-]{11}")
# Last-known-good subscriptions + feed. Lives on the writable /data bind
# mount (services/mediacast/data) so it survives rebuilds, not just
# restarts — the page never opens onto the placeholder channel list while
# waiting on a fresh ~90s account scrape.
YT_STATE_FILE = os.environ.get("YT_STATE_FILE", "/data/yt-state.json")

# Jellyfin ("mrpflix"). Optional: if MRPFLIX_URL is unset the endpoints
# 404. We log in with the user's own (non-admin) credentials; the token
# never leaves the container (images are proxied).
JELLYFIN_URL = os.environ.get("MRPFLIX_URL", "").rstrip("/")
JELLYFIN_USER = os.environ.get("MRPFLIX_USER", "")
JELLYFIN_PASS = os.environ.get("MRPFLIX_PASS", "")
JELLYFIN_ENABLED = bool(JELLYFIN_URL and JELLYFIN_USER and JELLYFIN_PASS)
JELLYFIN_TIMEOUT = float(os.environ.get("MRPFLIX_TIMEOUT", "20.0"))
_JF_DEVICE_ID = "mediacast-" + (os.environ.get("HOSTNAME", "homeserver"))
_JF_AUTH_HEADER = (
    f'MediaBrowser Client="mediacast", Device="homeserver", '
    f'DeviceId="{_JF_DEVICE_ID}", Version="{VERSION}"'
)
# MaxStreamingBitrate is the one device-profile field the "low quality"
# toggle varies: the remote mrpflix link is occasionally starved (seen
# 2026-07-19: ~1Mbps actual vs ~7Mbps wanted) with nothing to fix on our end.
_JF_MAX_BITRATE = int(os.environ.get("MRPFLIX_MAX_BITRATE", "20000000"))
_JF_LOW_BITRATE = int(os.environ.get("MRPFLIX_LOW_BITRATE", "1200000"))
JF_PROGRESS_INTERVAL = 10.0
_TICKS = 10_000_000


def _jf_device_profile(max_bitrate: int) -> dict:
    # We direct play H.264 (the only codec the projector's GTX 970 decodes
    # in hardware); anything else Jellyfin transcodes to H.264 HLS so mpv
    # still rides NVDEC.
    return {
        "MaxStreamingBitrate": max_bitrate,
        "DirectPlayProfiles": [
            {
                "Container": "mp4,m4v,mkv,mov,webm,ts",
                "Type": "Video",
                "VideoCodec": "h264",
                "AudioCodec": "aac,mp3,ac3,eac3,opus,flac,vorbis",
            }
        ],
        "TranscodingProfiles": [
            {
                "Container": "ts",
                "Type": "Video",
                "VideoCodec": "h264",
                "AudioCodec": "aac,mp3,ac3",
                "Protocol": "hls",
                "Context": "Streaming",
                "MaxAudioChannels": "2",
            }
        ],
    }


if not TOKEN:
    raise RuntimeError("MEDIACAST_TOKEN is unset — refusing to start with an open endpoint")


# ---------------------------------------------------------------------------
# App, shared HTTP client, errors, auth
# ---------------------------------------------------------------------------

_http: httpx.AsyncClient | None = None
_bg_tasks: set = set()


def _spawn(coro) -> None:
    # asyncio only keeps weak refs to tasks; hold strong ones.
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def http() -> httpx.AsyncClient:
    assert _http is not None
    return _http


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global _http
    _http = httpx.AsyncClient(
        timeout=JELLYFIN_TIMEOUT, follow_redirects=True,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    _load_yt_state()
    watcher = asyncio.create_task(_host_watch())
    yt_refresher = asyncio.create_task(_yt_refresh_loop())
    try:
        yield
    finally:
        watcher.cancel()
        yt_refresher.cancel()
        await _http.aclose()


app = FastAPI(title="mediacast", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)

_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
          405: "method_not_allowed", 409: "conflict", 422: "validation_error",
          502: "upstream_error", 503: "unavailable", 504: "upstream_timeout"}


class ApiError(HTTPException):
    def __init__(self, status: int, error: str, code: str | None = None):
        super().__init__(status_code=status, detail=error)
        self.code = code or _CODES.get(status, "error")


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    if not _is_api(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    code = getattr(exc, "code", None) or _CODES.get(exc.status_code, "error")
    return JSONResponse({"error": str(exc.detail), "code": code}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def _validation_exc(request: Request, exc: RequestValidationError):
    if not _is_api(request):
        return JSONResponse({"detail": exc.errors()}, status_code=422)
    errs = exc.errors()
    first = errs[0] if errs else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    msg = f"{loc}: {first.get('msg')}" if loc else str(first.get("msg", "invalid request"))
    return JSONResponse({"error": msg, "code": "validation_error"}, status_code=422)


def _check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise ApiError(401, "missing bearer token", "unauthorized")
    presented = authorization.removeprefix("Bearer ").strip()
    # Constant-time compare so a remote attacker can't time-attack the secret.
    if not hmac.compare_digest(presented, TOKEN):
        raise ApiError(401, "bad token", "unauthorized")


async def require_token(authorization: str | None = Header(default=None)) -> None:
    _check_auth(authorization)


def _check_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise ApiError(400, "unparseable url", "bad_url")
    if parsed.scheme not in ("http", "https"):
        # Block file://, javascript:, data:, etc. — those are remote-code
        # execution against the auto-logged-in projector browser.
        raise ApiError(400, "only http(s) urls allowed", "bad_url")
    if not parsed.netloc:
        raise ApiError(400, "url missing host", "bad_url")


# ---------------------------------------------------------------------------
# Host helper forwarding
# ---------------------------------------------------------------------------

_HOST_AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def _host_post(path: str, payload: dict, timeout: float = HOST_TIMEOUT) -> dict:
    try:
        r = await http().post(f"{HOST_BASE}{path}", json=payload, headers=_HOST_AUTH,
                              timeout=timeout)
    except httpx.HTTPError as exc:
        logger.warning("host helper unreachable (%s): %s", path, exc)
        raise ApiError(502, f"host helper unreachable: {exc}", "host_unreachable")
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    if r.status_code == 404 and path == "/control":
        raise ApiError(409, "nothing is playing", "no_active_cast")
    if r.status_code >= 400:
        detail = body.get("error") or body.get("detail") or "request failed"
        if body.get("stderr"):
            detail = f"{detail}: {str(body['stderr'])[:200]}"
        logger.warning("host helper %s -> %s: %s", path, r.status_code, detail)
        status = r.status_code if r.status_code in (400, 404) else 502
        raise ApiError(status, str(detail), "host_error")
    return body


async def _host_open(url: str, *, backend: str | None = None, title: str | None = None,
                     meta: dict | None = None, start: float | None = None) -> None:
    payload: dict = {"url": url}
    if backend:
        # Force a player for an already-resolved media URL (Jellyfin → mpv).
        payload["backend"] = backend
    if title:
        payload["title"] = title
    if meta:
        payload["meta"] = meta
    if start:
        payload["start"] = start
    await _host_post("/open", payload, timeout=CAST_TIMEOUT)


# ---------------------------------------------------------------------------
# State: host long-poll → merged state → SSE fan-out
# ---------------------------------------------------------------------------

_host_state: dict = {"version": -1, "state": None}
_spotify: dict | None = None
_subscribers: set[asyncio.Queue] = set()

_OFFLINE_STATE = {
    "power": "on", "screen": "on", "activity": "idle", "source": None, "title": None,
    "subtitle": None, "artwork_url": None, "position_s": None, "duration_s": None,
    "volume": 0, "muted": False, "item": None,
    "screensaver": {"active": False, "theme": None, "themes": [], "idle_minutes": 0},
    "night": None,
    "updated_at": 0,
}


def _merged_state() -> dict:
    st = dict(_host_state["state"] or _OFFLINE_STATE)
    sp = _spotify
    st["spotify"] = dict(sp, controllable=bool(SPOTIFY_CONTROL_ADDR)) if sp else None
    st["updated_at"] = max(int(st.get("updated_at") or 0), int(sp["updated_at"]) if sp else 0)
    return st


def _publish(event: str, data: str) -> None:
    for q in list(_subscribers):
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()   # slow client: drop the stale frame
        q.put_nowait((event, data))


def _broadcast() -> None:
    _publish("state", json.dumps(_merged_state(), separators=(",", ":")))


def _feed_event(new_count: int = 0, removed_count: int = 0) -> str:
    return json.dumps({"updated_at": _feed_cache["changed_at"], "new_count": new_count,
                       "removed_count": removed_count}, separators=(",", ":"))


async def _fetch_host_state(since: int, wait: float) -> dict:
    r = await http().get(f"{HOST_BASE}/v1/state", params={"since": since, "wait": wait},
                         headers=_HOST_AUTH, timeout=wait + 10)
    r.raise_for_status()
    return r.json()


async def _host_watch() -> None:
    """Long-poll the host helper forever; every new version is merged and
    pushed to SSE subscribers (and drives Jellyfin progress reporting)."""
    backoff = 1.0
    while True:
        try:
            data = await _fetch_host_state(_host_state["version"], 25)
            backoff = 1.0
            if data.get("version") != _host_state["version"]:
                _host_state.update(version=data["version"], state=data["state"])
                _broadcast()
                await _jf_on_state(data["state"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep watching no matter what
            logger.warning("host state watch: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)


api = APIRouter(prefix="/api/v1")
authed = [Depends(require_token)]


@api.get("/health")
async def api_health() -> dict:
    return {"ok": True, "version": VERSION}


@api.get("/state", dependencies=authed)
async def api_state() -> dict:
    if _host_state["state"] is None:
        try:
            data = await _fetch_host_state(-1, 0)
            _host_state.update(version=data["version"], state=data["state"])
        except Exception as exc:  # noqa: BLE001
            raise ApiError(502, f"host helper unreachable: {exc}", "host_unreachable")
    return _merged_state()


@api.get("/events", dependencies=authed)
async def api_events(request: Request) -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    _subscribers.add(q)

    async def gen():
        try:
            yield "retry: 3000\n"
            yield f"event: state\ndata: {json.dumps(_merged_state(), separators=(',', ':'))}\n\n"
            if _feed_cache["changed_at"]:
                # Lets a (re)connecting client tell whether its feed is stale.
                yield f"event: feed\ndata: {_feed_event()}\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"event: {event}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Playback / screensaver / power
# ---------------------------------------------------------------------------

class CastRequest(BaseModel):
    url: str


class V1Control(BaseModel):
    action: str
    value: float | None = None


class V1Screensaver(BaseModel):
    theme: str | None = None


class V1Power(BaseModel):
    action: str


def _yt_vid(url: str) -> str:
    try:
        u = urlparse(url)
    except ValueError:
        return ""
    host = u.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif host.endswith("youtube.com"):
        q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        parts = u.path.split("/")
        vid = q.get("v") or (parts[2] if len(parts) > 2 and parts[1] in ("shorts", "live", "embed") else "")
    else:
        return ""
    return vid if _VID_RE.fullmatch(vid or "") else ""


def _cast_meta(url: str) -> dict | None:
    """Pre-fill title/channel for a YouTube cast from the feed cache so the
    now-playing card is right before the page has even loaded."""
    vid = _yt_vid(url)
    if not vid:
        return None
    meta: dict = {"source": "youtube", "item": {"kind": "youtube", "id": vid},
                  "artwork_url": f"/api/v1/artwork/youtube/{vid}"}
    hit = next((it for it in _feed_cache["items"] if it.get("id") == vid), None)
    if hit:
        meta.update(title=hit.get("title"), subtitle=hit.get("channel"))
    return meta


@api.post("/cast", dependencies=authed)
async def api_cast(req: CastRequest) -> dict:
    _check_url(req.url)
    logger.info("cast host=%s", urlparse(req.url).netloc)
    await _jf_stop_session()
    await _host_open(req.url, meta=_cast_meta(req.url))
    return {"ok": True}


_V1_VALUE_ACTIONS = {"seek", "seek_to", "volume", "volume_delta"}


@api.post("/control", dependencies=authed)
async def api_control(req: V1Control) -> dict:
    a, v = req.action, req.value
    if a in _V1_VALUE_ACTIONS and v is None:
        raise ApiError(400, f"action {a!r} needs a numeric value", "missing_value")
    if a == "toggle":
        body = {"action": "pause"}
    elif a in ("play", "pause"):
        body = {"action": "set_pause", "paused": a == "pause"}
    elif a == "stop":
        body = {"action": "stop"}
    elif a == "seek":
        body = {"action": "seek", "offset": int(v)}
    elif a == "seek_to":
        body = {"action": "seek_to", "position": max(0.0, float(v))}
    elif a == "volume":
        body = {"action": "volume", "set": max(0, min(100, int(round(v))))}
    elif a == "volume_delta":
        body = {"action": "volume", "delta": int(round(v))}
    else:
        raise ApiError(400, f"unknown action {a!r}", "bad_action")
    await _host_post("/control", body)
    return {"ok": True}


@api.post("/screensaver", dependencies=authed)
async def api_screensaver(req: V1Screensaver) -> dict:
    if req.theme:
        await _jf_stop_session()
    # Starting one goes through the same yt-dlp/Firefox cast path → roomy budget.
    await _host_post("/screensaver", {"theme": req.theme}, timeout=CAST_TIMEOUT)
    return {"ok": True}


@api.post("/power", dependencies=authed)
async def api_power(req: V1Power, request: Request) -> dict:
    if req.action not in ("sleep", "wake", "screen_off", "deep_sleep", "bedtime_cancel", "poweroff"):
        raise ApiError(400, f"unknown power action {req.action!r}", "bad_action")
    client = request.client.host if request.client else "?"
    if req.action in ("poweroff", "deep_sleep"):
        logger.warning("%s requested from=%s", req.action.upper(), client)
    else:
        logger.info("power %s from=%s", req.action, client)
    if req.action in ("sleep", "screen_off", "deep_sleep"):
        await _jf_stop_session()
    await _host_post("/power", {"action": req.action}, timeout=15)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

_art_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_ART_CACHE_MAX = 200


@api.get("/artwork/youtube/{vid}")
async def api_artwork_youtube(vid: str) -> Response:
    if not _VID_RE.fullmatch(vid):
        raise ApiError(404, "bad video id", "not_found")
    hit = _art_cache.get(vid)
    if hit is None:
        for name in ("maxresdefault", "hqdefault"):
            try:
                r = await http().get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg", timeout=10)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                hit = (r.content, r.headers.get("Content-Type", "image/jpeg"))
                break
        if hit is None:
            raise ApiError(404, "no thumbnail", "not_found")
        _art_cache[vid] = hit
        while len(_art_cache) > _ART_CACHE_MAX:
            _art_cache.popitem(last=False)
    _art_cache.move_to_end(vid)
    return Response(content=hit[0], media_type=hit[1],
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------------------
# Spotify (librespot --onevent hook → state.spotify)
# ---------------------------------------------------------------------------

class SpotifyEvent(BaseModel):
    event: str
    track_id: str | None = None
    name: str | None = None
    artists: str | None = None
    album: str | None = None
    covers: str | None = None
    duration_ms: int | None = None
    position_ms: int | None = None
    volume: int | None = None      # volume_changed: librespot 0-65535


@api.post("/spotify/event", dependencies=authed)
async def api_spotify_event(ev: SpotifyEvent) -> dict:
    """Internal: librespot's --onevent hook (services/librespot/onevent.sh)
    posts every player event here."""
    global _spotify
    sp = dict(_spotify) if _spotify else {
        "active": False, "playing": False, "title": None, "artist": None, "album": None,
        "artwork_url": None, "position_s": None, "duration_s": None, "updated_at": 0,
    }
    e = ev.event
    now_ms = int(time.time() * 1000)
    if e in ("session_connected", "session_client_changed"):
        sp["active"] = True
    elif e == "session_disconnected":
        sp.update(active=False, playing=False)
    elif e == "track_changed":
        covers = [c for c in (ev.covers or "").splitlines() if c.strip()]
        sp.update(
            active=True,
            title=ev.name or None,
            artist=", ".join(a for a in (ev.artists or "").splitlines() if a) or None,
            album=ev.album or None,
            artwork_url=covers[0].strip() if covers else None,
            duration_s=round(ev.duration_ms / 1000, 1) if ev.duration_ms else None,
            position_s=0.0,
        )
    elif e == "playing":
        sp.update(active=True, playing=True)
    elif e in ("paused", "stopped", "end_of_track", "unavailable"):
        sp["playing"] = False
    elif e == "volume_changed":
        if ev.volume is None:
            return {"ok": True}
        sp["volume"] = round(max(0, min(65535, ev.volume)) * 100 / 65535)
    elif e not in ("seeked", "position_correction", "loading", "preloading"):
        return {"ok": True}   # shuffle/repeat/…: not part of the state
    if ev.position_ms is not None:
        sp["position_s"] = round(ev.position_ms / 1000, 1)
    sp["updated_at"] = now_ms
    _spotify = sp
    _broadcast()
    # The host helper's night deep-sleep idle check needs to know whether
    # Spotify is playing (it also watches PulseAudio, this is the fallback).
    _bg(_host_spotify(sp))
    return {"ok": True}


class V1SpotifyControl(BaseModel):
    action: str
    value: float | None = None


_SPOTIFY_VALUE_ACTIONS = {"seek", "seek_to", "volume", "volume_delta"}


def _spotify_position_s(sp: dict) -> float:
    """Current position, extrapolated from the last librespot event."""
    pos = float(sp.get("position_s") or 0)
    if sp.get("playing") and sp.get("updated_at"):
        pos += max(0.0, time.time() - sp["updated_at"] / 1000)
    dur = sp.get("duration_s")
    return min(pos, float(dur)) if dur else pos


async def _spotify_send(line: str) -> None:
    """One request to librespot's control socket: `<token> <cmd> [arg]`."""
    host, _, port = SPOTIFY_CONTROL_ADDR.rpartition(":")
    try:
        async with asyncio.timeout(SPOTIFY_CONTROL_TIMEOUT):
            reader, writer = await asyncio.open_connection(host.strip("[]"), int(port))
            try:
                writer.write(f"{TOKEN} {line}\n".encode())
                await writer.drain()
                reply = (await reader.readline()).decode(errors="replace").strip()
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except (OSError, TimeoutError, ValueError) as exc:
        raise ApiError(502, f"librespot control unreachable: {exc}", "spotify_unreachable")
    if reply != "ok":
        raise ApiError(409, f"librespot: {reply.removeprefix('err ').strip() or 'no reply'}", "spotify_error")


@api.post("/spotify/control", dependencies=authed)
async def api_spotify_control(req: V1SpotifyControl) -> dict:
    """Play/pause/skip/seek/volume on the librespot Connect session."""
    if not SPOTIFY_CONTROL_ADDR or not TOKEN:
        raise ApiError(409, "Spotify control is not configured", "spotify_unavailable")
    a, v = req.action, req.value
    if a in _SPOTIFY_VALUE_ACTIONS and v is None:
        raise ApiError(400, f"action {a!r} needs a numeric value", "missing_value")
    sp = _spotify or {}
    if a == "toggle":
        a = "pause" if sp.get("playing") else "play"
    if a in ("play", "pause", "next"):
        line = a
    elif a in ("previous", "prev"):
        line = "prev"
    elif a in ("seek", "seek_to"):
        target = float(v) + (_spotify_position_s(sp) if a == "seek" else 0.0)
        dur = sp.get("duration_s")
        if dur:
            target = min(target, max(0.0, float(dur) - 1))
        line = f"seek {int(max(0.0, target) * 1000)}"
    elif a in ("volume", "volume_delta"):
        pct = float(v) + (float(sp.get("volume") or 0) if a == "volume_delta" else 0.0)
        line = f"volume {round(max(0.0, min(100.0, pct)) * 65535 / 100)}"
    else:
        raise ApiError(400, f"unknown action {req.action!r}", "bad_action")
    await _spotify_send(line)
    return {"ok": True}


_bg_tasks: set[asyncio.Task] = set()


def _bg(coro) -> None:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def _host_spotify(sp: dict) -> None:
    try:
        await http().post(f"{HOST_BASE}/spotify", headers=_HOST_AUTH, timeout=5, json={
            "playing": bool(sp.get("playing")), "updated_at": sp.get("updated_at"),
            "position_s": sp.get("position_s"), "duration_s": sp.get("duration_s"),
        })
    except httpx.HTTPError as exc:
        logger.debug("host /spotify push failed: %s", exc)


# ---------------------------------------------------------------------------
# Jellyfin ("mrpflix")
# ---------------------------------------------------------------------------

_jf_token: str | None = None
_jf_user_id: str | None = None
_jf_lock = asyncio.Lock()
# The one Jellyfin item we are playing (for Sessions/Playing reporting).
_jf_session: dict | None = None


def _require_jellyfin() -> None:
    if not JELLYFIN_ENABLED:
        raise ApiError(404, "jellyfin not configured", "not_configured")


async def _jf_authenticate(force: bool = False) -> tuple[str, str]:
    """Log in with the configured user creds; cache the token + user id."""
    global _jf_token, _jf_user_id
    async with _jf_lock:
        if force:
            _jf_token = _jf_user_id = None
        if _jf_token and _jf_user_id:
            return _jf_token, _jf_user_id
        try:
            r = await http().post(
                f"{JELLYFIN_URL}/Users/AuthenticateByName",
                headers={"Authorization": _JF_AUTH_HEADER},
                json={"Username": JELLYFIN_USER, "Pw": JELLYFIN_PASS},
            )
        except httpx.HTTPError as exc:
            raise ApiError(502, f"jellyfin unreachable: {exc}", "jellyfin_unreachable")
        if r.status_code >= 400:
            raise ApiError(502, f"jellyfin auth failed ({r.status_code})", "jellyfin_auth")
        data = r.json()
        _jf_token = data["AccessToken"]
        _jf_user_id = data["User"]["Id"]
        logger.info("jellyfin: authenticated as %s", JELLYFIN_USER)
        return _jf_token, _jf_user_id


async def _jf_request(method: str, path: str, **kw) -> httpx.Response:
    """Call the Jellyfin API with the cached token, re-authing once on 401.
    The token rides in the Authorization header (current Jellyfin rejects
    X-Emby-Token / api_key on the REST API)."""
    token, _ = await _jf_authenticate()
    extra = kw.pop("headers", {})
    for attempt in range(2):
        headers = {**extra, "Authorization": f'{_JF_AUTH_HEADER}, Token="{token}"'}
        try:
            r = await http().request(method, f"{JELLYFIN_URL}{path}", headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise ApiError(502, f"jellyfin unreachable: {exc}", "jellyfin_unreachable")
        if r.status_code != 401 or attempt:
            return r
        token, _ = await _jf_authenticate(force=True)
    return r


async def _jf_json(method: str, path: str, **kw) -> dict:
    r = await _jf_request(method, path, **kw)
    if r.status_code == 404:
        raise ApiError(404, "jellyfin item not found", "not_found")
    if r.status_code >= 400:
        raise ApiError(502, f"jellyfin error ({r.status_code}) on {path}", "jellyfin_error")
    return r.json() if r.content else {}


_JF_FIELDS = "Overview,ChildCount,RecursiveItemCount,ProductionYear,ParentId"


def _jf_img(item_id: str, kind: str = "Primary") -> str:
    return f"/api/v1/jellyfin/image/{item_id}?type={kind}"


def _jf_item(it: dict) -> dict:
    """Jellyfin item → JfItem (docs/api-v1.md)."""
    typ = it.get("Type", "")
    ud = it.get("UserData") or {}
    out: dict = {"id": it.get("Id"), "name": it.get("Name") or "?", "type": typ,
                 "played": bool(ud.get("Played"))}
    if typ in ("Episode", "Season") and it.get("SeriesName"):
        out["series_name"] = it["SeriesName"]
    if typ == "Episode":
        if it.get("ParentIndexNumber") is not None:
            out["season"] = it["ParentIndexNumber"]
        if it.get("IndexNumber") is not None:
            out["episode"] = it["IndexNumber"]
    elif typ == "Season" and it.get("IndexNumber") is not None:
        out["season"] = it["IndexNumber"]
    if it.get("ProductionYear"):
        out["year"] = it["ProductionYear"]
    if it.get("Overview"):
        out["overview"] = it["Overview"]
    if it.get("RunTimeTicks"):
        out["runtime_s"] = round(it["RunTimeTicks"] / _TICKS)
    if ud.get("PlayedPercentage"):
        out["played_pct"] = round(ud["PlayedPercentage"], 1)
    if ud.get("PlaybackPositionTicks"):
        out["resume_s"] = round(ud["PlaybackPositionTicks"] / _TICKS)
    cc = it.get("ChildCount")
    if cc is None and typ in ("Series", "Season", "BoxSet", "Folder", "CollectionFolder"):
        cc = it.get("RecursiveItemCount")
    if cc is not None:
        out["child_count"] = cc
    # Poster: own Primary, else the series' / parent's (episodes without stills).
    tags = it.get("ImageTags") or {}
    img_id = it.get("Id")
    if "Primary" not in tags:
        if it.get("SeriesPrimaryImageTag") and it.get("SeriesId"):
            img_id = it["SeriesId"]
        elif it.get("ParentPrimaryImageItemId"):
            img_id = it["ParentPrimaryImageItemId"]
    out["image_url"] = _jf_img(img_id)
    if it.get("BackdropImageTags"):
        out["backdrop_url"] = _jf_img(it["Id"], "Backdrop")
    elif it.get("ParentBackdropItemId"):
        out["backdrop_url"] = _jf_img(it["ParentBackdropItemId"], "Backdrop")
    return out


def _jf_common_params(uid: str) -> dict:
    return {"userId": uid, "Fields": _JF_FIELDS, "EnableUserData": "true",
            "EnableImageTypes": "Primary,Backdrop", "ImageTypeLimit": 1}


@api.get("/jellyfin/views", dependencies=authed)
async def api_jf_views() -> dict:
    _require_jellyfin()
    _, uid = await _jf_authenticate()
    data = await _jf_json("GET", "/UserViews", params={"userId": uid})
    return {"items": [_jf_item(it) for it in data.get("Items", [])]}


@api.get("/jellyfin/items", dependencies=authed)
async def api_jf_items(parent_id: str | None = None, search: str | None = None,
                       start: int = 0, limit: int = 60, types: str | None = None) -> dict:
    _require_jellyfin()
    _, uid = await _jf_authenticate()
    params = _jf_common_params(uid)
    params.update(StartIndex=max(0, start), Limit=max(1, min(500, limit)),
                  EnableTotalRecordCount="true")
    if parent_id:
        params["parentId"] = parent_id
    if search:
        params["searchTerm"] = search
    if types:
        params["IncludeItemTypes"] = types
    elif not parent_id:
        params["IncludeItemTypes"] = "Movie,Series"
    # Recursive whenever we're filtering by type/search or at the top
    # level; a bare parent_id lists that container's direct children.
    if search or types or not parent_id:
        params["Recursive"] = "true"
        params["SortBy"] = "SortName"
    else:
        params["SortBy"] = "IndexNumber,SortName"
    data = await _jf_json("GET", "/Items", params=params)
    items = [_jf_item(it) for it in data.get("Items", [])]
    return {"items": items, "total": data.get("TotalRecordCount", len(items))}


@api.get("/jellyfin/resume", dependencies=authed)
async def api_jf_resume() -> dict:
    _require_jellyfin()
    _, uid = await _jf_authenticate()
    params = {**_jf_common_params(uid), "MediaTypes": "Video", "Limit": 30}
    data = await _jf_json("GET", "/UserItems/Resume", params=params)
    return {"items": [_jf_item(it) for it in data.get("Items", [])]}


@api.get("/jellyfin/nextup", dependencies=authed)
async def api_jf_nextup() -> dict:
    _require_jellyfin()
    _, uid = await _jf_authenticate()
    params = {**_jf_common_params(uid), "Limit": 30}
    data = await _jf_json("GET", "/Shows/NextUp", params=params)
    return {"items": [_jf_item(it) for it in data.get("Items", [])]}


async def _jf_get_item(item_id: str) -> dict:
    _, uid = await _jf_authenticate()
    return await _jf_json("GET", f"/Items/{item_id}", params={"userId": uid})


@api.get("/jellyfin/item/{item_id}", dependencies=authed)
async def api_jf_item(item_id: str) -> dict:
    _require_jellyfin()
    _, uid = await _jf_authenticate()
    it = await _jf_get_item(item_id)
    out = _jf_item(it)
    typ = it.get("Type")
    params = _jf_common_params(uid)
    children = None
    if typ == "Series":
        data = await _jf_json("GET", f"/Shows/{item_id}/Seasons", params=params)
        children = data.get("Items", [])
    elif typ == "Season":
        series_id = it.get("SeriesId") or it.get("ParentId")
        data = await _jf_json("GET", f"/Shows/{series_id}/Episodes",
                              params={**params, "seasonId": item_id})
        children = data.get("Items", [])
    elif it.get("IsFolder"):
        data = await _jf_json("GET", "/Items", params={
            **params, "parentId": item_id, "SortBy": "IndexNumber,SortName", "Limit": 500})
        children = data.get("Items", [])
    if children is not None:
        out["children"] = [_jf_item(c) for c in children]
    return out


@api.get("/jellyfin/image/{item_id}")
async def api_jf_image(item_id: str, type: str = "Primary", max_width: int = 600) -> Response:
    # Proxied so the Jellyfin token stays server-side; no auth (it's an image).
    _require_jellyfin()
    kind = type if type in ("Primary", "Backdrop", "Thumb", "Logo") else "Primary"
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", item_id):
        raise ApiError(404, "bad item id", "not_found")
    r = await _jf_request("GET", f"/Items/{item_id}/Images/{kind}",
                          params={"maxWidth": max(64, min(3840, max_width)), "quality": 90})
    if r.status_code >= 400:
        raise ApiError(404, "no image", "not_found")
    return Response(content=r.content, media_type=r.headers.get("Content-Type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})


async def _jf_resolve_stream(item_id: str, max_bitrate: int) -> tuple[str, dict]:
    """Ask Jellyfin how to play an item; return (URL mpv can open, session
    info for playback reporting).

    Direct-stream when the source is already H.264; otherwise Jellyfin
    hands back an HLS TranscodingUrl (transcoded to H.264 per our device
    profile) which mpv plays and the GTX 970 hardware-decodes.
    """
    token, uid = await _jf_authenticate()
    data = await _jf_json(
        "POST", f"/Items/{item_id}/PlaybackInfo",
        params={"userId": uid},
        json={"DeviceProfile": _jf_device_profile(max_bitrate)},
    )
    sources = data.get("MediaSources") or []
    if not sources:
        raise ApiError(404, "no playable media source", "not_playable")
    ms = sources[0]
    psid = data.get("PlaySessionId", "")
    info = {"media_source_id": ms.get("Id", item_id), "play_session_id": psid}
    transcode_url = ms.get("TranscodingUrl")
    if transcode_url:
        url = f"{JELLYFIN_URL}{transcode_url}"
        if "api_key=" not in url:
            url += f"&api_key={token}"
        return url, {**info, "play_method": "Transcode"}
    if ms.get("SupportsDirectStream") or ms.get("SupportsDirectPlay"):
        url = (
            f"{JELLYFIN_URL}/Videos/{item_id}/stream"
            f"?static=true&mediaSourceId={ms.get('Id', item_id)}"
            f"&api_key={token}&PlaySessionId={psid}"
        )
        if ms.get("Container"):
            url += f"&Container={ms['Container']}"
        return url, {**info, "play_method": "DirectStream"}
    raise ApiError(502, "jellyfin returned no usable stream", "not_playable")


def _jf_report_body(sess: dict, position_s: float | None, paused: bool = False) -> dict:
    return {
        "ItemId": sess["item_id"],
        "MediaSourceId": sess["media_source_id"],
        "PlaySessionId": sess["play_session_id"],
        "PlayMethod": sess["play_method"],
        "PositionTicks": int((position_s or 0) * _TICKS),
        "IsPaused": paused,
        "CanSeek": True,
    }


async def _jf_report(path: str, body: dict) -> None:
    try:
        r = await _jf_request("POST", path, json=body)
        if r.status_code >= 400:
            logger.warning("jellyfin %s -> %s", path, r.status_code)
    except Exception as exc:  # noqa: BLE001 — reporting is best-effort
        logger.warning("jellyfin %s failed: %s", path, exc)


async def _jf_stop_session() -> None:
    """Report Stopped for the tracked Jellyfin playback (if any)."""
    global _jf_session
    sess, _jf_session = _jf_session, None
    if sess:
        logger.info("jellyfin: stopped item=%s at %.0fs", sess["item_id"], sess["last_pos"] or 0)
        await _jf_report("/Sessions/Playing/Stopped", _jf_report_body(sess, sess["last_pos"]))


async def _jf_on_state(st: dict) -> None:
    """Drive Playing/Progress/Stopped reporting from the host's mpv position."""
    sess = _jf_session
    if not sess:
        return
    item = st.get("item") or {}
    now = time.monotonic()
    if st.get("source") == "jellyfin" and item.get("id") == sess["item_id"]:
        if st.get("position_s") is not None:
            sess["last_pos"] = st["position_s"]
        paused = st.get("activity") == "paused"
        if now - sess["last_report"] >= JF_PROGRESS_INTERVAL or paused != sess["paused"]:
            sess["last_report"], sess["paused"] = now, paused
            _spawn(_jf_report("/Sessions/Playing/Progress",
                              _jf_report_body(sess, sess["last_pos"], paused)))
    elif now - sess["started"] > 15:
        # Something else is on the projector (or nothing): playback is over.
        await _jf_stop_session()


class V1JellyfinPlay(BaseModel):
    id: str
    resume: bool = False
    low_quality: bool = False


@api.post("/jellyfin/play", dependencies=authed)
async def api_jf_play(req: V1JellyfinPlay, request: Request) -> dict:
    global _jf_session
    _require_jellyfin()
    client = request.client.host if request.client else "?"
    logger.info("jellyfin play from=%s item=%s resume=%s low=%s",
                client, req.id, req.resume, req.low_quality)
    it = await _jf_get_item(req.id)
    if it.get("Type") not in ("Movie", "Episode", "Video", "MusicVideo"):
        raise ApiError(400, f"{it.get('Type')} is not playable", "not_playable")
    start = 0.0
    if req.resume:
        start = ((it.get("UserData") or {}).get("PlaybackPositionTicks") or 0) / _TICKS
    max_bitrate = _JF_LOW_BITRATE if req.low_quality else _JF_MAX_BITRATE
    stream_url, info = await _jf_resolve_stream(req.id, max_bitrate)
    card = _jf_item(it)
    if it.get("Type") == "Episode":
        s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
        tag = f"S{s}E{e}" if s is not None and e is not None else ""
        subtitle = " · ".join(x for x in (it.get("SeriesName"), tag) if x) or None
    else:
        subtitle = str(it["ProductionYear"]) if it.get("ProductionYear") else None
    meta = {"source": "jellyfin", "item": {"kind": "jellyfin", "id": req.id},
            "title": it.get("Name"), "subtitle": subtitle,
            "artwork_url": card.get("backdrop_url") or card["image_url"]}
    await _jf_stop_session()
    await _host_open(stream_url, backend="mpv", title=it.get("Name"), meta=meta, start=start)
    sess = {"item_id": req.id, **info, "last_pos": start, "paused": False,
            "started": time.monotonic(), "last_report": time.monotonic()}
    _jf_session = sess
    _spawn(_jf_report("/Sessions/Playing", _jf_report_body(sess, start)))
    return {"ok": True}


# ---------------------------------------------------------------------------
# YouTube subscriptions feed (public per-channel RSS, no account/API)
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_YTNS = "{http://www.youtube.com/xml/schemas/2015}"
# ts: last successful build; changed_at: unix ms of the last content change.
_feed_cache: dict = {"ts": 0.0, "items": [], "changed_at": 0}
_feed_lock = asyncio.Lock()
# Per-channel RSS copies: cid -> {"items", "expires"} (in-memory only).
_rss_cache: dict[str, dict] = {}
_yt_backoff: dict = {"level": 0, "until": 0.0}
_subs_cache: dict = {"ts": 0.0, "ids": [], "refreshing": False}


def _norm_feed_item(it: dict) -> dict:
    """Upgrade a cached item from the pre-v1 shape (image/short)."""
    vid = it.get("id", "")
    return {
        "id": vid,
        "url": it.get("url") or f"https://www.youtube.com/watch?v={vid}",
        "title": it.get("title") or "(untitled)",
        "channel": it.get("channel") or "",
        "channel_id": it.get("channel_id") or "",
        "published": it.get("published") or "",
        "thumbnail": it.get("thumbnail") or it.get("image") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "duration_s": it.get("duration_s"),
        "is_short": bool(it.get("is_short", it.get("short", False))),
    }


def _load_yt_state() -> None:
    """Seed the subs/feed caches from the last run's snapshot (/data)."""
    for path in (YT_STATE_FILE, "/tmp/mediacast-yt-state.json"):
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            break
        except (OSError, ValueError):
            continue
    else:
        return
    subs = state.get("subs") or {}
    if subs.get("ids"):
        _subs_cache["ids"] = subs["ids"]
        _subs_cache["ts"] = subs.get("ts", 0.0)
    feed = state.get("feed") or {}
    if feed.get("items"):
        _feed_cache["items"] = [_norm_feed_item(i) for i in feed["items"] if i.get("id")]
        _feed_cache["ts"] = feed.get("ts", 0.0)
        _feed_cache["changed_at"] = int(feed.get("changed_at") or feed.get("ts", 0.0) * 1000)


def _save_yt_state() -> None:
    try:
        tmp = YT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "subs": {"ids": _subs_cache["ids"], "ts": _subs_cache["ts"]},
                "feed": {"items": _feed_cache["items"], "ts": _feed_cache["ts"],
                         "changed_at": _feed_cache["changed_at"]},
            }, f)
        os.replace(tmp, YT_STATE_FILE)
    except OSError as exc:
        logger.warning("yt state save failed: %s", exc)


async def _refresh_account_subs() -> None:
    """Scrape the account subscriptions into the cache (background task;
    the host's yt-dlp scrape can take tens of seconds)."""
    try:
        r = await http().get(f"{HOST_BASE}/yt-subscriptions", headers=_HOST_AUTH,
                             timeout=YT_SUBS_FETCH_TIMEOUT)
        if r.status_code == 200:
            ids = [c for c in r.json().get("channel_ids", []) if _UC_RE.fullmatch(c)]
            if ids:
                changed = ids != _subs_cache["ids"]
                _subs_cache["ids"] = ids
                _subs_cache["ts"] = time.time()
                _save_yt_state()
                logger.info("account subscriptions: %d channels", len(ids))
                # Rebuild now if the list changed (cold cache → populated).
                if changed and not _feed_lock.locked():
                    _spawn(_refresh_feed())
            else:
                logger.warning("account subscriptions returned no ids")
        else:
            logger.warning("account subscriptions fetch http %s", r.status_code)
    except httpx.HTTPError as exc:
        logger.warning("account subscriptions fetch failed: %s", exc)
    finally:
        _subs_cache["refreshing"] = False


async def _account_channel_ids() -> list[str]:
    """Cached subscribed channel ids; refreshed in the background, never blocks."""
    if not YT_SUBS_FROM_ACCOUNT:
        return []
    stale = (time.time() - _subs_cache["ts"]) >= YT_SUBS_TTL or not _subs_cache["ids"]
    if stale and not _subs_cache["refreshing"]:
        _subs_cache["refreshing"] = True
        _spawn(_refresh_account_subs())
    return _subs_cache["ids"]


async def _channel_ids() -> list[str]:
    """The account's real subscriptions when we have them, else the static
    file. Deliberately not a union (placeholder channels would linger)."""
    if YT_SUBS_FROM_ACCOUNT:
        ids = await _account_channel_ids()
        if ids:
            return ids
    return _load_channel_ids()


def _load_channel_ids() -> list[str]:
    """Channel ids from the list file (bare ids, Takeout CSV rows or channel
    URLs — first UC… token per line), order-preserving and de-duped."""
    try:
        with open(YT_CHANNELS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    ids: list[str] = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = _UC_RE.search(ln)
        if m and m.group(0) not in ids:
            ids.append(m.group(0))
    return ids


def _parse_channel_rss(xml: str) -> list[dict]:
    """Newest uploads from one channel's RSS as feed items."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    channel = root.findtext(f"{_ATOM}title") or ""
    channel_id = root.findtext(f"{_YTNS}channelId") or ""
    if channel_id and not channel_id.startswith("UC"):
        channel_id = "UC" + channel_id
    out: list[dict] = []
    for e in root.findall(f"{_ATOM}entry")[:YT_PER_CHANNEL]:
        vid = e.findtext(f"{_YTNS}videoId")
        if not vid:
            continue
        out.append(_norm_feed_item({
            "id": vid,
            "title": e.findtext(f"{_ATOM}title"),
            "channel": channel,
            "channel_id": channel_id,
            "published": e.findtext(f"{_ATOM}published"),
        }))
    return out


async def _is_short(vid: str) -> bool:
    """/shorts/<id> answers 200 for an actual Short and 303s to /watch for
    everything else. Spoofs a curl UA: a browser-looking one detours
    through the /consent cookie wall (302 for everything)."""
    try:
        r = await http().head(
            f"https://www.youtube.com/shorts/{vid}",
            headers={"User-Agent": "curl/8.5.0"}, follow_redirects=False, timeout=15,
        )
        return r.status_code == 200
    except httpx.HTTPError:
        return False  # fail open: an unknown video stays visible


def _max_age(r: httpx.Response) -> float:
    m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
    return min(float(m.group(1)), YT_RSS_MAX_AGE_CAP) if m else 0.0


class _Throttled(Exception):
    """YouTube answered 429/503: stop this cycle and back off."""


async def _build_feed() -> list[dict]:
    """Merge every subscribed channel's RSS. Only channels whose cached copy
    has expired (per its max-age) are re-fetched; a 429/503 aborts the rest
    of the cycle (cached copies stand in) and raises the global backoff."""
    ids = await _channel_ids()
    if not ids:
        return []
    sem = asyncio.Semaphore(YT_FETCH_CONCURRENCY)
    known = {it["id"]: it for it in _feed_cache["items"]}
    # Channels with no RSS copy yet (fresh process) keep their last-known
    # feed items, so an aborted cycle never shrinks the feed.
    fallback: dict[str, list[dict]] = {}
    for it in _feed_cache["items"]:
        fallback.setdefault(it["channel_id"], []).append(it)
    now = time.time()
    stats = {"fetched": 0, "errors": 0, "throttled": None}

    async def fetch(cid: str) -> list[dict]:
        cached = _rss_cache.get(cid)
        if cached and cached["expires"] > now + 30:
            return cached["items"]
        old = cached["items"] if cached else fallback.get(cid, [])
        async with sem:
            if stats["throttled"]:
                return old
            try:
                r = await http().get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
                                     headers={"User-Agent": _YT_UA}, timeout=15)
            except httpx.HTTPError as exc:
                stats["errors"] += 1
                logger.warning("yt rss fetch failed for %s: %s", cid, exc)
                return old
        stats["fetched"] += 1
        if r.status_code in (429, 503):
            stats["throttled"] = stats["throttled"] or r.status_code
            return old
        if r.status_code >= 400:   # e.g. 404 for a deleted channel
            stats["errors"] += 1
            if r.status_code < 500:
                _rss_cache[cid] = {"items": [], "expires": time.time() + 3600}
                return []
            return old
        items = _parse_channel_rss(r.text)
        # Jittered expiry spreads the channels over the cycles instead of
        # all 560 coming due in the same one every 15 min.
        _rss_cache[cid] = {"items": items,
                           "expires": time.time() + _max_age(r) * random.uniform(0.3, 1.0)}
        return items

    async def tag_short(item: dict) -> None:
        if item["id"] in known:   # already checked on an earlier build
            item["is_short"] = known[item["id"]]["is_short"]
            return
        async with sem:
            item["is_short"] = await _is_short(item["id"])

    t0 = time.monotonic()
    results = await asyncio.gather(*(fetch(c) for c in ids))
    for cid in list(_rss_cache):
        if cid not in ids:
            del _rss_cache[cid]
    # Copy: RSS cache entries are shared across builds.
    items = [dict(v) for chan in results for v in chan]
    # ISO-8601 timestamps share the same offset → string sort is chronological.
    items.sort(key=lambda v: v["published"], reverse=True)
    items = items[:YT_FEED_LIMIT]
    fresh = sum(1 for it in items if it["id"] not in known)
    await asyncio.gather(*(tag_short(it) for it in items))
    logger.info("yt feed build: %d channels, %d rss fetched, %d errors, %d new shorts checks, %.1fs%s",
                len(ids), stats["fetched"], stats["errors"], fresh, time.monotonic() - t0,
                f", THROTTLED http {stats['throttled']}" if stats["throttled"] else "")
    if stats["throttled"]:
        raise _Throttled(items)
    return items


def _apply_feed(items: list[dict]) -> None:
    """Swap in a new feed; on a content change stamp changed_at and tell
    SSE clients."""
    old = {it["id"] for it in _feed_cache["items"]}
    new = {it["id"] for it in items}
    _feed_cache["items"] = items
    _feed_cache["ts"] = time.time()
    if old != new:
        _feed_cache["changed_at"] = int(time.time() * 1000)
        added, removed = len(new - old), len(old - new)
        logger.info("yt feed changed: +%d -%d", added, removed)
        _publish("feed", _feed_event(added, removed))
    _save_yt_state()


async def _refresh_feed() -> None:
    """Rebuild the feed cache; keeps the last good feed if a refresh comes
    back empty. Serialised: a caller arriving mid-refresh waits for the
    in-flight one instead of starting another."""
    if _feed_lock.locked():
        async with _feed_lock:
            return
    async with _feed_lock:
        try:
            items = await _build_feed()
            _yt_backoff["level"] = 0
        except _Throttled as exc:
            items = exc.args[0]
            _yt_backoff["level"] = min(_yt_backoff["level"] + 1, 4)
            pause = YT_FEED_REFRESH_MIN * 60 * (2 ** _yt_backoff["level"])
            _yt_backoff["until"] = time.time() + pause
            logger.warning("yt rss throttled; backing off %.0f min", pause / 60)
        if items:
            _apply_feed(items)


async def _yt_refresh_loop() -> None:
    """Rebuild the feed every YT_FEED_REFRESH_MIN regardless of clients."""
    interval = max(YT_FEED_REFRESH_MIN, 0.5) * 60
    await asyncio.sleep(5)   # let the app come up first
    while True:
        try:
            if time.time() >= _yt_backoff["until"] and await _channel_ids():
                await _refresh_feed()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — keep refreshing no matter what
            logger.exception("yt background refresh failed")
        await asyncio.sleep(interval)


async def _get_feed(force: bool = False) -> list[dict]:
    # The background loop keeps the cache warm; only a cold cache or an
    # explicit refresh builds inline (sharing the lock with the loop).
    if force or not _feed_cache["items"]:
        await _refresh_feed()
    return _feed_cache["items"]


@api.get("/youtube/feed", dependencies=authed)
async def api_youtube_feed(refresh: int = 0, shorts: int = 1) -> dict:
    if not await _channel_ids():
        raise ApiError(404, "youtube feed not configured", "not_configured")
    items = await _get_feed(force=bool(refresh))
    if not shorts:
        items = [it for it in items if not it["is_short"]]
    return {"items": items, "updated_at": int(_feed_cache["ts"] * 1000),
            "changed_at": _feed_cache["changed_at"]}


app.include_router(api)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
               include_in_schema=False)
async def api_not_found(rest: str) -> None:
    raise ApiError(404, f"no such endpoint: /api/{rest}", "not_found")


# ---------------------------------------------------------------------------
# Legacy token routes (Home Assistant, OpenClaw, HTTP Shortcuts)
# ---------------------------------------------------------------------------

class LegacyControl(BaseModel):
    action: str
    offset: int | None = None
    set: int | None = None
    delta: int | None = None
    position: float | None = None


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


async def _legacy(coro):
    try:
        return await coro
    except ApiError as exc:
        # Pre-v1 shape and status codes ({"detail"}, 404 for "no cast").
        status = 404 if exc.code == "no_active_cast" else exc.status_code
        raise HTTPException(status_code=status, detail=exc.detail)


@app.post("/cast")
async def cast(req: CastRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    await _legacy(require_token(authorization))
    try:
        _check_url(req.url)
    except ApiError as exc:
        raise HTTPException(status_code=400, detail=exc.detail)
    logger.info("cast (legacy) host=%s", urlparse(req.url).netloc)
    await _legacy(_jf_stop_session())
    await _legacy(_host_open(req.url, meta=_cast_meta(req.url)))
    return {"status": "ok"}


@app.post("/control")
async def control(req: LegacyControl, authorization: str | None = Header(default=None)) -> dict:
    await _legacy(require_token(authorization))
    return await _legacy(_host_post("/control", req.model_dump(exclude_none=True)))


# ---------------------------------------------------------------------------
# Web portal (static/)
# ---------------------------------------------------------------------------

def _portal_config() -> str:
    cfg = json.dumps({"token": TOKEN, "pairUrls": PAIR_URLS, "version": VERSION})
    # Never let a value close the <script> element early.
    return "<script>window.MEDIACAST=" + cfg.replace("</", "<\\/") + ";</script>"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    try:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    except OSError:
        html = "<!doctype html><title>mediacast</title><!--MEDIACAST_CONFIG--><p>portal files missing</p>"
    html = html.replace("<!--MEDIACAST_CONFIG-->", _portal_config(), 1)
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        # The page embeds the token: never let another site frame it.
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
