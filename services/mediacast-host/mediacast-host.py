#!/usr/bin/env python3
"""
mediacast-host — the X11/Firefox side of the projector cast pipeline.

Runs as a systemd --user unit on the host (not in a container) because
firing X11 calls and managing a Firefox instance from inside a
container is more pain than it's worth (xauth, fonts, GL, audio).

Trust model:
    mediacast container --> POST http://host.docker.internal:8766/open
        with header `Authorization: Bearer <MEDIACAST_TOKEN>` and
        body `{"url": "https://..."}`.

The token is shared with the container via the same .env file.
Binding to 0.0.0.0 (the docker bridge gateway isn't a stable
loopback-equivalent for compose networks) is intentional; the token
is the trust boundary. The container has already validated the
scheme is http(s), but we re-check defensively.

stdlib only — no pip install on the host. Keeps deployment trivial
and survives Python upgrades without breaking.
"""

from __future__ import annotations

import base64
import glob
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

LOG = logging.getLogger("mediacast-host")

PORT = int(os.environ.get("MEDIACAST_HOST_PORT", "8766"))
TOKEN = os.environ.get("MEDIACAST_TOKEN", "")
# Real path, not the bare command name — sudoers rules match exact paths,
# and /sbin/poweroff -> systemctl on this box (see install_mediacast_sudo
# in bootstrap.sh, which grants NOPASSWD for exactly this path).
POWEROFF_BIN = os.environ.get("MEDIACAST_POWEROFF_BIN", "/sbin/poweroff")
FIREFOX_BIN = os.environ.get("MEDIACAST_FIREFOX_BIN", "firefox-esr")
MPV_BIN = os.environ.get("MEDIACAST_MPV_BIN", "/usr/bin/mpv")
# Unix socket mpv listens on for JSON-IPC. Lets us cycle pause / send
# other commands to the running player without spawning a fresh
# process. Lives under /tmp so /run/user permissions (or its absence
# during early boot) doesn't bite us.
MPV_IPC_SOCK = os.environ.get(
    "MEDIACAST_MPV_SOCK", "/tmp/mediacast-mpv.sock"
)
# Latest yt-dlp from pipx — Debian's apt package lags by ~12 months and
# YouTube's anti-bot signatures iterate weekly, so apt's yt-dlp dies on
# "Sign in to confirm you're not a bot" for most videos. The pipx
# install bin path is stable across user sessions.
YTDLP_BIN = os.environ.get(
    "MEDIACAST_YTDLP_BIN",
    f"{os.path.expanduser('~')}/.local/bin/yt-dlp",
)
# Subprocess wall-clock budget. Firefox `--new-tab` returns once it has
# handed the URL to the running instance, which is usually fast; bound
# it so a wedged Firefox doesn't hold the HTTP response open.
SUBPROC_TIMEOUT = float(os.environ.get("MEDIACAST_SUBPROC_TIMEOUT", "10.0"))

# Hosts whose URLs we route through mpv+yt-dlp instead of Firefox.
# Trying to play these in a browser hits a wall of anti-embed,
# anti-autoplay, and consent-banner gates; mpv talks to the same
# streaming endpoints directly with none of that surface area.
# Anything not in this set falls through to Firefox.
MPV_HOSTS = frozenset({
    "youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com",
    "vimeo.com",
    "twitch.tv", "clips.twitch.tv",
    "dailymotion.com", "dai.ly",
    "soundcloud.com",
})

# YouTube only. These hosts stream from googlevideo, which throttles
# open-ended HTTP range reads (what ffmpeg/mpv send) to ~1.5x real-time
# — barely above playback, so the demuxer cache underruns once or twice
# a minute and mpv pauses to rebuffer. (Decode is fine: nvdec, zero
# dropped frames.) Bounded range requests are NOT throttled (full
# ~6 MB/s), so we resolve the stream URL ourselves and feed mpv through
# a local chunking proxy (see GVProxyHandler) that converts mpv's one
# open-ended read into a stream of bounded sub-requests. Non-YouTube
# MPV_HOSTS and Jellyfin (LAN, no throttle) keep their existing paths.
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com",
})

# Shared yt-dlp format ladder: prefer 1080p H.264 (avc1) + m4a so the
# GTX 970's NVDEC decodes it (no VP9/AV1 block on Maxwell — see projector
# GPU notes). Used both by the in-mpv ytdl_hook path and by our own
# pre-resolution for the proxy path, so they pick identical streams.
YTDLP_FORMAT = (
    "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
    "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
)


def _default_cookies_from_browser() -> str:
    """yt-dlp --cookies-from-browser spec to authenticate with YouTube.

    YouTube gates this host's IP behind "Sign in to confirm you're not a
    bot": every anonymous extraction (all player_clients) is refused, so
    yt-dlp has to ride a logged-in browser's cookies. We point it at the
    always-on Firefox ESR (mediacast-firefox.service) — log into a
    (throwaway, ban-risk) YouTube account there once and both the proxy
    resolve path and the in-mpv ytdl_hook fallback authenticate from it.

    Override with MEDIACAST_YTDLP_COOKIES_FROM_BROWSER (any yt-dlp browser
    spec, e.g. "firefox:/path/to/profile" or "chromium"); set it empty to
    disable cookie auth entirely.
    """
    env = os.environ.get("MEDIACAST_YTDLP_COOKIES_FROM_BROWSER")
    if env is not None:
        return env.strip()
    # Firefox keeps each profile in ~/.mozilla/firefox/<rand>.<name>.
    # firefox-esr runs the *.default-esr profile, but the bare "firefox"
    # selector picks profiles.ini's Default=, which here is a different,
    # never-logged-in profile — so target the ESR profile explicitly.
    matches = sorted(
        glob.glob(os.path.expanduser("~/.mozilla/firefox/*.default-esr"))
    )
    if matches:
        return f"firefox:{matches[0]}"
    return "firefox"


# Resolved once at startup. Empty string => no cookie auth.
COOKIES_FROM_BROWSER = _default_cookies_from_browser()


def _ytdlp_cookie_args() -> list[str]:
    """CLI args injecting browser cookies into a direct yt-dlp call."""
    if not COOKIES_FROM_BROWSER:
        return []
    return ["--cookies-from-browser", COOKIES_FROM_BROWSER]


# Where YouTube watch URLs play. YouTube now gates this host's IP behind
# "Sign in to confirm you're not a bot" — for yt-dlp AND the logged-out
# web player alike — so the mpv+yt-dlp resolve/proxy path (the historical
# default, "mpv") dead-ends on a black screen. With a YouTube account
# logged into the projector's Firefox ESR, the web player authenticates
# from its session and just plays; "firefox" routes YouTube there and
# drives transport over MPRIS (see _playerctl). Flip back to "mpv" to
# revert to the resolve/proxy path (still present, untouched).
YOUTUBE_BACKEND = os.environ.get("MEDIACAST_YOUTUBE_BACKEND", "firefox").strip()

# MPRIS player-name prefix for the cast Firefox. playerctl matches any
# org.mpris.MediaPlayer2.firefox.instance_* the running Firefox registers
# once an HTML5 <video> (YouTube included) is actually playing.
PLAYERCTL_BIN = os.environ.get("MEDIACAST_PLAYERCTL_BIN", "playerctl")
PLAYERCTL_PLAYER = os.environ.get("MEDIACAST_PLAYERCTL_PLAYER", "firefox")

# Max seconds to wait for a freshly-opened YouTube tab to actually start
# playing before sending YouTube's 'f' (its own video-fullscreen, so the
# video fills the projector rather than sitting amid page furniture). We
# poll the MPRIS player for "Playing" instead of guessing a fixed delay —
# 'f' only fullscreens once the player exists and the page has focus.
YT_FULLSCREEN_WAIT = float(os.environ.get("MEDIACAST_YT_FULLSCREEN_WAIT", "15"))

# Local chunking proxy: binds 127.0.0.1 only (mpv is the sole client),
# so no auth — reachable only from this host's loopback.
PROXY_PORT = int(os.environ.get("MEDIACAST_PROXY_PORT", "8767"))
# Bounded sub-request size against googlevideo. 10 MiB is comfortably
# inside the "fast" regime (measured ~6 MB/s for bounded reads) while
# keeping per-request overhead negligible.
PROXY_CHUNK = int(os.environ.get("MEDIACAST_PROXY_CHUNK", str(10 * 1024 * 1024)))
# yt-dlp resolution can outlast a normal subprocess budget (network +
# signature work), so it gets its own, longer wall-clock cap.
RESOLVE_TIMEOUT = float(os.environ.get("MEDIACAST_RESOLVE_TIMEOUT", "45.0"))
# Scraping the account's subscriptions page can enumerate many channels;
# give it a generous cap (flat-playlist, so no per-channel extraction).
SUBS_TIMEOUT = float(os.environ.get("MEDIACAST_SUBS_TIMEOUT", "90.0"))
UPSTREAM_TIMEOUT = float(os.environ.get("MEDIACAST_UPSTREAM_TIMEOUT", "30.0"))
# A plain desktop UA — googlevideo serves the signed URL to any client;
# bounded ranges work with no special headers (verified with curl).
PROXY_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

# ---------------------------------------------------------------------------
# Screensaver: casts a long looping ambient video when the projector's been
# idle a while, or immediately when picked from the portal.
# ---------------------------------------------------------------------------
# A small, fixed set of curated defaults — not a user-editable list like
# yt-channels.txt — override any one with its MEDIACAST_SCREENSAVER_*_URL
# env var (any http(s) URL the normal cast pipeline can play, not just
# YouTube). label, url.
SCREENSAVER_THEMES: dict[str, tuple[str, str]] = {
    "woods": ("🌲 Woods", os.environ.get(
        "MEDIACAST_SCREENSAVER_WOODS_URL",
        "https://www.youtube.com/watch?v=NveAIUhEi3M")),
    "waterfall": ("💧 Waterfall", os.environ.get(
        "MEDIACAST_SCREENSAVER_WATERFALL_URL",
        "https://www.youtube.com/watch?v=wX2L28WZwHo")),
    "fire": ("🔥 Fire", os.environ.get(
        "MEDIACAST_SCREENSAVER_FIRE_URL",
        "https://www.youtube.com/watch?v=ZOAExL-xIDM")),
}
SCREENSAVER_DEFAULT = os.environ.get("MEDIACAST_SCREENSAVER_DEFAULT", "woods")
# How long the projector can sit with nothing actually playing before the
# default screensaver starts on its own. 0 disables auto-trigger — the
# portal's buttons still work either way. Checked by a background thread
# (see _screensaver_watchdog), so this fires even if no one has the portal
# page open to notice the idle projector.
SCREENSAVER_IDLE_TIMEOUT = float(os.environ.get("MEDIACAST_SCREENSAVER_IDLE_TIMEOUT", "900"))
SCREENSAVER_POLL_INTERVAL = float(os.environ.get("MEDIACAST_SCREENSAVER_POLL_INTERVAL", "20"))


def _run(cmd: list[str], timeout: float = SUBPROC_TIMEOUT) -> tuple[int, str]:
    """Run cmd, return (exit_code, combined_stdout_stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"binary not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def wake_display() -> str:
    # `xset dpms force on` brings the HDMI output back from DPMS blank.
    # `xset s reset` cancels the X screensaver countdown so the wake
    # sticks for a few minutes. We ignore the exit codes — if X isn't
    # up yet, we still want to fall through to firefox (which will
    # complain more loudly and surface a useful error).
    rc1, out1 = _run(["xset", "dpms", "force", "on"])
    rc2, out2 = _run(["xset", "s", "reset"])
    return f"xset dpms rc={rc1} ({out1!r}); xset s rc={rc2} ({out2!r})"


def open_in_firefox(url: str) -> tuple[int, str]:
    # `--new-tab` talks to a Firefox already running in the same
    # profile via remote control. If none is running, Firefox starts
    # fresh and uses the URL as the initial tab. Either way works.
    return _run([FIREFOX_BIN, "--new-tab", url])


def is_video_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    host = host.removeprefix("www.")
    return host in MPV_HOSTS


def open_in_mpv(
    url: str,
    *,
    use_ytdl: bool = True,
    audio_url: str | None = None,
    title: str | None = None,
) -> tuple[int, str]:
    """Spawn a detached fullscreen mpv with the given URL.

    Why mpv: a stock Firefox playing a YouTube watch URL faces three
    independent gates — the GDPR consent banner, YouTube's autoplay
    block (Firefox's media.autoplay.default=0 covers the browser side
    but YouTube's own JS checks navigator.userActivation), and the
    fullscreen-on-load problem (no user gesture to enter HTML5
    fullscreen). mpv talks to the same streaming endpoints via yt-dlp
    and shows the result in a native fullscreen window with none of
    these gates.

    use_ytdl=False is for already-resolved direct media URLs (e.g. a
    Jellyfin HLS/stream URL the container produced via the Jellyfin
    API). Running the yt-dlp hook on those just adds a doomed
    extraction attempt + latency, so we pass --no-ytdl and skip the
    YouTube-shaped --ytdl-format. Hardware decode + the deeper cache
    still apply — Jellyfin transcodes to H.264 for us, so NVDEC works.

    Any in-progress mpv is killed first so the projector shows one
    thing at a time. mpv runs detached (start_new_session=True) so
    a host-helper restart doesn't take down the playback.
    """
    _run(["pkill", "-f", MPV_BIN])
    # 200 ms is enough for SIGTERM to land + the X server to release
    # the fullscreen grab before we ask for it again.
    time.sleep(0.2)
    args = [
        MPV_BIN,
        "--fullscreen",
        "--really-quiet",
        # JSON-IPC socket for pause/resume from /pause endpoint.
        f"--input-ipc-server={MPV_IPC_SOCK}",
        # Hardware-decode on the GTX 970's NVDEC block. auto-safe
        # only engages a decoder the GPU actually has (H.264/HEVC
        # on this Maxwell GM204) and silently falls back to
        # software otherwise, so it never wedges playback.
        "--hwdec=auto-safe",
        # Deeper demuxer buffer so a transient network dip absorbs
        # into readahead instead of pausing playback to rebuffer.
        "--cache=yes",
        "--demuxer-max-bytes=150MiB",
        "--demuxer-readahead-secs=60",
    ]
    if title:
        # Drives the UI's "Now playing" label (read back as mpv's
        # media-title). Needed on the --no-ytdl paths (YouTube proxy,
        # Jellyfin), where mpv would otherwise show the raw stream URL;
        # the ytdl_hook path sets media-title on its own from metadata.
        args.append(f"--force-media-title={title}")
    if use_ytdl:
        args += [
            # Use the pipx yt-dlp, not apt's stale one.
            f"--script-opts=ytdl_hook-ytdl_path={YTDLP_BIN}",
            # Prefer H.264 (avc1) — the only 1080p codec this GTX 970
            # decodes in hardware (Maxwell GM204 has no VP9/AV1 block).
            # See YTDLP_FORMAT and the projector GPU notes.
            f"--ytdl-format={YTDLP_FORMAT}",
        ]
        if COOKIES_FROM_BROWSER:
            # Same browser-cookie auth as the direct resolve path, handed
            # through to the yt-dlp the ytdl_hook spawns. -append avoids
            # comma-escaping the profile path. Without this the hook hits
            # the identical "confirm you're not a bot" wall and mpv opens
            # to a black screen (nothing plays).
            args.append(
                "--ytdl-raw-options-append="
                f"cookies-from-browser={COOKIES_FROM_BROWSER}"
            )
    else:
        # Pre-resolved direct URL(s) — don't let the ytdl hook touch it.
        args.append("--no-ytdl")
        if audio_url:
            # YouTube 1080p is split video+audio; we resolved them
            # separately and proxy each, so mux the audio track in.
            args.append(f"--audio-file={audio_url}")
    args.append(url)
    try:
        proc = subprocess.Popen(
            args,
            env={
                **os.environ,
                "DISPLAY": os.environ.get("DISPLAY", ":0"),
                "XAUTHORITY": os.environ.get(
                    "XAUTHORITY", os.path.expanduser("~/.Xauthority")
                ),
                # PulseAudio socket lives here for the user-session.
                "XDG_RUNTIME_DIR": os.environ.get(
                    "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
                ),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return 127, f"binary not found: {MPV_BIN}"
    # Give mpv a beat to either start playing or die on its face.
    time.sleep(1.5)
    rc = proc.poll()
    if rc is None:
        return 0, f"mpv pid={proc.pid}"
    return rc or 1, f"mpv exited rc={rc} (likely yt-dlp extractor failure)"


# ---------------------------------------------------------------------------
# YouTube anti-throttle: resolve + local chunking proxy
# ---------------------------------------------------------------------------

def is_youtube_host(url: str) -> bool:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host in YOUTUBE_HOSTS


def _proxy_url(upstream: str) -> str:
    # Wrap an upstream googlevideo URL as a loopback proxy URL mpv opens.
    # The upstream is base64url-encoded so its own query string survives
    # intact through our query string.
    token = base64.urlsafe_b64encode(upstream.encode("utf-8")).decode("ascii")
    return f"http://127.0.0.1:{PROXY_PORT}/s?u={quote(token)}"


def resolve_youtube(url: str) -> tuple[str, str | None, str] | None:
    """Resolve a YouTube watch URL to (video_url, audio_url|None, title).

    Runs yt-dlp ourselves (instead of letting mpv's ytdl_hook do it) so
    we can route the resulting googlevideo URLs through the chunking
    proxy. A single call prints the title first, then one `urls` line
    per selected stream: for 1080p YouTube that's two (separate video +
    audio); for a progressive/combined format it's one. The title rides
    along so the UI's "Now playing" can show it (the proxy path uses
    --no-ytdl, so mpv can't read the title itself). Returns None on any
    failure so the caller can fall back to the in-mpv ytdl_hook path.
    """
    rc, out = _run(
        [YTDLP_BIN, "-f", YTDLP_FORMAT, "--no-warnings",
         *_ytdlp_cookie_args(),
         "--print", "%(title)s", "--print", "urls", url],
        timeout=RESOLVE_TIMEOUT,
    )
    if rc != 0:
        LOG.warning("yt-dlp resolve failed rc=%s: %s", rc, out[:200])
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    urls = [ln for ln in lines if ln.startswith("http")]
    if not urls:
        LOG.warning("yt-dlp resolve produced no urls: %s", out[:200])
        return None
    # The first non-URL line is the title (empty string if it somehow
    # didn't print — playback still works, the UI just shows no title).
    title = next((ln for ln in lines if not ln.startswith("http")), "")
    # yt-dlp prints video before audio for a "video+audio" selection.
    return (urls[0], urls[1] if len(urls) > 1 else None, title)


def scrape_subscriptions() -> list[str] | None:
    """UC… channel ids the logged-in YouTube account is subscribed to.

    yt-dlp reads the account's subscriptions page using the same browser
    cookies as everything else; --flat-playlist keeps it to one listing
    request (no per-channel extraction), printing one channel id per row.
    The container turns these into the public per-channel RSS feeds it
    already renders, so this just auto-populates that list from the
    account instead of a hand-maintained file. Returns a de-duped,
    order-preserving list, or None on any failure (no cookies, not logged
    in, yt-dlp error) so the container keeps its static fallback list.
    """
    if not COOKIES_FROM_BROWSER:
        LOG.warning("subscriptions scrape skipped: no cookies configured")
        return None
    rc, out = _run(
        [YTDLP_BIN, "--flat-playlist", "--no-warnings",
         *_ytdlp_cookie_args(),
         "--print", "%(channel_id)s",
         "https://www.youtube.com/feed/channels"],
        timeout=SUBS_TIMEOUT,
    )
    if rc != 0:
        LOG.warning("yt-dlp subscriptions scrape failed rc=%s: %s", rc, out[:200])
        return None
    ids: list[str] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("UC") and ln not in ids:
            ids.append(ln)
    LOG.info("subscriptions scrape: %d channels", len(ids))
    return ids or None


class GVProxyHandler(BaseHTTPRequestHandler):
    """Loopback proxy that defeats googlevideo's open-ended-range throttle.

    mpv/ffmpeg streams a media URL with a single open-ended request
    (`Range: bytes=N-`), which googlevideo rate-limits to ~1.5x
    real-time. Bounded requests (`bytes=a-b`) are served at full speed,
    so for each request from mpv we fan the byte range out into a
    sequence of bounded PROXY_CHUNK sub-requests to the upstream and
    splice them back together. To mpv it looks like one ordinary
    seekable resource; to googlevideo it looks like a well-behaved
    chunked download. Net effect: the demuxer cache fills to its full
    readahead and never underruns.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.debug("gvproxy " + fmt, *args)

    def _upstream(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(url, headers={"User-Agent": PROXY_UA})

    def _total_size(self, url: str) -> int | None:
        # A 1-byte ranged GET returns `Content-Range: bytes 0-0/<total>`,
        # which is how we learn the full length to answer mpv's seeks.
        req = self._upstream(url)
        req.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
                cr = r.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)\s*$", cr)
                if m:
                    return int(m.group(1))
                cl = r.headers.get("Content-Length")
                return int(cl) if cl else None
        except Exception as exc:  # noqa: BLE001 — any failure → 502 upstream
            LOG.warning("gvproxy size probe failed: %s", exc)
            return None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/s":
            self.send_error(404)
            return
        token = parse_qs(parsed.query).get("u", [""])[0]
        try:
            upstream = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            self.send_error(400)
            return

        total = self._total_size(upstream)
        if total is None:
            self.send_error(502)
            return

        # Parse mpv's requested range. Open-ended ("bytes=N-") and
        # absent both mean "to the end"; we always know `total`.
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
        else:
            start, end = 0, total - 1
        if start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.end_headers()
            return
        end = min(end, total - 1)
        length = end - start + 1

        self.send_response(206 if rng else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()

        if self.command == "HEAD":
            return

        # Stream the range as bounded sub-requests — the part that
        # sidesteps the throttle. Stop quietly if mpv hangs up (seek or
        # stop closes the socket); that's normal, not an error.
        pos = start
        try:
            while pos <= end:
                sub_end = min(pos + PROXY_CHUNK - 1, end)
                req = self._upstream(upstream)
                req.add_header("Range", f"bytes={pos}-{sub_end}")
                with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
                    while True:
                        buf = r.read(256 * 1024)
                        if not buf:
                            break
                        self.wfile.write(buf)
                pos = sub_end + 1
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            LOG.warning("gvproxy stream aborted at byte %d: %s", pos, exc)
            return

    do_HEAD = do_GET


def start_proxy() -> None:
    # Loopback-only; mpv on this host is the only client. Runs in a
    # daemon thread alongside the main control server so there's no
    # extra systemd unit to manage.
    srv = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), GVProxyHandler)
    # Without this, a request thread stuck on a slow/stalled upstream
    # read is non-daemon and outlives a clean shutdown — systemd's
    # Restart=always then has to wait out StopTimeout and SIGKILL before
    # it can come back, turning one wedged request into a projector
    # outage instead of a blip.
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, name="gvproxy", daemon=True)
    t.start()
    LOG.info("gvproxy listening on 127.0.0.1:%d", PROXY_PORT)


def focus_firefox() -> str:
    # wmctrl raises and focuses the most recent Firefox window. If
    # wmctrl isn't installed or no window matches, fall back to
    # xdotool. Neither failing is fatal — Firefox itself usually
    # raises on `--new-tab` thanks to the WM honoring its activation
    # request — so we just collect diagnostics.
    rc, out = _run(["wmctrl", "-a", "Firefox"])
    if rc == 0:
        return f"wmctrl rc=0"
    rc2, out2 = _run(["xdotool", "search", "--name", "Firefox", "windowactivate"])
    return f"wmctrl rc={rc} ({out!r}); xdotool rc={rc2} ({out2!r})"


def _mpv_command(cmd: list) -> tuple[int, str]:
    """Send a JSON-IPC command to the running mpv. Returns (rc, detail).

    rc==0 on success, rc==404 if no mpv is reachable (treated as
    "no active cast" upstream so control falls through to Firefox),
    rc==1 for unexpected errors.

    mpv 0.40 leaves the socket file behind on `quit`, so after a stop
    the next command would hit ECONNREFUSED on a path that exists.
    We treat ECONNREFUSED + ENOENT identically (mpv is gone) and
    unlink the stale path so subsequent state queries are clean.
    """
    import errno as _errno
    import socket as _sock

    if not os.path.exists(MPV_IPC_SOCK):
        return 404, "mpv socket missing"
    try:
        s = _sock.socket(_sock.AF_UNIX)
        s.settimeout(2.0)
        s.connect(MPV_IPC_SOCK)
        s.sendall(json.dumps({"command": cmd}).encode("utf-8") + b"\n")
        resp = s.recv(512)
        s.close()
        return 0, resp.decode("utf-8", "replace").strip()
    except OSError as exc:
        if exc.errno in (_errno.ECONNREFUSED, _errno.ENOENT):
            # mpv is gone but didn't clean up after itself. Tidy up
            # so subsequent calls don't have to repeat this dance.
            try:
                os.unlink(MPV_IPC_SOCK)
            except OSError:
                pass
            return 404, f"mpv gone ({exc.errno})"
        return 1, f"mpv ipc error: {exc}"


def _firefox_key(key: str) -> tuple[int, str]:
    """Synthesize a keypress on the Firefox window via xdotool.

    --clearmodifiers strips a stuck Shift/Ctrl on the attached USB
    keyboard so the keypress is unmodified.
    """
    rc, out = _run([
        "xdotool", "search", "--name", "Mozilla Firefox",
        "key", "--clearmodifiers", key,
    ])
    return rc, f"xdotool key {key} rc={rc} ({out!r})"


def _no_cast_or(firefox_rc: int, firefox_out: str) -> tuple[int, str]:
    # xdotool rc=1 means "no window matched" — i.e. no Firefox cast
    # is on screen either. Surface that as 404 (no active cast) so
    # the UI shows a clean message instead of a generic 502.
    if firefox_rc == 1:
        return 404, f"no active cast ({firefox_out})"
    return firefox_rc, firefox_out


def _playerctl(*args: str, timeout: float = 4.0) -> tuple[int, str]:
    """Drive the cast Firefox's MPRIS player via playerctl.

    Firefox publishes an org.mpris.MediaPlayer2.firefox.instance_* player
    on the user session bus once an HTML5 <video> is actually playing —
    YouTube included — exposing real transport: play/pause, absolute and
    relative seek, position, duration, title (full mpv-IPC parity, which
    keystrokes can't give). rc!=0 (typically "No players found") means no
    Firefox playback we can drive; callers treat it like mpv's 404 and
    fall through to the next tier.
    """
    return _run([PLAYERCTL_BIN, "-p", PLAYERCTL_PLAYER, *args], timeout=timeout)


# ---------------------------------------------------------------------------
# Firefox CDP: read/seek the YouTube <video> (drives the portal scrub bar)
# ---------------------------------------------------------------------------
# Firefox exposes no usable MPRIS position for YouTube (it sticks at 0), so
# for a real scrub bar + absolute seek we talk CDP — the Chrome DevTools
# Protocol Firefox still serves when remote.active-protocols includes it —
# to the cast Firefox over loopback. Runtime.evaluate against the page
# reads video.currentTime/duration and sets currentTime to seek.

CDP_PORT = int(os.environ.get("MEDIACAST_CDP_PORT", "9222"))
CDP_BASE = f"http://127.0.0.1:{CDP_PORT}"
# _cdp_eval opens a brand-new WebSocket connection per call. Two threads
# doing that at once — the portal's ~1Hz status poll and
# youtube_go_fullscreen's own ~3Hz settle-loop polling right after a cast
# — measurably contend with each other (observed: normally ~0.1s calls
# stretching to 1.5-2s during that overlap, which is what made the portal
# feel like it hung right after casting). Serializing all CDP access
# behind one lock keeps calls to one at a time instead of two connections
# fighting over Firefox's single debugger session.
_cdp_lock = threading.Lock()
# Read currentTime/duration/paused + a cleaned title in one round-trip.
_CDP_STATE_JS = (
    "(()=>{const v=document.querySelector('video');return v?"
    "{t:v.currentTime,d:v.duration,p:v.paused,"
    # Strip YouTube's "(3) " unread-count prefix and " - YouTube" suffix.
    "title:(document.title||'').replace(/^\\(\\d+\\)\\s*/,'').replace(/ - YouTube$/,'')}"
    ":null})()"
)


def _cdp_target_ws(prefer: str | None = None) -> str | None:
    """webSocketDebuggerUrl of the cast Firefox's active page target.

    With `prefer`, only matches a tab whose URL contains that substring
    and returns None if no such tab exists yet — used to pin
    youtube_go_fullscreen's polling to the specific tab a cast just
    opened, so a slow-to-load new tab can never fall through to
    whatever unrelated tab happens to be open (see that function).
    Without `prefer`: prefers a YouTube tab, else the first page — the
    "whatever's currently showing" behavior status polling wants. None
    if the CDP endpoint isn't up (remote debugging off / Firefox down)
    so callers fall back.
    """
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json/list", timeout=2) as r:
            targets = json.loads(r.read())
    except (OSError, ValueError):
        return None
    pages = [t for t in targets
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    if prefer:
        m = next((t for t in pages if prefer in (t.get("url") or "")), None)
        return m["webSocketDebuggerUrl"] if m else None
    yt = next((t for t in pages if "youtube.com" in (t.get("url") or "")), pages[0])
    return yt["webSocketDebuggerUrl"]


def _cdp_pages() -> list:
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json/list", timeout=2) as r:
            return [t for t in json.loads(r.read()) if t.get("type") == "page"]
    except (OSError, ValueError):
        return []


def _yt_video_id(url: str) -> str:
    """The 11-char video id from a YouTube URL, or '' if not a watch URL."""
    try:
        u = urlparse(url)
    except ValueError:
        return ""
    host = u.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        return u.path.lstrip("/").split("/")[0]
    if "youtube" in host:
        q = parse_qs(u.query)
        if q.get("v"):
            return q["v"][0]
        parts = u.path.split("/")
        if len(parts) > 2 and parts[1] in ("shorts", "live", "embed"):
            return parts[2]
    return ""


def _cdp_close_other_tabs(keep_vid: str) -> int:
    """Close other YouTube tabs (keep the one for keep_vid) so the projector
    plays one video — no leaked background tabs stealing audio or confusing
    the CDP/MPRIS target. about:home and non-YouTube tabs are left alone."""
    if not keep_vid:
        return 0
    closed = 0
    for t in _cdp_pages():
        url = t.get("url") or ""
        if keep_vid in url or not ("youtube.com" in url or "youtu.be" in url):
            continue
        try:
            urllib.request.urlopen(f"{CDP_BASE}/json/close/{t['id']}", timeout=2).read()
            closed += 1
        except (OSError, ValueError, KeyError):
            pass
    return closed


def _cdp_close_media_tabs() -> int:
    """Close *every* open tab — YouTube or not — so nothing is left behind
    for a new cast to collide with.

    Used to only target YouTube/youtu.be tabs, leaving any other stray tab
    (a manual-browsing leftover, an ad popup/redirect) open and, crucially,
    still eligible to be the one with OS keyboard focus. The projector cast
    flow drives playback via real X11 keystrokes ('k'/'f') aimed at
    whatever tab currently has focus — not at a specific tab — because
    YouTube requires a real user-activation gesture, not a JS-triggered
    one, to start playback. If a stray tab was still open and still
    focused when those keystrokes fired, *that* tab would play/fullscreen
    instead of the new cast. Closing everything down to one tab before
    every cast removes that possibility outright, rather than relying on
    winning a focus race. This is a single-purpose kiosk display — the
    projector shows one thing at a time — so there's no "user's other tab"
    to preserve here.

    Guard: never close the last remaining page. Firefox tears down the whole
    window when its final tab closes, and mediacast-firefox.service's
    Restart=always would then flap the browser (3s black screen + cold
    start) mid-cast. If a tab is the only page left we leave it open and
    rely on the MPRIS/CDP pause in stop_active_playback to silence it."""
    pages = _cdp_pages()
    remaining = len(pages)
    closed = 0
    for t in pages:
        if remaining <= 1:
            break  # last page — leave it; closing it would kill the window
        try:
            urllib.request.urlopen(f"{CDP_BASE}/json/close/{t['id']}", timeout=2).read()
            closed += 1
            remaining -= 1
        except (OSError, ValueError, KeyError):
            pass
    return closed


def _cdp_eval(expr: str, timeout: float = 3.0, prefer: str | None = None):
    """Runtime.evaluate `expr` in the cast Firefox page; return its value.

    Minimal stdlib WebSocket client, one short-lived connection per call —
    simple and robust at the portal's ~1 Hz poll. returnByValue gives JSON
    back. Returns None on any failure (no CDP, no <video>, JS error).

    `prefer` narrows the target to a specific tab (see _cdp_target_ws) —
    None here just means "no matching tab (yet)", not an error, so it's
    the quiet early-return above, not logged.
    """
    import socket as _sock
    import struct as _struct

    ws_url = _cdp_target_ws(prefer)
    if not ws_url:
        return None
    s = None
    # Serialized (see _cdp_lock) so this is always the only CDP call in
    # flight — a status poll now waits a beat instead of racing a second
    # connection against Firefox's debugger session.
    with _cdp_lock:
        try:
            u = urlparse(ws_url)
            s = _sock.create_connection((u.hostname, u.port), timeout=timeout)
            s.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode()
            path = u.path + (("?" + u.query) if u.query else "")
            s.sendall((
                f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(1)
                if not chunk:
                    return None
                buf += chunk
            if b" 101 " not in buf.split(b"\r\n", 1)[0]:
                return None

            def send(text: str) -> None:
                payload = text.encode()
                mask = os.urandom(4)
                n = len(payload)
                hdr = bytearray([0x81])
                if n < 126:
                    hdr.append(0x80 | n)
                elif n < 65536:
                    hdr.append(0x80 | 126); hdr += _struct.pack(">H", n)
                else:
                    hdr.append(0x80 | 127); hdr += _struct.pack(">Q", n)
                hdr += mask
                s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

            def rx(n: int) -> bytes:
                data = b""
                while len(data) < n:
                    part = s.recv(n - len(data))
                    if not part:
                        raise OSError("ws closed")
                    data += part
                return data

            def recv() -> dict:
                h = rx(2)
                n = h[1] & 0x7f
                if n == 126:
                    n = _struct.unpack(">H", rx(2))[0]
                elif n == 127:
                    n = _struct.unpack(">Q", rx(8))[0]
                return json.loads(rx(n).decode("utf-8", "replace"))

            # Firefox CDP needs Runtime.enable to create the page's execution
            # context before evaluate works ("context is null" otherwise). Wait
            # for the executionContextCreated event, then evaluate in it.
            send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            for _ in range(25):
                msg = recv()
                if msg.get("method") == "Runtime.executionContextCreated":
                    break
                if msg.get("id") == 1:
                    # enable acked; context event should follow imminently
                    continue
            send(json.dumps({
                "id": 2, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
            }))
            for _ in range(25):
                msg = recv()
                if msg.get("id") == 2:
                    return (msg.get("result", {}).get("result", {}) or {}).get("value")
            return None
        except (OSError, ValueError, KeyError, IndexError) as exc:
            # ws_url was found (a real target existed), so this is a genuine
            # failure mid-connection/eval — e.g. Firefox's CDP port wedged.
            # Worth a log line: this is the quiet failure mode behind past
            # portal hangs, where nothing else recorded that CDP had stopped
            # answering.
            LOG.warning("cdp eval failed (%s): %s", type(exc).__name__, exc)
            return None
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def firefox_cdp_state() -> dict | None:
    """{position, duration, paused, title} from the YouTube <video> via CDP,
    or None when CDP/the video isn't available (caller falls back to MPRIS)."""
    v = _cdp_eval(_CDP_STATE_JS)
    if not isinstance(v, dict):
        return None
    pos, dur = v.get("t"), v.get("d")
    return {
        "position": pos if isinstance(pos, (int, float)) else None,
        "duration": dur if isinstance(dur, (int, float)) and dur > 0 else None,
        "paused": bool(v.get("p")),
        "title": v.get("title") or None,
        # CDP gives a live, seekable position → the portal can show a
        # tracking scrub bar (unlike the MPRIS fallback, which is frozen).
        "seekable": True,
    }


def _cdp_seek_abs(seconds: float) -> bool:
    v = _cdp_eval(
        "(()=>{const v=document.querySelector('video');if(!v)return null;"
        f"v.currentTime=Math.max(0,{seconds:.3f});return v.currentTime}})()"
    )
    return isinstance(v, (int, float))


def _cdp_seek_rel(offset: int) -> bool:
    v = _cdp_eval(
        "(()=>{const v=document.querySelector('video');if(!v)return null;"
        f"v.currentTime=Math.max(0,v.currentTime+({int(offset)}));return v.currentTime}})()"
    )
    return isinstance(v, (int, float))


def _cdp_pause_toggle() -> bool:
    v = _cdp_eval(
        "(()=>{const v=document.querySelector('video');if(!v)return null;"
        "if(v.paused)v.play();else v.pause();return v.paused})()"
    )
    return isinstance(v, bool)


def firefox_playback_state() -> dict:
    """{position, duration, paused, title} for a browser cast.

    CDP first (real currentTime/duration for the YouTube <video>, so the
    scrub bar tracks); falls back to MPRIS (title + paused work, but
    YouTube position is unavailable) when CDP is off/unreachable.
    """
    cdp = firefox_cdp_state()
    if cdp is not None:
        return cdp
    empty = {"position": None, "duration": None, "paused": None,
             "title": None, "seekable": False}
    rc, out = _playerctl(
        "metadata", "--format",
        "{{status}};;{{position}};;{{mpris:length}};;{{xesam:title}}",
    )
    if rc != 0:
        return empty
    parts = out.strip().split(";;")
    if len(parts) != 4 or parts[0] not in ("Playing", "Paused"):
        return empty

    def _num(s: str):
        try:
            return float(s)
        except ValueError:
            return None

    pos_us, len_us = _num(parts[1]), _num(parts[2])
    return {
        "position": pos_us / 1_000_000 if pos_us is not None else None,
        "duration": len_us / 1_000_000 if len_us else None,
        "paused": parts[0] == "Paused",
        "title": parts[3] or None,
        # MPRIS position for YouTube is frozen at 0 — not a usable scrub.
        "seekable": False,
    }


def control_pause() -> tuple[int, str]:
    # Tier 1: mpv (true toggle via IPC — Jellyfin, or the legacy YouTube
    # mpv backend). Tier 2: Firefox MPRIS play/pause (browser YouTube).
    # Tier 3: a space keystroke for any other HTML5 page that never
    # registered an MPRIS player.
    rc, out = _mpv_command(["cycle", "pause"])
    if rc != 404:
        return rc, f"mpv: {out}"
    if _cdp_pause_toggle():
        return 0, "cdp: play-pause"
    prc, pout = _playerctl("play-pause")
    if prc == 0:
        return 0, "mpris: play-pause"
    return _no_cast_or(*_firefox_key("space"))


def control_seek(offset: int) -> tuple[int, str]:
    # Relative seek in seconds (positive = forward).
    rc, out = _mpv_command(["seek", offset, "relative"])
    if rc != 404:
        return rc, f"mpv seek {offset:+d}s: {out}"
    # Firefox/YouTube: CDP sets video.currentTime exactly. Keystroke
    # fallback if CDP is off (j/l = YouTube 10s, repeated for bigger
    # offsets; arrows = HTML5 5s for sub-10s deltas).
    if _cdp_seek_rel(offset):
        return 0, f"cdp seek {offset:+d}s"
    if abs(offset) >= 10:
        key = "l" if offset > 0 else "j"
        reps = max(1, round(abs(offset) / 10))
        last = (1, "no key sent")
        for _ in range(reps):
            last = _firefox_key(key)
        return _no_cast_or(*last)
    return _no_cast_or(*_firefox_key("Right" if offset > 0 else "Left"))


def control_seek_abs(seconds: float) -> tuple[int, str]:
    # Absolute seek to a position in seconds. mpv (Jellyfin) → Firefox
    # MPRIS (browser YouTube: a bare position in seconds). There's no
    # reliable absolute-seek keystroke, so a Firefox page with no MPRIS
    # player reports no active cast.
    rc, out = _mpv_command(["seek", seconds, "absolute"])
    if rc != 404:
        return rc, f"mpv seek -> {seconds:.0f}s: {out}"
    # Firefox/YouTube absolute seek via CDP (sets video.currentTime).
    if _cdp_seek_abs(seconds):
        return 0, f"cdp seek -> {seconds:.0f}s"
    return 404, "no active cast"


def mpv_playback_state() -> dict:
    """Current {position, duration, paused, title} from mpv, or Nones if idle.

    Reads a few properties over one IPC connection. Matches each reply by
    request_id so an interleaved mpv event line can't be mistaken for the
    answer. Any failure (no socket, stale socket, unparseable) degrades
    to all-None, which the UI reads as "nothing playing".
    """
    import socket as _sock

    empty = {"position": None, "duration": None, "paused": None,
             "title": None, "seekable": False}
    if not os.path.exists(MPV_IPC_SOCK):
        return empty
    try:
        s = _sock.socket(_sock.AF_UNIX)
        s.settimeout(2.0)
        s.connect(MPV_IPC_SOCK)
    except OSError:
        return empty

    def get(prop: str, rid: int):
        try:
            s.sendall(json.dumps({"command": ["get_property", prop], "request_id": rid}).encode() + b"\n")
            buf = b""
            while True:
                chunk = s.recv(1024)
                if not chunk:
                    return None
                buf += chunk
                for line in buf.split(b"\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == rid:
                        return msg.get("data") if msg.get("error") == "success" else None
        except OSError:
            return None

    try:
        return {
            "position": get("time-pos", 1),
            "duration": get("duration", 2),
            "paused": get("pause", 3),
            "title": get("media-title", 4),
            "seekable": True,  # mpv reports a live, seekable position
        }
    finally:
        s.close()


def control_stop() -> tuple[int, str]:
    # mpv: quit cleanly so the IPC socket file gets unlinked. Firefox:
    # close the active tab via Ctrl+W; the WM keeps Firefox itself
    # running because mediacast-firefox.service spawns one window.
    rc, out = _mpv_command(["quit"])
    if rc == 0:
        return 0, f"mpv quit: {out}"
    if rc != 404:
        # mpv is present but its IPC isn't answering — e.g. it wedged on a
        # GPU/VT switch when the session locked mid-playback. A clean quit
        # is impossible, so hard-kill it. The stale socket is unlinked by
        # the next _mpv_command. Stop must always work from the UI.
        _run(["pkill", "-f", MPV_BIN])
        return 0, f"mpv killed (ipc unresponsive: {out})"
    rc2, out2 = _firefox_key("ctrl+w")
    if rc2 == 0:
        return 0, "firefox tab closed"
    return _no_cast_or(rc2, out2)


def stop_active_playback() -> str:
    """Explicitly end whatever is currently on the projector before a new
    cast starts, so the old video never keeps playing underneath the new one.

    control_stop() is either/or (mpv XOR the focused Firefox tab); casting a
    fresh URL needs a hard reset of *both* backends. Without it a new cast
    layers on top: an mpv cast (mrpflix/Jellyfin mpv-direct, YouTube-proxy)
    left a prior Firefox YouTube tab playing audio behind mpv, and
    Firefox-over-Firefox briefly overlapped two players fighting for the
    MPRIS/CDP target. Every step here is best-effort and safe to run with
    nothing playing."""
    notes = []
    # mpv (video-host, YouTube-proxy, mrpflix/Jellyfin): quit cleanly via IPC
    # if it answers, else hard-kill. rc==0 => it quit; anything else (404
    # no-socket, or a wedged IPC) => pkill catch-all.
    rc, _ = _mpv_command(["quit"])
    if rc != 0:
        _run(["pkill", "-f", MPV_BIN])
    notes.append(f"mpv(rc={rc})")
    # Firefox: an MPRIS "stop" only pauses and a paused YouTube tab keeps its
    # audio context (can ad-roll/resume), so pause AND close the stale tabs.
    # Pause first (MPRIS + a CDP <video>/<audio> pause covering the last tab
    # the close guard leaves open), then close the extra YouTube tabs.
    _playerctl("pause")
    _cdp_eval(
        "document.querySelectorAll('video,audio')"
        ".forEach(function(m){try{m.pause();}catch(e){}}); true"
    )
    closed = _cdp_close_media_tabs()
    notes.append(f"ff_paused ff_closed={closed}")
    return " ".join(notes)


def control_volume(*, absolute: int | None = None, delta: int | None = None) -> tuple[int, str]:
    """Set or nudge the default PulseAudio sink volume.

    pactl talks to the user-session PulseAudio (the same instance
    Firefox + mpv push audio into). It does NOT affect librespot,
    which writes to ALSA directly — that volume is controlled from
    the Spotify app and is intentionally separate.

    `set-sink-volume @DEFAULT_SINK@` is a single source of truth:
    whatever sink the user has selected (typically the Scarlett) is
    the one that moves.
    """
    if absolute is not None:
        absolute = max(0, min(100, int(absolute)))
        arg = f"{absolute}%"
    elif delta is not None:
        sign = "+" if delta >= 0 else "-"
        arg = f"{sign}{abs(int(delta))}%"
    else:
        return 1, "neither set nor delta provided"
    rc, out = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", arg])
    if rc != 0:
        return rc, f"pactl: {out!r}"
    # Read back the volume so the UI can render the new value.
    rc2, out2 = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    # Output looks like "Volume: front-left: 39322 / 60% / -13.36 dB, ..."
    import re
    m = re.search(r"(\d+)\s*%", out2)
    pct = int(m.group(1)) if m else -1
    return 0, f"{arg} -> {pct}%"


def control_volume_get() -> tuple[int, int]:
    rc, out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if rc != 0:
        return rc, -1
    import re
    m = re.search(r"(\d+)\s*%", out)
    return 0, (int(m.group(1)) if m else -1)


def youtube_go_fullscreen(url: str = "") -> str:
    """Settle a freshly-cast YouTube tab: close stale tabs, window-fullscreen,
    start playback, video-fullscreen — as soon as the player is confirmed
    ready (don't wait for autoplay), never before.

    Runs in a background thread off the cast request. YouTube often does
    NOT autoplay the watch page, so waiting for playback wastes ~10s; the
    page itself is ready in ~3s. We poll CDP — pinned to this cast's own
    tab — until the <video> has metadata (duration known), close any other
    YouTube tabs (one video, no leaked background audio), throw the window
    fullscreen, then start playback and fullscreen the video.

    Playback is driven by a direct CDP call to video.play() — no OS focus
    or keystroke involved at all, so it can't land on the wrong tab.
    Works because Firefox's enterprise Autoplay permission (see
    firefox-policies.json) allows programmatic play with no real gesture
    required. Video-fullscreen still needs the 'f' keystroke, though:
    verified empirically that Firefox's Fullscreen API rejects a
    CDP-triggered requestFullscreen() ("Fullscreen request denied") with
    no prior real input event, and Firefox's CDP doesn't implement
    Input.dispatchKeyEvent to synthesize one scoped to a specific tab
    (confirmed — the command just times out, unanswered). So 'f' is a
    real X11 keystroke via _firefox_key, same OS-focus caveat as before —
    but by this point in the function every other stale tab has already
    been closed and CDP has confirmed *this* tab is the ready one, so the
    window this lands on should always be correct in practice. See
    docs/projector-cast.md for the "wrong video played" bug history this
    is all mitigating.
    """
    vid = _yt_video_id(url)
    deadline = time.time() + YT_FULLSCREEN_WAIT
    closed = False
    ready = False
    # Poll for YouTube's player API (#movie_player.getPlayerState). On the
    # un-played watch page there's no <video> yet — only the thumbnail +
    # play button — so we drive the player API instead of the element.
    # Every eval is pinned to *this cast's* tab (prefer=vid), not "the
    # first CDP page" — see the note below on why that matters.
    while time.time() < deadline:
        if vid and not closed and any(vid in (t.get("url") or "") for t in _cdp_pages()):
            _cdp_close_other_tabs(vid)
            closed = True
        if _cdp_eval("!!(document.querySelector('#movie_player')"
                     "&&document.querySelector('#movie_player').getPlayerState)",
                     prefer=vid) is True:
            ready = True
            break
        time.sleep(0.3)

    if not ready:
        # The new tab never came up as a ready YouTube player within
        # YT_FULLSCREEN_WAIT (a slow/failed `--new-tab`, or Firefox just
        # not keeping up). The 'k'/'f' keystrokes below are raw X
        # keypresses aimed at whatever window/tab currently has focus,
        # not at this cast's tab specifically — if we sent them anyway,
        # a stale unrelated tab (leftover browsing, an ad popup) would
        # be the one that gets unpaused and fullscreened instead of the
        # cast. Bail out here rather than risk that.
        LOG.warning(
            "youtube_go_fullscreen: %s never became ready after %.0fs — "
            "skipping playback/fullscreen keystrokes to avoid driving an "
            "unrelated tab", vid or url, YT_FULLSCREEN_WAIT,
        )
        return "ready=False (skipped keystrokes)"

    # Only now — once CDP has confirmed *this specific tab* has a ready
    # player — do we throw the window fullscreen (EWMH, hides chrome).
    # Doing this eagerly, synchronously, right after firing `--new-tab`
    # (the old code did exactly that) races Firefox's own tab switch: if
    # the browser hadn't visually moved to the new tab yet, fullscreening
    # the window just blew up whatever was still showing to fill the
    # screen. Tying it to this confirmed-ready point closes that race.
    fullscreen_firefox()

    def player_state():
        return _cdp_eval("(()=>{const p=document.querySelector('#movie_player');"
                         "return p&&p.getPlayerState?p.getPlayerState():null})()",
                         prefer=vid)

    # 1) Ensure playing: call video.play() directly in the target tab
    # until the player reports playing(1)/buffering(3). No OS focus or
    # window activation involved — this can't hit the wrong tab.
    for _ in range(8):
        if player_state() in (1, 3):
            break
        _cdp_eval(
            "(()=>{const v=document.querySelector('video');"
            "if(v)v.play().catch(()=>{});return null})()",
            prefer=vid,
        )
        time.sleep(0.5)
    # 2) Fullscreen. A CDP-triggered requestFullscreen() is rejected with
    # no prior real input event (verified — see the docstring), so this
    # still needs the 'f' keystroke (a real gesture). By this point every
    # stale tab is already closed and CDP has confirmed *this* tab is the
    # ready one, so OS focus should reliably be on the right window.
    # Retried since a single 'f' often misses if the player isn't focused
    # yet; only sent while NOT already fullscreen, so it never toggles
    # back out.
    fs = False
    for _ in range(5):
        if _cdp_eval("!!document.fullscreenElement", prefer=vid) is True:
            fs = True
            break
        focus_firefox()
        time.sleep(0.15)
        _firefox_key("f")
        time.sleep(0.6)
    LOG.info("youtube fullscreen ready=%s playing=%s fs=%s", ready, player_state(), fs)
    return f"ready={ready} fs={fs}"


def fullscreen_firefox() -> str:
    # Force the Firefox window into EWMH fullscreen state, hiding the
    # URL bar + tabs + window decorations. Idempotent — `add,fullscreen`
    # is a no-op if the window is already fullscreen. Done on every cast
    # so a user who exits fullscreen mid-session gets snapped back next
    # time something is cast.
    rc, out = _run(["wmctrl", "-r", "Firefox", "-b", "add,fullscreen"])
    return f"wmctrl fs rc={rc} ({out!r})"


def _check_auth(header: str | None) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    presented = header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(presented, TOKEN)


def _check_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---------------------------------------------------------------------------
# Cast dispatch + screensaver/screen-off state
# ---------------------------------------------------------------------------
# _state is touched by request-handling threads (/open, /screensaver,
# /screen-off) and by the idle watchdog thread, hence the lock.
_state_lock = threading.Lock()
_state: dict = {
    "screensaver": None,   # theme key currently showing, or None
    "screen_off": False,   # display explicitly turned off via /screen-off
}


def _cast(url: str, want_backend: str = "", want_title: str | None = None) -> tuple[int, str, str]:
    """Pick a backend for `url`, drive it, return (rc, backend, out).

    This is the entire "how a URL ends up on the projector" logic, shared
    by /open, /screensaver, and the idle screensaver watchdog — there's
    exactly one implementation of it, the endpoints just differ in what
    URL they pass in and how they report the result.
    """
    dpms = wake_display()

    # Explicitly end whatever is currently cast before starting the new
    # one — finished or not. Otherwise the old video keeps playing
    # underneath (audio leak, MPRIS/CDP target confusion); see
    # stop_active_playback.
    stopped = stop_active_playback()

    # Route video URLs through mpv (no embed gates, no consent
    # banner, no autoplay block, native fullscreen). Everything
    # else goes to Firefox where browser semantics make sense
    # (twitter threads, articles, dashboards, etc.). If mpv
    # fails on a video URL we fall back to Firefox rather than
    # leaving the projector dark.
    backend = "firefox"
    focus = ""
    fs = ""
    if want_backend == "mpv":
        # Pre-resolved direct media URL — play it as-is, no yt-dlp.
        _run(["wmctrl", "-r", "Firefox", "-b", "remove,fullscreen"])
        rc, out = open_in_mpv(url, use_ytdl=False, title=want_title)
        backend = "mpv-direct"
    elif is_youtube_host(url) and YOUTUBE_BACKEND == "firefox":
        # Browser YouTube: play in the logged-in Firefox (its session
        # clears YouTube's "confirm you're not a bot" wall, which now
        # blocks both yt-dlp and the logged-out web player from this
        # IP). Transport (pause/seek/stop/status) rides MPRIS — see
        # the control_* tiers. Kill any mpv first so its fullscreen
        # doesn't sit on top of the browser.
        _run(["pkill", "-f", MPV_BIN])
        rc, out = open_in_firefox(url)
        focus = focus_firefox()
        # Deliberately NOT fullscreening here. This used to call
        # fullscreen_firefox() immediately — before Firefox had
        # necessarily finished switching to the new tab — which could
        # throw a still-visible stale tab fullscreen within a second
        # of casting. youtube_go_fullscreen() now does the window
        # fullscreen itself, gated on CDP confirming *this* tab is
        # the one that's actually ready.
        if rc == 0:
            # Autoplay is on (enterprise policy). Put the video
            # fullscreen in the background once it starts playing so
            # the cast response returns immediately instead of
            # blocking on the player load.
            threading.Thread(
                target=youtube_go_fullscreen, args=(url,),
                name="yt-fs", daemon=True,
            ).start()
        backend = "firefox-youtube"
    elif is_video_host(url):
        # On a new cast, the previous Firefox tab (if any) is no
        # longer wanted — kill its fullscreen so mpv owns the
        # screen alone. Cheap and idempotent.
        _run(["wmctrl", "-r", "Firefox", "-b", "remove,fullscreen"])
        if is_youtube_host(url):
            # YouTube → resolve the googlevideo stream ourselves and
            # play it through the chunking proxy so the open-ended
            # throttle never bites. If resolution fails for any
            # reason, fall back to the in-mpv ytdl_hook path (still
            # plays, just throttle-prone) before Firefox.
            resolved = resolve_youtube(url)
            if resolved:
                video_url, audio_url, yt_title = resolved
                rc, out = open_in_mpv(
                    _proxy_url(video_url),
                    use_ytdl=False,
                    audio_url=_proxy_url(audio_url) if audio_url else None,
                    title=yt_title or want_title,
                )
                backend = "mpv-proxy"
            else:
                rc, out = open_in_mpv(url)
                backend = "mpv(ytdl-fallback)"
        else:
            rc, out = open_in_mpv(url)
            backend = "mpv"
        if rc != 0:
            LOG.warning("mpv failed (%s) — falling back to firefox", out)
            rc, out = open_in_firefox(url)
            focus = focus_firefox()
            fs = fullscreen_firefox()
            backend = "firefox(fallback)"
    else:
        # Stop any in-progress mpv so a Firefox cast actually
        # ends up visible — mpv's fullscreen would otherwise sit
        # on top of the browser window.
        _run(["pkill", "-f", MPV_BIN])
        rc, out = open_in_firefox(url)
        focus = focus_firefox()
        fs = fullscreen_firefox()

    LOG.info(
        "cast url_host=%s backend=%s rc=%s stopped=[%s] dpms=[%s] focus=[%s] fs=[%s]",
        urlparse(url).netloc, backend, rc, stopped, dpms, focus, fs,
    )
    return rc, backend, out


def _current_activity() -> bool:
    """Is anything actually playing right now (mpv or Firefox)? Reuses
    the same state readers the /control status action polls, so "idle"
    here means the exact same thing the portal's Now-playing UI does."""
    if mpv_playback_state()["position"] is not None:
        return True
    if firefox_playback_state()["position"] is not None:
        return True
    return False


def _start_screensaver(theme: str) -> tuple[int, str, str]:
    entry = SCREENSAVER_THEMES.get(theme)
    if not entry:
        return 400, "", f"unknown screensaver theme: {theme!r}"
    _, url = entry
    rc, backend, out = _cast(url)
    with _state_lock:
        _state["screen_off"] = False
        _state["screensaver"] = theme if rc == 0 else None
    LOG.info("screensaver theme=%s rc=%s backend=%s", theme, rc, backend)
    return rc, backend, out


def _screensaver_watchdog() -> None:
    """Background thread: auto-start the default screensaver once the
    projector's sat with nothing actually playing for
    SCREENSAVER_IDLE_TIMEOUT — independent of whether anyone has the
    portal page open to notice. Never fires over an explicit screen-off,
    and re-arms itself once real playback (or a different screensaver
    pick) starts, so it only acts once per idle spell.

    Known gap: if the running screensaver's own (8-12h) video somehow
    ends on its own, _state["screensaver"] stays set to that theme and
    this won't re-trigger — not worth the extra bookkeeping for a video
    length nothing here realistically reaches."""
    if SCREENSAVER_IDLE_TIMEOUT <= 0:
        return
    idle_since: float | None = None
    while True:
        time.sleep(SCREENSAVER_POLL_INTERVAL)
        with _state_lock:
            screen_off = _state["screen_off"]
            already = _state["screensaver"]
        if screen_off or already:
            idle_since = None
            continue
        if _current_activity():
            idle_since = None
            continue
        if idle_since is None:
            idle_since = time.time()
            continue
        if time.time() - idle_since >= SCREENSAVER_IDLE_TIMEOUT:
            LOG.info("screensaver: idle for %.0fs, auto-starting %r",
                      time.time() - idle_since, SCREENSAVER_DEFAULT)
            _start_screensaver(SCREENSAVER_DEFAULT)
            idle_since = None


class Handler(BaseHTTPRequestHandler):
    # Quiet down the default per-request access log to stderr; we log
    # our own structured lines.
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        LOG.debug(fmt, *args)

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True})
        elif self.path == "/yt-subscriptions":
            # Authenticated: hands the container the account's subscribed
            # channel ids (scraped via yt-dlp + browser cookies) so it can
            # build the feed from the real account, not a static file.
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            ids = scrape_subscriptions()
            if ids is None:
                self._json(502, {"error": "subscriptions unavailable"})
                return
            self._json(200, {"channel_ids": ids})
        elif self.path == "/state":
            # Screensaver/screen-off state + the theme list, for the
            # portal's screensaver buttons and status line.
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            with _state_lock:
                st = dict(_state)
            self._json(200, {
                "themes": {k: label for k, (label, _url) in SCREENSAVER_THEMES.items()},
                "screensaver": st["screensaver"],
                "screen_off": st["screen_off"],
            })
        else:
            self._json(404, {"error": "not found"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _do_control(self, body: dict) -> None:
        action = body.get("action", "")
        if action == "pause":
            rc, out = control_pause()
        elif action == "stop":
            rc, out = control_stop()
        elif action == "seek":
            offset = body.get("offset", 0)
            try:
                offset = int(offset)
            except (TypeError, ValueError):
                self._json(400, {"error": "offset must be int"})
                return
            rc, out = control_seek(offset)
        elif action == "seek_to":
            position = body.get("position")
            try:
                position = float(position)
            except (TypeError, ValueError):
                self._json(400, {"error": "position must be a number"})
                return
            rc, out = control_seek_abs(position)
        elif action == "volume":
            if "set" in body:
                rc, out = control_volume(absolute=body["set"])
            elif "delta" in body:
                rc, out = control_volume(delta=body["delta"])
            else:
                self._json(400, {"error": "volume needs `set` or `delta`"})
                return
        elif action == "status":
            # Read-only: current volume + playback position/duration. The
            # UI polls this to drive the volume slider and the seek bar.
            # "active" is derived from whether we actually read a position
            # (a stale mpv socket alone doesn't count). mpv (Jellyfin /
            # legacy YouTube) wins if present; otherwise fall to the
            # Firefox MPRIS player (browser YouTube) so the seek bar +
            # now-playing work the same for both.
            t0 = time.monotonic()
            _, vol = control_volume_get()
            pb = mpv_playback_state()
            backend = "mpv"
            if pb["position"] is None:
                fb = firefox_playback_state()
                if fb["position"] is not None:
                    pb, backend = fb, "firefox"
            active = pb["position"] is not None
            # This poll runs every ~1s from the portal; logging every hit
            # would be pure noise. But a slow one is exactly the signal
            # for "the portal hung" reports — flag it so a look at
            # journalctl afterward finds it without turning on debug
            # logging and waiting for a repeat.
            elapsed = time.monotonic() - t0
            if elapsed > 1.5:
                LOG.warning(
                    "status poll slow: %.1fs (backend=%s active=%s)",
                    elapsed, backend, active,
                )
            self._json(200, {
                "status": "ok",
                "volume": vol,
                "active": active,
                "backend": backend if active else None,
                # Kept for backward compatibility with older UI builds.
                "mpv": active and backend == "mpv",
                "position": pb["position"],
                "duration": pb["duration"],
                "paused": pb["paused"],
                # Scrub bar only when the position is live/seekable (mpv or
                # CDP); the MPRIS fallback reports a frozen 0 for YouTube.
                "seekable": active and pb.get("seekable", False),
                "title": pb["title"] if active else None,
            })
            return
        else:
            self._json(400, {"error": f"unknown action: {action!r}"})
            return

        LOG.info("control action=%s rc=%s detail=%s", action, rc, out)
        if rc == 404:
            self._json(404, {"error": "no active cast", "detail": out})
            return
        if rc != 0:
            self._json(502, {"error": f"{action} failed", "detail": out})
            return
        self._json(200, {"status": "ok", "detail": out})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/control":
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            self._do_control(self._read_body())
            return
        if self.path == "/screensaver":
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            theme = self._read_body().get("theme", "")
            rc, backend, out = _start_screensaver(theme)
            if rc == 400:
                self._json(400, {"error": out})
                return
            if rc != 0:
                self._json(502, {"error": f"{backend} failed", "rc": rc, "stderr": out})
                return
            self._json(200, {"status": "ok", "screensaver": theme})
            return
        if self.path == "/screen-off":
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            # Stop whatever's playing (mpv/Firefox alike) before actually
            # blanking the display — otherwise audio keeps going behind a
            # dark screen. wake_display()'s DPMS counterpart is what
            # /open and /screensaver already call, so turning back on is
            # just "cast or pick a screensaver" — no separate "screen on"
            # endpoint needed.
            stopped = stop_active_playback()
            rc, out = _run(["xset", "dpms", "force", "off"])
            with _state_lock:
                _state["screen_off"] = True
                _state["screensaver"] = None
            LOG.info("screen off (stopped=[%s] xset rc=%s out=%r)", stopped, rc, out)
            self._json(200, {"status": "ok"})
            return
        if self.path == "/poweroff":
            if not _check_auth(self.headers.get("Authorization")):
                LOG.warning("auth failed from %s", self.client_address[0])
                self._json(401, {"error": "bad token"})
                return
            # WARNING (not INFO) — this is the single most consequential
            # action the portal can trigger, worth standing out in the
            # journal as an audit trail on its own.
            LOG.warning("POWEROFF requested from %s — shutting the host down",
                        self.client_address[0])
            # Answer before actually shutting down: the HTTP response has
            # to make it out (through this container, to the browser)
            # before the machine starts going away, so the poweroff
            # itself runs on a short delay in the background rather than
            # inline before we reply.
            self._json(200, {"status": "ok", "detail": "powering off"})

            def _do_poweroff() -> None:
                time.sleep(1.5)
                # -n: fail fast instead of hanging if the sudoers grant
                # is somehow missing, rather than silently blocking
                # forever with no way to observe why nothing happened.
                rc, out = _run(["sudo", "-n", POWEROFF_BIN], timeout=30)
                if rc != 0:
                    LOG.error("poweroff command failed rc=%s: %s", rc, out)

            threading.Thread(target=_do_poweroff, name="poweroff", daemon=True).start()
            return
        if self.path != "/open":
            self._json(404, {"error": "not found"})
            return
        if not _check_auth(self.headers.get("Authorization")):
            LOG.warning("auth failed from %s", self.client_address[0])
            self._json(401, {"error": "bad token"})
            return
        body = self._read_body()
        url = body.get("url", "")
        if not _check_url(url):
            self._json(400, {"error": "bad url"})
            return
        # The caller can force the mpv-direct backend for a URL it has
        # already resolved to a playable media stream (e.g. the
        # container's Jellyfin integration hands us an HLS/stream URL).
        # Such URLs aren't on a known video host and would otherwise
        # fall through to Firefox.
        want_backend = body.get("backend", "")
        # Optional display title from the caller (e.g. the container's
        # Jellyfin integration passes the movie/episode name). YouTube's
        # title is fetched here by yt-dlp instead; Firefox casts have none.
        want_title = (body.get("title") or "").strip() or None

        rc, backend, out = _cast(url, want_backend=want_backend, want_title=want_title)
        # A real cast always wins over whatever screensaver/screen-off
        # state was active — the whole point of casting is to take over
        # the projector.
        with _state_lock:
            _state["screensaver"] = None
            _state["screen_off"] = False
        if rc != 0:
            self._json(502, {"error": f"{backend} failed", "rc": rc, "stderr": out})
            return
        self._json(200, {"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MEDIACAST_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not TOKEN:
        raise SystemExit("MEDIACAST_TOKEN unset — refusing to start with an open endpoint")
    # The systemd unit sets DISPLAY/XAUTHORITY but not the session-bus /
    # runtime-dir vars. playerctl (MPRIS over the user D-Bus) and pactl
    # need them, so fill in the standard per-user defaults if unset. mpv
    # already injects its own copies at spawn; this just covers the
    # subprocess helpers that inherit our environment.
    uid = os.getuid()
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"
    )
    # Local YouTube anti-throttle proxy (loopback only). Started before
    # the control server so the first cast can already use it.
    start_proxy()
    # Idle screensaver watchdog — runs independent of the HTTP server so
    # it fires even with no one polling the portal.
    threading.Thread(target=_screensaver_watchdog, name="screensaver", daemon=True).start()
    # 0.0.0.0 so the container reaches us via host.docker.internal.
    # Token is the trust boundary. See module docstring.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # The portal polls /control (action=status) roughly once a second;
    # each poll is its own thread. If one blocks (e.g. a wedged CDP call
    # to Firefox — see _cdp_eval), non-daemon threads would pile up and
    # also block a clean restart. Same reasoning as start_proxy() above.
    server.daemon_threads = True
    LOG.info("mediacast-host listening on :%d (DISPLAY=%s)", PORT, os.environ.get("DISPLAY", "?"))
    server.serve_forever()


if __name__ == "__main__":
    main()
