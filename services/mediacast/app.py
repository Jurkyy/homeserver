"""
mediacast — phone-to-projector URL relay.

Two surfaces, same forwarding logic:

  - POST /cast        token-protected JSON API for scripts / HTTP
                      Shortcuts on Android. Bearer MEDIACAST_TOKEN.
  - GET  /            human web UI: paste-and-cast form + a
                      drag-to-bookmarks "Cast" bookmarklet. Pairs with…
  - POST /ui-cast     same-origin, no bearer — LAN trust. Anyone who can
                      load the form can fire one. (Cross-origin browsers
                      can't reach it: no CORS headers, JSON-only body
                      blocks form-style CSRF.)

Both paths funnel into the host helper at MEDIACAST_HOST_URL, which
owns the X11/Firefox side and runs outside the container.
"""

import asyncio
import hmac
import logging
import os
import re
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

logger = logging.getLogger("mediacast")

TOKEN = os.environ.get("MEDIACAST_TOKEN", "")
HOST_URL = os.environ.get("MEDIACAST_HOST_URL", "http://host.docker.internal:8766/open")
HOST_CONTROL_URL = HOST_URL.rsplit("/", 1)[0] + "/control"
HOST_TIMEOUT = float(os.environ.get("MEDIACAST_HOST_TIMEOUT", "5.0"))
# Casting a YouTube URL makes the host helper resolve the stream with
# yt-dlp before it answers (so it can route playback through the local
# anti-throttle proxy) — a few seconds, occasionally more on a slow
# extract. Give the cast forward its own roomy budget so a normal
# resolution never trips a spurious "host unreachable" in the UI. The
# control path keeps the snappy HOST_TIMEOUT.
CAST_TIMEOUT = float(os.environ.get("MEDIACAST_CAST_TIMEOUT", "60.0"))

# YouTube subscriptions feed. Optional, like mrpflix: if the channel
# list file is missing/empty the section hides itself and the endpoint
# 404s. We read a newline-separated list of channels (a Google Takeout
# subscriptions.csv works as-is — we pull the UC… id out of each row),
# poll each channel's PUBLIC RSS feed (no account, no API key, no
# quota), merge by publish date, and show the newest uploads as a grid.
# Clicking a card casts the watch URL through the normal YouTube path
# (host helper → anti-throttle proxy → mpv), so playback rides NVDEC.
YT_CHANNELS_FILE = os.environ.get("YT_CHANNELS_FILE", "/config/yt-channels.txt")
YT_FEED_TTL = float(os.environ.get("YT_FEED_TTL", "600"))      # cache 10 min
YT_FEED_LIMIT = int(os.environ.get("YT_FEED_LIMIT", "48"))     # cards shown
YT_PER_CHANNEL = int(os.environ.get("YT_PER_CHANNEL", "6"))    # recent per channel
YT_FETCH_CONCURRENCY = int(os.environ.get("YT_FETCH_CONCURRENCY", "12"))
_YT_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
# A YouTube channel id: "UC" + 22 url-safe base64 chars.
_UC_RE = re.compile(r"UC[0-9A-Za-z_-]{22}")

# Jellyfin ("mrpflix") integration. Optional: if MRPFLIX_URL is unset
# the catalog section is simply hidden and the endpoints 404. We log in
# with the user's own (non-admin) credentials and mint a session token
# on demand — no admin API key needed. The token never leaves the
# container: the browser talks only to our /ui-jellyfin/* proxy.
JELLYFIN_URL = os.environ.get("MRPFLIX_URL", "").rstrip("/")
JELLYFIN_USER = os.environ.get("MRPFLIX_USER", "")
JELLYFIN_PASS = os.environ.get("MRPFLIX_PASS", "")
JELLYFIN_ENABLED = bool(JELLYFIN_URL and JELLYFIN_USER and JELLYFIN_PASS)
JELLYFIN_TIMEOUT = float(os.environ.get("MRPFLIX_TIMEOUT", "20.0"))
# Stable per-install device id so Jellyfin groups our sessions.
_JF_DEVICE_ID = "mediacast-" + (os.environ.get("HOSTNAME", "homeserver"))
_JF_AUTH_HEADER = (
    f'MediaBrowser Client="mediacast", Device="homeserver", '
    f'DeviceId="{_JF_DEVICE_ID}", Version="1.0"'
)
# Device profile we advertise to Jellyfin's PlaybackInfo. We can direct
# play H.264 (the only codec the projector's GTX 970 decodes in
# hardware); anything else (HEVC/AV1/VP9) Jellyfin transcodes to an
# H.264 HLS stream so mpv still rides NVDEC. See projector GPU notes.
_JF_DEVICE_PROFILE = {
    "MaxStreamingBitrate": 20_000_000,
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


async def _forward(url: str, backend: str | None = None, title: str | None = None) -> None:
    payload = {"url": url}
    if backend:
        # Tell the host helper which player to use for an already-resolved
        # URL (e.g. "mpv" for a Jellyfin stream the host can't recognise
        # by hostname). Omitted for normal casts so host-side routing
        # (video host → mpv, else Firefox) stays in charge.
        payload["backend"] = backend
    if title:
        # Display name for the UI's "Now playing" (e.g. the Jellyfin
        # movie/episode title). YouTube titles are resolved host-side by
        # yt-dlp, so plain URL casts leave this unset.
        payload["title"] = title
    try:
        async with httpx.AsyncClient(timeout=CAST_TIMEOUT) as client:
            # Forward with the bearer token — host helper binds 0.0.0.0
            # and re-validates the token as its trust boundary.
            r = await client.post(
                HOST_URL,
                json=payload,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
    except httpx.HTTPError as exc:
        logger.warning("host helper unreachable: %s", exc)
        raise HTTPException(status_code=502, detail=f"host helper unreachable: {exc}")

    if r.status_code >= 400:
        logger.warning("host helper returned %s: %s", r.status_code, r.text)
        raise HTTPException(status_code=502, detail=f"host helper error: {r.text}")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/cast")
async def cast(req: CastRequest, authorization: str | None = Header(default=None)) -> dict[str, str]:
    _check_auth(authorization)
    _check_url(req.url)
    logger.info("cast (token) host=%s", urlparse(req.url).netloc)
    await _forward(req.url)
    return {"status": "ok"}


async def _forward_control(action: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=HOST_TIMEOUT) as client:
            r = await client.post(
                HOST_CONTROL_URL,
                json=action,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"host helper unreachable: {exc}")
    try:
        body = r.json()
    except ValueError:
        body = {"detail": r.text}
    if r.status_code >= 400:
        # Propagate the host helper's status code so the UI's
        # `if (r.ok)` check fires for "no active cast" 404s and the
        # 502 surface for upstream errors.
        raise HTTPException(
            status_code=r.status_code,
            detail=body.get("detail") or body.get("error") or "control failed",
        )
    return body


class ControlRequest(BaseModel):
    action: str
    offset: int | None = None
    set: int | None = None
    delta: int | None = None
    position: float | None = None


@app.post("/control")
async def control(req: ControlRequest, authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return await _forward_control(req.model_dump(exclude_none=True))


@app.post("/ui-control")
async def ui_control(req: ControlRequest, request: Request) -> dict:
    # Same trust model as /ui-cast — LAN-only, no token. The host
    # helper still re-checks the token on its side, so this is the
    # one trust hop here.
    client_host = request.client.host if request.client else "?"
    logger.info("control (ui) from=%s action=%s", client_host, req.action)
    return await _forward_control(req.model_dump(exclude_none=True))


@app.post("/ui-cast")
async def ui_cast(req: CastRequest, request: Request) -> dict[str, str]:
    # No bearer here on purpose: this is the surface the web UI and the
    # bookmarklet talk to. Trust boundary is "you reached :8765" (LAN +
    # Tailscale only — never exposed publicly). JSON-only body blocks
    # simple-form CSRF; cross-origin fetch is preflighted and we don't
    # send CORS-allow headers.
    _check_url(req.url)
    client_host = request.client.host if request.client else "?"
    logger.info("cast (ui) from=%s host=%s", client_host, urlparse(req.url).netloc)
    await _forward(req.url)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Jellyfin ("mrpflix") catalog + playback
# ---------------------------------------------------------------------------
# A small async client over the Jellyfin REST API. The browser never sees
# the Jellyfin host or token: it browses through /ui-jellyfin/items, loads
# posters through /ui-jellyfin/image/<id>, and plays through
# /ui-jellyfin/play, all LAN-trust (same model as /ui-cast).

_jf_token: str | None = None
_jf_user_id: str | None = None
_jf_lock = asyncio.Lock()


async def _jf_authenticate(client: httpx.AsyncClient) -> tuple[str, str]:
    """Log in with the configured user creds; cache the token + user id."""
    global _jf_token, _jf_user_id
    async with _jf_lock:
        if _jf_token and _jf_user_id:
            return _jf_token, _jf_user_id
        r = await client.post(
            f"{JELLYFIN_URL}/Users/AuthenticateByName",
            headers={"Authorization": _JF_AUTH_HEADER},
            json={"Username": JELLYFIN_USER, "Pw": JELLYFIN_PASS},
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"jellyfin auth failed ({r.status_code})")
        data = r.json()
        _jf_token = data["AccessToken"]
        _jf_user_id = data["User"]["Id"]
        logger.info("jellyfin: authenticated as %s", JELLYFIN_USER)
        return _jf_token, _jf_user_id


async def _jf_request(method: str, path: str, **kw) -> httpx.Response:
    """Call the Jellyfin API with the cached token, re-authing once on 401."""
    global _jf_token, _jf_user_id
    async with httpx.AsyncClient(timeout=JELLYFIN_TIMEOUT) as client:
        token, _ = await _jf_authenticate(client)
        headers = {**kw.pop("headers", {}), "X-Emby-Token": token}
        r = await client.request(method, f"{JELLYFIN_URL}{path}", headers=headers, **kw)
        if r.status_code == 401:
            # Token expired/revoked — drop the cache, re-auth, retry once.
            async with _jf_lock:
                _jf_token = _jf_user_id = None
            token, _ = await _jf_authenticate(client)
            headers["X-Emby-Token"] = token
            r = await client.request(method, f"{JELLYFIN_URL}{path}", headers=headers, **kw)
        return r


_PLAYABLE = frozenset({"Movie", "Episode"})


def _jf_card(it: dict) -> dict:
    """Shape one Jellyfin item into the minimal card the UI renders."""
    typ = it.get("Type", "")
    name = it.get("Name", "?")
    # Episodes read better with their SxEy prefix and the series name.
    if typ == "Episode":
        s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
        tag = f"S{s}·E{e}" if s and e else "Episode"
        subtitle = f"{it.get('SeriesName', '')} · {tag}".strip(" ·")
    elif typ == "Series":
        yr = it.get("ProductionYear")
        subtitle = f"Series · {yr}" if yr else "Series"
    elif typ == "Season":
        subtitle = "Season"
    else:
        subtitle = str(it.get("ProductionYear") or "")
    has_img = "Primary" in (it.get("ImageTags") or {})
    return {
        "id": it.get("Id"),
        "name": name,
        "type": typ,
        "subtitle": subtitle,
        "playable": typ in _PLAYABLE,
        "image": f"/ui-jellyfin/image/{it.get('Id')}" if has_img else None,
    }


def _require_jellyfin() -> None:
    if not JELLYFIN_ENABLED:
        raise HTTPException(status_code=404, detail="jellyfin not configured")


@app.get("/ui-jellyfin/items")
async def jellyfin_items(parentId: str | None = None) -> dict:
    # No parent → top level: every Movie and Series the account can see.
    # With a parent → that container's children (Series→Seasons→Episodes).
    _require_jellyfin()
    _, uid = await _jf_authenticate_cached()
    params = {
        "userId": uid,
        "Fields": "ProductionYear",
        "ImageTypeLimit": 1,
        "EnableImageTypes": "Primary",
        "Limit": 500,
    }
    if parentId:
        params["parentId"] = parentId
        params["SortBy"] = "IndexNumber,SortName"
    else:
        params["IncludeItemTypes"] = "Movie,Series"
        params["Recursive"] = "true"
        params["SortBy"] = "SortName"
    r = await _jf_request("GET", "/Items", params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"jellyfin items error ({r.status_code})")
    items = [_jf_card(it) for it in r.json().get("Items", [])]
    return {"items": items}


async def _jf_authenticate_cached() -> tuple[str, str]:
    # Thin wrapper so the items route can get (token, uid) without making
    # a throwaway request; reuses the same client lifecycle.
    async with httpx.AsyncClient(timeout=JELLYFIN_TIMEOUT) as client:
        return await _jf_authenticate(client)


@app.get("/ui-jellyfin/image/{item_id}")
async def jellyfin_image(item_id: str) -> Response:
    # Proxy the Primary poster so the token stays server-side. Cached hard
    # in the browser — posters don't change.
    _require_jellyfin()
    r = await _jf_request(
        "GET", f"/Items/{item_id}/Images/Primary", params={"maxWidth": 400, "quality": 90}
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=404, detail="no image")
    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )


class JellyfinPlayRequest(BaseModel):
    itemId: str
    title: str | None = None


async def _jf_resolve_stream(item_id: str) -> str:
    """Ask Jellyfin how to play an item; return a URL mpv can open.

    Direct-stream when the source is already H.264; otherwise Jellyfin
    hands back an HLS TranscodingUrl (transcoded to H.264 per our device
    profile) which mpv plays and the GTX 970 hardware-decodes.
    """
    token, uid = await _jf_authenticate_cached()
    r = await _jf_request(
        "POST",
        f"/Items/{item_id}/PlaybackInfo",
        params={"userId": uid},
        json={"DeviceProfile": _JF_DEVICE_PROFILE},
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"jellyfin playbackinfo error ({r.status_code})")
    data = r.json()
    sources = data.get("MediaSources") or []
    if not sources:
        raise HTTPException(status_code=404, detail="no playable media source")
    ms = sources[0]
    psid = data.get("PlaySessionId", "")
    transcode_url = ms.get("TranscodingUrl")
    if transcode_url:
        url = f"{JELLYFIN_URL}{transcode_url}"
        if "api_key=" not in url:
            url += f"&api_key={token}"
        return url
    if ms.get("SupportsDirectStream") or ms.get("SupportsDirectPlay"):
        url = (
            f"{JELLYFIN_URL}/Videos/{item_id}/stream"
            f"?static=true&mediaSourceId={ms.get('Id', item_id)}"
            f"&api_key={token}&PlaySessionId={psid}"
        )
        if ms.get("Container"):
            url += f"&Container={ms['Container']}"
        return url
    raise HTTPException(status_code=502, detail="jellyfin returned no usable stream")


@app.post("/ui-jellyfin/play")
async def jellyfin_play(req: JellyfinPlayRequest, request: Request) -> dict[str, str]:
    # LAN-trust, same as /ui-cast. Resolve the item to a stream URL, then
    # hand it to the host helper forcing the mpv backend (the Jellyfin
    # host isn't in the helper's video-host list).
    _require_jellyfin()
    client_host = request.client.host if request.client else "?"
    logger.info("jellyfin play from=%s item=%s", client_host, req.itemId)
    stream_url = await _jf_resolve_stream(req.itemId)
    await _forward(stream_url, backend="mpv", title=req.title)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# YouTube subscriptions feed (public per-channel RSS, no account/API)
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_YTNS = "{http://www.youtube.com/xml/schemas/2015}"
_feed_cache: dict = {"ts": 0.0, "items": []}
_feed_lock = asyncio.Lock()


def _load_channel_ids() -> list[str]:
    """Channel ids from the list file, order-preserving and de-duped.

    Each non-comment line may be a bare UC… id, a Takeout CSV row
    (Channel Id,Channel Url,Channel Title), or a channel URL — we just
    pull the first UC… token out of the line. Handles (@name) carry no
    UC id and are skipped (Takeout gives ids, which is the supported
    path). Missing file → empty list → the section hides itself.
    """
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
    """Newest uploads from one channel's RSS as card dicts."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    channel = root.findtext(f"{_ATOM}title") or ""
    out: list[dict] = []
    for e in root.findall(f"{_ATOM}entry")[:YT_PER_CHANNEL]:
        vid = e.findtext(f"{_YTNS}videoId")
        if not vid:
            continue
        out.append({
            "id": vid,
            "title": e.findtext(f"{_ATOM}title") or "(untitled)",
            "channel": channel,
            "published": e.findtext(f"{_ATOM}published") or "",
            # Public thumbnail CDN (https loads fine on the http portal).
            "image": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    return out


async def _build_feed() -> list[dict]:
    ids = _load_channel_ids()
    if not ids:
        return []
    sem = asyncio.Semaphore(YT_FETCH_CONCURRENCY)

    async def fetch(client: httpx.AsyncClient, cid: str) -> list[dict]:
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
        async with sem:
            try:
                r = await client.get(url, headers={"User-Agent": _YT_UA})
            except httpx.HTTPError as exc:
                logger.warning("yt rss fetch failed for %s: %s", cid, exc)
                return []
        return _parse_channel_rss(r.text) if r.status_code < 400 else []

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        results = await asyncio.gather(*(fetch(client, c) for c in ids))
    items = [v for chan in results for v in chan]
    # ISO-8601 timestamps share the same offset, so a plain string sort
    # is chronological. Newest first.
    items.sort(key=lambda v: v["published"], reverse=True)
    return items[:YT_FEED_LIMIT]


async def _get_feed(force: bool = False) -> list[dict]:
    async with _feed_lock:
        fresh = (time.time() - _feed_cache["ts"]) < YT_FEED_TTL
        if not force and fresh and _feed_cache["items"]:
            return _feed_cache["items"]
        items = await _build_feed()
        if items:  # keep the last good feed if a refresh comes back empty
            _feed_cache["items"] = items
            _feed_cache["ts"] = time.time()
            return items
        return _feed_cache["items"]


@app.get("/ui-youtube/feed")
async def youtube_feed(refresh: int = 0) -> dict:
    # LAN-trust, same model as /ui-jellyfin and /ui-cast. 404 when no
    # channel list is configured so the UI hides the whole section.
    if not _load_channel_ids():
        raise HTTPException(status_code=404, detail="youtube feed not configured")
    items = await _get_feed(force=bool(refresh))
    return {"items": items}


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cast to projector</title>
<style>
  :root { color-scheme: dark light; }
  body { font: 16px/1.45 system-ui, -apple-system, sans-serif;
         max-width: 38rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { margin: 0 0 .25rem; }
  h2 { margin-top: 2rem; font-size: 1.05rem; }
  p  { margin: .5rem 0; }
  form { display: flex; gap: .5rem; margin: 1rem 0 .5rem; }
  input[type=url] { flex: 1; padding: .65rem .8rem; font-size: 1rem;
                    border: 1px solid #888; border-radius: .35rem;
                    background: transparent; color: inherit; }
  button { padding: .65rem 1.1rem; font-size: 1rem; cursor: pointer;
           border: 0; border-radius: .35rem; background: #3b82f6; color: #fff; }
  button:hover { background: #2563eb; }
  .status { padding: .55rem .8rem; border-radius: .35rem;
            min-height: 1.4em; font-size: .95rem; word-break: break-all; }
  .status.ok  { background: #1f5132; color: #d1f3dd; }
  .status.err { background: #5d1f1f; color: #f3d1d1; }
  .bm { display: inline-block; padding: .4rem .9rem; margin: .2rem 0;
        border: 1px solid currentColor; border-radius: .35rem;
        text-decoration: none; color: inherit; }
  .controls { display: flex; gap: .5rem; margin: .5rem 0; flex-wrap: wrap; }
  .ctrl { padding: .55rem .9rem; font-size: 1rem; cursor: pointer;
          border: 1px solid currentColor; border-radius: .35rem;
          background: transparent; color: inherit; min-width: 4rem; }
  .ctrl:hover { background: rgba(127,127,127,.12); }
  .seekrow { display: flex; gap: .6rem; align-items: center; margin: .5rem 0; }
  .seekrow input[type=range] { flex: 1; }
  .seekrow .t { font-variant-numeric: tabular-nums; font-size: .85rem;
                min-width: 3.2rem; opacity: .85; }
  .seekrow #cur { text-align: right; }
  .volrow { display: flex; gap: .6rem; align-items: center; margin: .5rem 0 1rem; }
  .volrow input[type=range] { flex: 1; }
  #volv { min-width: 3.2rem; text-align: right; font-variant-numeric: tabular-nums; }
  pre { background: rgba(127,127,127,.15); padding: .6rem .8rem;
        border-radius: .35rem; overflow-x: auto; font-size: .85rem; }
  small { opacity: .7; }
  .jfbar { display: flex; align-items: center; gap: .6rem; margin: .4rem 0; }
  .jfbar .crumb { font-size: .9rem; opacity: .75; word-break: break-word; }
  .grid { display: grid; gap: .7rem; margin: .6rem 0 1rem;
          grid-template-columns: repeat(auto-fill, minmax(7rem, 1fr)); }
  .card { cursor: pointer; border: 0; background: transparent; color: inherit;
          padding: 0; text-align: left; font: inherit; }
  .card img, .card .ph { width: 100%; aspect-ratio: 2/3; object-fit: cover;
          border-radius: .4rem; background: rgba(127,127,127,.18); display: block; }
  .card .ph { display: flex; align-items: center; justify-content: center; font-size: 1.8rem; }
  .card .t  { font-size: .82rem; margin-top: .25rem; line-height: 1.2; }
  .card .st { font-size: .72rem; opacity: .65; }
  .card:hover img, .card:hover .ph { outline: 2px solid #3b82f6; }
  .muted { opacity: .6; font-size: .9rem; }
  .nowtitle { font-size: 1.05rem; font-weight: 600; margin: .2rem 0 .6rem;
              line-height: 1.3; word-break: break-word; }
  .nowtitle.muted { font-weight: 400; }
  /* YouTube subscriptions grid — wider cards, 16:9 thumbnails. */
  .ytgrid { display: grid; gap: .8rem; margin: .6rem 0 1rem;
            grid-template-columns: repeat(auto-fill, minmax(10rem, 1fr)); }
  .ytcard img, .ytcard .ph { aspect-ratio: 16/9; }
  .ytcard .t { -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
               display: -webkit-box; overflow: hidden; }
  .secbar { display: flex; align-items: center; gap: .6rem; margin-top: 2rem; }
  .secbar h2 { margin: 0; }
  .secbar .refresh { margin-left: auto; padding: .3rem .7rem; font-size: .85rem;
            cursor: pointer; border: 1px solid currentColor; border-radius: .35rem;
            background: transparent; color: inherit; }
</style>
</head>
<body>
<h2>Now playing</h2>
<div id="nowtitle" class="nowtitle muted">Nothing playing</div>
<div class="seekrow" id="seekrow" style="display:none">
  <span class="t" id="cur">0:00</span>
  <input type="range" id="seek" min="0" max="100" value="0" step="1" title="Seek">
  <span class="t" id="dur">0:00</span>
</div>
<div class="controls">
  <button class="ctrl" data-action="seek" data-offset="-10" title="Back 10s">⏪ &minus;10s</button>
  <button class="ctrl" data-action="pause" title="Pause / resume">⏯</button>
  <button class="ctrl" data-action="seek" data-offset="10"  title="Forward 10s">+10s ⏩</button>
  <button class="ctrl" data-action="stop" title="Stop the cast">⏹ Stop</button>
</div>
<div class="volrow">
  <label for="vol">🔊</label>
  <input type="range" id="vol" min="0" max="100" step="2" value="60">
  <span id="volv">—%</span>
</div>
<div id="s" class="status"></div>

<h2>Cast a link</h2>
<p><small>Paste a URL to open it on the projector.</small></p>
<form id="f">
  <input type="url" id="url" placeholder="https://..." required autofocus>
  <button type="submit">Cast</button>
</form>

<section id="yt-section" style="display:none">
  <div class="secbar">
    <h2>📺 Subscriptions</h2>
    <button class="refresh" id="yt-refresh" type="button" title="Reload feed">↻ Refresh</button>
  </div>
  <div class="grid ytgrid" id="yt-grid"></div>
</section>

<section id="jf-section" style="display:none">
  <h2>🎬 mrpflix</h2>
  <div class="jfbar" id="jf-bar" style="display:none">
    <button class="ctrl" id="jf-back" type="button">← Back</button>
    <span class="crumb" id="jf-crumb"></span>
  </div>
  <div class="grid" id="jf-grid"></div>
</section>

<h2>One-tap from any page</h2>
<p>Drag this to your bookmarks bar (PC), or save it as a bookmark and
   tap it from any page (mobile):</p>
<p><a class="bm" id="bm">Cast to projector</a></p>
<details>
  <summary><small>show bookmarklet code (copy-paste for mobile)</small></summary>
  <pre id="bmcode"></pre>
</details>

<script>
const HOST = location.host;
const f = document.getElementById('f');
const i = document.getElementById('url');
const s = document.getElementById('s');
const q = new URLSearchParams(location.search);
if (q.get('url')) i.value = q.get('url');

async function cast(url) {
  s.className = 'status'; s.textContent = 'casting…';
  try {
    const r = await fetch('/ui-cast', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { s.className = 'status ok';  s.textContent = '✓ cast: ' + url; }
    else      { s.className = 'status err'; s.textContent = '✗ ' + (j.detail || r.statusText); }
  } catch (e) {
    s.className = 'status err'; s.textContent = '✗ ' + e;
  }
}
f.addEventListener('submit', e => { e.preventDefault(); cast(i.value); });
if (q.get('auto') === '1' && i.value) cast(i.value);

// Control panel: pause/seek/stop buttons + volume slider all go to
// /ui-control. Same trust model as /ui-cast (LAN-only).
async function ctrl(body) {
  try {
    const r = await fetch('/ui-control', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { s.className = 'status ok';  s.textContent = '✓ ' + (j.detail || body.action); }
    else      { s.className = 'status err'; s.textContent = '✗ ' + (j.detail || j.error || r.statusText); }
    return j;
  } catch (e) {
    s.className = 'status err'; s.textContent = '✗ ' + e;
  }
}
document.querySelectorAll('button.ctrl').forEach(b => {
  b.addEventListener('click', () => {
    const action = b.dataset.action;
    const body = { action };
    if (action === 'seek') body.offset = Number(b.dataset.offset);
    ctrl(body);
  });
});

// Volume slider — debounce so dragging doesn't fire dozens of
// pactl calls per second. 80ms is comfortably below human-perceived
// latency but lets the slider be responsive on touch.
const vol = document.getElementById('vol');
const volv = document.getElementById('volv');
let volTimer = null;
vol.addEventListener('input', () => {
  volv.textContent = vol.value + '%';
  clearTimeout(volTimer);
  volTimer = setTimeout(() => ctrl({ action: 'volume', set: Number(vol.value) }), 80);
});
// Silent status read (no toast) — drives the 1s poll for the seek bar
// and the one-time volume sync on load.
async function fetchStatus() {
  try {
    const r = await fetch('/ui-control', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action: 'status' }),
    });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? h + ':' : '') + mm + ':' + String(s2).padStart(2, '0');
}

// Seek bar. While the user drags, suppress poll updates so the thumb
// doesn't snap back; on release send an absolute seek and keep
// suppressing briefly until mpv reports the new position.
const seekrow = document.getElementById('seekrow');
const seek = document.getElementById('seek');
const cur = document.getElementById('cur');
const dur = document.getElementById('dur');
let dragging = false, suppressUntil = 0;
seek.addEventListener('input', () => { dragging = true; cur.textContent = fmt(Number(seek.value)); });
seek.addEventListener('change', () => {
  dragging = false; suppressUntil = Date.now() + 1500;
  ctrl({ action: 'seek_to', position: Number(seek.value) });
});

// Poll: refresh seek bar (and sync volume once). The seek row only
// shows while something with a known duration is playing (mpv = YouTube
// or mrpflix); a Firefox-only cast or idle screen hides it.
let volSynced = false;
async function poll() {
  const j = await fetchStatus();
  if (!volSynced && j && typeof j.volume === 'number' && j.volume >= 0) {
    vol.value = j.volume; volv.textContent = j.volume + '%'; volSynced = true;
  }
  const playing = j && typeof j.duration === 'number' && j.duration > 0
                    && typeof j.position === 'number';
  // "Now playing" title — present whenever mpv is alive (YouTube title
  // from yt-dlp, mrpflix title from the catalog). Falls back to a muted
  // placeholder when nothing's on mpv.
  const nowtitle = document.getElementById('nowtitle');
  const title = j && j.mpv && j.title ? j.title : null;
  if (title) { nowtitle.textContent = title; nowtitle.classList.remove('muted'); }
  else { nowtitle.textContent = 'Nothing playing'; nowtitle.classList.add('muted'); }
  seekrow.style.display = playing ? 'flex' : 'none';
  if (playing && !dragging && Date.now() > suppressUntil) {
    seek.max = Math.floor(j.duration);
    seek.value = Math.floor(j.position);
    cur.textContent = fmt(j.position);
    dur.textContent = fmt(j.duration);
  }
}
poll();
setInterval(poll, 1000);

// mrpflix (Jellyfin) catalog. Browse a poster grid; click a movie or
// episode to play it on the projector via mpv (same controls above).
// Folders (series/seasons) drill in. If the server isn't configured the
// first fetch 404s and we leave the whole section hidden.
const jfSection = document.getElementById('jf-section');
const jfGrid = document.getElementById('jf-grid');
const jfBar = document.getElementById('jf-bar');
const jfCrumb = document.getElementById('jf-crumb');
const jfBack = document.getElementById('jf-back');
let jfStack = [];  // breadcrumb: [{id, name}, ...]

function esc(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

async function jfLoad(parentId) {
  jfGrid.innerHTML = '<span class="muted">loading…</span>';
  try {
    const u = parentId ? '/ui-jellyfin/items?parentId=' + encodeURIComponent(parentId)
                       : '/ui-jellyfin/items';
    const r = await fetch(u);
    if (!r.ok) {
      if (!jfStack.length) jfSection.style.display = 'none';   // not configured
      else jfGrid.innerHTML = '<span class="muted">unavailable</span>';
      return;
    }
    jfSection.style.display = '';
    renderJf((await r.json()).items || []);
  } catch (e) { if (!jfStack.length) jfSection.style.display = 'none'; }
}

function renderJf(items) {
  jfBar.style.display = jfStack.length ? 'flex' : 'none';
  jfCrumb.textContent = jfStack.map(x => x.name).join(' › ');
  jfGrid.innerHTML = '';
  if (!items.length) { jfGrid.innerHTML = '<span class="muted">empty</span>'; return; }
  for (const it of items) {
    const card = document.createElement('button');
    card.className = 'card'; card.type = 'button';
    const art = it.image
      ? '<img loading="lazy" alt="" src="' + it.image +
        '" onerror="this.outerHTML=\\'<div class=ph>🎬</div>\\'">'
      : '<div class="ph">' + (it.playable ? '▶' : '📁') + '</div>';
    card.innerHTML = art + '<div class="t">' + esc(it.name) + '</div>' +
                     '<div class="st">' + esc(it.subtitle || '') + '</div>';
    card.addEventListener('click', () => it.playable ? jfPlay(it) : jfEnter(it));
    jfGrid.appendChild(card);
  }
}

function jfEnter(it) { jfStack.push({ id: it.id, name: it.name }); jfLoad(it.id); }
jfBack.addEventListener('click', () => {
  jfStack.pop();
  const top = jfStack[jfStack.length - 1];
  jfLoad(top ? top.id : null);
});

async function jfPlay(it) {
  s.className = 'status'; s.textContent = 'casting ' + it.name + '…';
  // Episodes read better with their series + SxEy prefix; movies are
  // just the title. This is what shows under "Now playing".
  const title = it.type === 'Episode' && it.subtitle
    ? it.subtitle + ' · ' + it.name : it.name;
  try {
    const r = await fetch('/ui-jellyfin/play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ itemId: it.id, title }),
    });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { s.className = 'status ok';  s.textContent = '✓ playing: ' + it.name; }
    else      { s.className = 'status err'; s.textContent = '✗ ' + (j.detail || r.statusText); }
  } catch (e) { s.className = 'status err'; s.textContent = '✗ ' + e; }
}

jfLoad(null);  // initial top-level load (hides itself if not configured)

// YouTube subscriptions feed. Newest uploads from the configured
// channels (merged from public RSS server-side). Click a card → cast
// the video via the same YouTube pipeline as a pasted link. The whole
// section stays hidden if no channel list is configured (feed 404s).
const ytSection = document.getElementById('yt-section');
const ytGrid = document.getElementById('yt-grid');
const ytRefresh = document.getElementById('yt-refresh');

function ago(iso) {
  const t = Date.parse(iso); if (isNaN(t)) return '';
  const d = Math.max(0, (Date.now() - t) / 1000);
  if (d < 3600)      return Math.floor(d / 60) + 'm ago';
  if (d < 86400)     return Math.floor(d / 3600) + 'h ago';
  if (d < 86400 * 7) return Math.floor(d / 86400) + 'd ago';
  return Math.floor(d / (86400 * 7)) + 'w ago';
}

async function ytLoad(refresh) {
  ytGrid.innerHTML = '<span class="muted">loading…</span>';
  try {
    const r = await fetch('/ui-youtube/feed' + (refresh ? '?refresh=1' : ''));
    if (!r.ok) { ytSection.style.display = 'none'; return; }   // not configured
    ytSection.style.display = '';
    const items = (await r.json()).items || [];
    ytGrid.innerHTML = '';
    if (!items.length) { ytGrid.innerHTML = '<span class="muted">no recent videos</span>'; return; }
    for (const it of items) {
      const card = document.createElement('button');
      card.className = 'card ytcard'; card.type = 'button';
      const meta = [esc(it.channel || ''), ago(it.published)].filter(Boolean).join(' · ');
      card.innerHTML =
        '<img loading="lazy" alt="" src="' + it.image +
        '" onerror="this.outerHTML=\\'<div class=ph>📺</div>\\'">' +
        '<div class="t">' + esc(it.title) + '</div>' +
        '<div class="st">' + meta + '</div>';
      card.addEventListener('click', () => cast(it.url));
      ytGrid.appendChild(card);
    }
  } catch (e) { ytSection.style.display = 'none'; }
}
ytRefresh.addEventListener('click', () => ytLoad(true));
ytLoad(false);

// Bookmarklet: open the UI in a new tab with ?url=<current>&auto=1.
// This dodges mixed-content (HTTPS page → HTTP API) because the new
// tab is loaded over plain HTTP and then does the POST same-origin.
const bm = "javascript:void(window.open('http://" + HOST +
           "/?url='+encodeURIComponent(location.href)+'&auto=1','_blank'))";
document.getElementById('bm').setAttribute('href', bm);
document.getElementById('bmcode').textContent = bm;
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)
