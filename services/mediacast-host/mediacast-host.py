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
import socket
import struct
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
# How often to check for active playback and, if found, reset X's idle
# counter so the DPMS/blank timeout (bootstrap.sh step 6, input-idle only)
# doesn't trip mid-playback. Independent of the screensaver feature above —
# runs even with SCREENSAVER_IDLE_TIMEOUT=0.
# X idle blank/DPMS timeout re-applied on every wake (see wake_display);
# matches the `xset s/dpms 3600` in bootstrap's .xprofile. 0 = leave alone.
IDLE_BLANK_S = float(os.environ.get("MEDIACAST_IDLE_BLANK_S", "3600"))
KEEPAWAKE_POLL_INTERVAL = float(os.environ.get("MEDIACAST_KEEPAWAKE_POLL_INTERVAL", "30"))


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
    # `xset dpms force off` (sleep / screen-off) silently *enables* X DPMS
    # with whatever timeouts are loaded (xfce leaves 600 s), which then
    # blanks the projector after 10 idle minutes. Re-assert the intended
    # input-idle policy (bootstrap's .xprofile: 1 h) on every wake.
    if IDLE_BLANK_S > 0:
        n = str(int(IDLE_BLANK_S))
        _run(["xset", "s", n, n])
        _run(["xset", "dpms", n, n, n])
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
    start: float | None = None,
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
        # Start at the portal's last-set volume, not mpv's own 100%
        # default — see _desired_volume. A freshly-spawned process here
        # (every cast kills the old one and starts a new one) has nothing
        # to restore from otherwise. Muted instead while a screensaver is
        # showing — see _effective_volume.
        f"--volume={_effective_volume()}",
    ]
    if start and start > 0:
        # Resume offset (Jellyfin "continue watching"). mpv seeks before
        # the first frame; for Jellyfin HLS transcodes the server restarts
        # the transcode at the requested segment, same as jellyfin-web.
        args.append(f"--start={float(start):.1f}")
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
# _cdp_eval used to open a brand-new WebSocket connection (+ a fresh
# Runtime.enable handshake) per call. Two threads doing that at once — the
# portal's ~1Hz status poll and youtube_go_fullscreen's own ~3Hz settle-loop
# polling right after a cast — measurably contended with each other
# (observed: normally ~0.1s calls stretching to 1.5-2s during that overlap,
# and up to 20s+ once the settle loop's dozens of reconnect-and-enable
# round trips piled up behind the lock — this is the "cast takes forever
# to load/fullscreen" slowness). Serializing all CDP access behind one
# lock stops two connections fighting over Firefox's single debugger
# session; keeping one persistent connection per tab (_cdp_conns) on top
# of that removes the reconnect+enable cost from every call after the
# first, so a lock holder's turn is a single send/recv instead of a full
# handshake.
_cdp_lock = threading.Lock()
# ws_url -> {"sock": socket, "next_id": int}. Torn down whenever we close
# tabs (the cached tab may no longer exist) — see _cdp_reset_conns.
_cdp_conns: dict[str, dict] = {}


def _mc_lock_volume_js(v_expr: str, desired_expr: str) -> str:
    """JS: pin `v_expr`'s real volume to `desired_expr` (0-100) and make it
    stick — override the HTMLMediaElement volume accessor on this specific
    element so any *later* write to .volume, from anyone (YouTube's own
    player re-applying the logged-in account's saved volume, an ad module,
    a fullscreen/quality-change reinit — whatever it turns out to be) is
    silently redirected back to our value instead of taking effect. Only
    `_mc_set_volume_js` (below) can actually move it after that.

    649c8fd's fix only ever set .volume once, when a <video> element first
    appeared, on the theory that a fresh 100%-default element was the only
    way this goes wrong. It wasn't: something was still overwriting the
    *same* element's volume a minute or two into playback, well after
    __mcInit had already fired, so that one-shot set went uncorrected. This
    closes that off structurally — nothing can push an unwanted volume
    through in the first place — instead of racing to poll for drift after
    the fact.

    Idempotent (v.__mcDesc marks "already locked"), so it's cheap to call
    on every poll and only does anything the first time it sees a given
    element.
    """
    return (
        f"if(!{v_expr}.__mcDesc){{"
        f"const d=Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype,'volume');"
        f"{v_expr}.__mcDesc=d;"
        f"{v_expr}.__mcVol=Math.max(0,Math.min(1,({desired_expr})/100));"
        f"d.set.call({v_expr},{v_expr}.__mcVol);"
        f"Object.defineProperty({v_expr},'volume',{{configurable:true,"
        f"get(){{return d.get.call({v_expr});}},"
        f"set(x){{d.set.call({v_expr},{v_expr}.__mcVol);}}}});"
        f"}}"
    )


def _mc_set_volume_js(v_expr: str, pct_expr: str) -> str:
    """JS: actually change `v_expr`'s volume to `pct_expr` (0-100) — the
    only way to move it once _mc_lock_volume_js has locked the element;
    falls back to a plain assignment if the lock isn't installed yet
    (e.g. a video element CDP hasn't polled at all since it appeared)."""
    return (
        f"if({v_expr}.__mcDesc){{"
        f"{v_expr}.__mcVol=Math.max(0,Math.min(1,({pct_expr})/100));"
        f"{v_expr}.__mcDesc.set.call({v_expr},{v_expr}.__mcVol);"
        f"}}else{{{v_expr}.volume=Math.max(0,Math.min(1,({pct_expr})/100));}}"
    )


def _cdp_state_js() -> str:
    """JS for the ~1Hz status poll: currentTime/duration/paused + a cleaned
    title in one round-trip. Also self-heals a *fresh* <video> element —
    not just the one our own cast flow (youtube_go_fullscreen) creates,
    but one YouTube's own "autoplay next video" swaps in on its own, with
    no /open call and no CDP hook of ours involved — by zeroing its
    currentTime once (__mcInit) and locking its volume (see
    _mc_lock_volume_js) so nothing can push it back to the 100% default
    afterwards, from this element's creation to the end of its life.
    """
    return (
        "(()=>{const v=document.querySelector('video');if(!v)return null;"
        "if(!v.__mcInit){v.__mcInit=1;v.currentTime=0;}"
        f"{_mc_lock_volume_js('v', str(_effective_volume()))}"
        # YouTube's player API knows the *current* video (it changes under
        # us on autoplay-next) and its channel name.
        "let vd=null;try{const p=document.querySelector('#movie_player');"
        "vd=p&&p.getVideoData?p.getVideoData():null}catch(e){}"
        "return {t:v.currentTime,d:v.duration,p:v.paused,e:v.ended,"
        "vid:(vd&&vd.video_id)||null,author:(vd&&vd.author)||null,"
        # Strip YouTube's "(3) " unread-count prefix and " - YouTube" suffix.
        "title:(document.title||'').replace(/^\\(\\d+\\)\\s*/,'').replace(/ - YouTube$/,'')}"
        "})()"
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
    if closed:
        _cdp_reset_conns()
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
    if closed:
        _cdp_reset_conns()
    return closed


def _cdp_ws_send(s: socket.socket, text: str) -> None:
    payload = text.encode()
    mask = os.urandom(4)
    n = len(payload)
    hdr = bytearray([0x81])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
    hdr += mask
    s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _cdp_ws_recv(s: socket.socket) -> dict:
    def rx(n: int) -> bytes:
        data = b""
        while len(data) < n:
            part = s.recv(n - len(data))
            if not part:
                raise OSError("ws closed")
            data += part
        return data

    h = rx(2)
    opcode = h[0] & 0x0f
    n = h[1] & 0x7f
    if n == 126:
        n = struct.unpack(">H", rx(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rx(8))[0]
    body = rx(n) if n else b""
    if opcode == 0x8:
        raise OSError("ws closed (close frame)")
    return json.loads(body.decode("utf-8", "replace")) if body else {}


def _cdp_connect(ws_url: str, timeout: float) -> dict | None:
    """Open a new CDP WebSocket to ws_url and enable the Runtime domain.

    Returns {"sock", "next_id"} cached by the caller in _cdp_conns and
    reused across calls — see _cdp_eval for why reconnecting+re-enabling
    on every call was the main source of cast/poll latency.
    """
    u = urlparse(ws_url)
    s = socket.create_connection((u.hostname, u.port), timeout=timeout)
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
            s.close()
            return None
        buf += chunk
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        s.close()
        return None
    # Firefox CDP needs Runtime.enable to create the page's execution
    # context before evaluate works ("context is null" otherwise). Wait
    # for the executionContextCreated event, then evaluate in it. Only
    # paid once per connection now, not once per call.
    _cdp_ws_send(s, json.dumps({"id": 1, "method": "Runtime.enable"}))
    for _ in range(25):
        msg = _cdp_ws_recv(s)
        if msg.get("method") == "Runtime.executionContextCreated":
            break
    return {"sock": s, "next_id": 1}


def _cdp_evaluate(entry: dict, expr: str) -> object:
    entry["next_id"] += 1
    mid = entry["next_id"]
    _cdp_ws_send(entry["sock"], json.dumps({
        "id": mid, "method": "Runtime.evaluate",
        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
    }))
    for _ in range(25):
        msg = _cdp_ws_recv(entry["sock"])
        if msg.get("id") == mid:
            return (msg.get("result", {}).get("result", {}) or {}).get("value")
    return None


def _cdp_reset_conns() -> None:
    """Drop all cached CDP connections. Call whenever tabs are closed —
    a cached connection for a now-closed tab is dead weight (and its
    socket eventually errors anyway), so this just reclaims it eagerly."""
    with _cdp_lock:
        for entry in _cdp_conns.values():
            try:
                entry["sock"].close()
            except OSError:
                pass
        _cdp_conns.clear()


def _cdp_eval(expr: str, timeout: float = 3.0, prefer: str | None = None):
    """Runtime.evaluate `expr` in the cast Firefox page; return its value.

    Keeps one persistent WebSocket per tab (_cdp_conns), reused across
    calls — the portal's ~1Hz status poll and youtube_go_fullscreen's
    settle-loop polling both hit this, and re-handshaking (TCP connect +
    WS upgrade + Runtime.enable) on every single call was the main cost
    behind multi-second "status poll slow" stalls and slow-feeling casts.
    returnByValue gives JSON back. Returns None on any failure (no CDP,
    no <video>, JS error, dead connection after one reconnect attempt).

    `prefer` narrows the target to a specific tab (see _cdp_target_ws) —
    None here just means "no matching tab (yet)", not an error, so it's
    the quiet early-return above, not logged.
    """
    ws_url = _cdp_target_ws(prefer)
    if not ws_url:
        return None
    # Serialized (see _cdp_lock) so this is always the only CDP call in
    # flight — a status poll now waits a beat instead of racing a second
    # connection against Firefox's debugger session.
    with _cdp_lock:
        entry = _cdp_conns.get(ws_url)
        for attempt in range(2):
            if entry is None:
                try:
                    entry = _cdp_connect(ws_url, timeout)
                except (OSError, ValueError) as exc:
                    LOG.warning("cdp connect failed (%s): %s", type(exc).__name__, exc)
                    return None
                if entry is None:
                    return None
                _cdp_conns[ws_url] = entry
            entry["sock"].settimeout(timeout)
            try:
                return _cdp_evaluate(entry, expr)
            except (OSError, ValueError, KeyError, IndexError) as exc:
                # The cached connection is dead (tab closed/navigated,
                # Firefox's CDP port wedged) — drop it and retry once
                # with a fresh connection before giving up.
                if attempt == 0:
                    LOG.debug("cdp conn stale (%s): %s — reconnecting", type(exc).__name__, exc)
                else:
                    LOG.warning("cdp eval failed (%s): %s", type(exc).__name__, exc)
                try:
                    entry["sock"].close()
                except OSError:
                    pass
                _cdp_conns.pop(ws_url, None)
                entry = None
        return None


def firefox_cdp_state() -> dict | None:
    """{position, duration, paused, title} from the YouTube <video> via CDP,
    or None when CDP/the video isn't available (caller falls back to MPRIS)."""
    v = _cdp_eval(_cdp_state_js())
    if not isinstance(v, dict):
        return None
    pos, dur = v.get("t"), v.get("d")
    return {
        "position": pos if isinstance(pos, (int, float)) else None,
        "duration": dur if isinstance(dur, (int, float)) and dur > 0 else None,
        "paused": bool(v.get("p")),
        "title": v.get("title") or None,
        "ended": bool(v.get("e")),
        "video_id": v.get("vid") or None,
        "author": v.get("author") or None,
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


def _cdp_cast_tab() -> dict | None:
    """The CDP page target that holds the current cast: the tab showing
    the tracked media's YouTube video if we know it, else any YouTube
    tab, else the only/first page. None if CDP is unreachable."""
    pages = [t for t in _cdp_pages() if t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    with _state_lock:
        media = dict(_state["media"] or {})
    vid = media.get("vid") or ""
    murl = media.get("url") or ""
    for t in pages:
        u = t.get("url") or ""
        if (vid and vid in u) or (murl and u == murl):
            return t
    yt = next((t for t in pages if "youtube.com" in (t.get("url") or "")), None)
    return yt or pages[0]


# Blank the cast tab in place: stop every media element for good (src
# dropped, so YouTube can't auto-resume or ad-roll) and replace the page
# with a black, empty document so the fullscreen kiosk shows black, not a
# white about:blank or a leftover YouTube end screen.
_BLANK_TAB_JS = (
    "(()=>{document.querySelectorAll('video,audio').forEach(m=>{"
    "try{m.pause();m.removeAttribute('src');m.load();}catch(e){}});"
    "document.documentElement.innerHTML='<head><title>mediacast</title></head>"
    "<body style=\"background:#000;margin:0\"></body>';return true})()"
)


def control_stop() -> tuple[int, str]:
    """Stop the cast. mpv: quit cleanly (socket gets unlinked). Firefox:
    act on the *cast tab* through CDP — close it when other pages exist,
    otherwise blank it in place (closing the last tab would take the
    whole kiosk window down and flap mediacast-firefox.service). This used
    to send Ctrl+W via xdotool to whichever tab had focus, which could
    close the wrong one."""
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
    tab = _cdp_cast_tab()
    if tab is None:
        # CDP down: last resort, pause over MPRIS so at least audio stops.
        prc, pout = _playerctl("pause")
        return (0, "mpris pause (cdp unavailable)") if prc == 0 else (404, f"no active cast ({pout})")
    pages = _cdp_pages()
    if len(pages) > 1:
        try:
            urllib.request.urlopen(f"{CDP_BASE}/json/close/{tab['id']}", timeout=2).read()
            _cdp_reset_conns()
            return 0, "firefox cast tab closed (cdp)"
        except (OSError, ValueError, KeyError) as exc:
            LOG.warning("cdp close of cast tab failed: %s — blanking instead", exc)
    _cdp_eval(_BLANK_TAB_JS, prefer=tab.get("url") or None)
    return 0, "firefox cast tab blanked (cdp)"


def control_set_pause(paused: bool) -> tuple[int, str]:
    """Explicit play / pause (not a toggle) for the v1 API."""
    rc, out = _mpv_command(["set_property", "pause", bool(paused)])
    if rc != 404:
        return rc, f"mpv pause={paused}: {out}"
    js = ("(()=>{const v=document.querySelector('video');if(!v)return null;"
          + ("v.pause();" if paused else "v.play().catch(()=>{});")
          + "return true})()")
    if _cdp_eval(js) is True:
        return 0, f"cdp {'pause' if paused else 'play'}"
    prc, pout = _playerctl("pause" if paused else "play")
    if prc == 0:
        return 0, f"mpris {'pause' if paused else 'play'}"
    return 404, f"no active cast ({pout})"


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


# Portal-driven cast volume, applied at the *player* level instead of
# PulseAudio: mpv's own "volume" property for mpv casts, the <video>
# element's .volume for browser YouTube casts. This intentionally never
# touches PulseAudio sink/sink-input volume, for two reasons:
#   - @DEFAULT_SINK@ is shared with Spotify (librespot) and the turntable
#     loopback (see docker-compose.yml) — moving it from here drags them
#     down with the video-cast audio, which once made Spotify go quiet
#     after a reboot.
#   - Targeting the *sink-input*'s own volume (the previous fix for that)
#     only survives as long as that particular stream lives. Every cast
#     kills the old stream and starts a brand new one (a fresh mpv
#     process, or a fresh Firefox tab/<video> via --new-tab) which starts
#     back at its own 100% default — PulseAudio's module-stream-restore
#     did not reliably catch this in practice, so every single new video
#     played at full volume until manually pulled back down. Setting the
#     player's own volume — passed to mpv at spawn, and (re-)applied to
#     the new <video> element as soon as it exists, see
#     youtube_go_fullscreen — means a fresh stream starts at the right
#     level with nothing to restore.
#   - Setting the <video>'s .volume once, on creation, wasn't the end of
#     it either: something (most likely YouTube's own player re-applying
#     the logged-in account's saved volume — we're forced onto a logged-in
#     account to dodge the bot wall, see resolve_youtube) kept overwriting
#     it a minute or two into playback, well after that one-shot set had
#     already run. See _mc_lock_volume_js for the actual fix: the element's
#     volume accessor is overridden so no later write, from anyone, can
#     take effect except through our own _mc_set_volume_js.
_desired_volume: int = max(0, min(100, int(os.environ.get("MEDIACAST_DEFAULT_VOLUME", "60"))))


def _mpv_get_property(prop: str, rid: int = 90) -> float | None:
    """Like the `get()` helper in mpv_playback_state, standalone: send one
    get_property and match the reply by request_id so an interleaved mpv
    event can't be mistaken for the answer."""
    if not os.path.exists(MPV_IPC_SOCK):
        return None
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2.0)
        s.connect(MPV_IPC_SOCK)
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
                    data = msg.get("data")
                    if msg.get("error") == "success" and isinstance(data, (int, float)):
                        return float(data)
                    return None
    except OSError:
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _cdp_set_volume(pct: int) -> bool:
    v = _cdp_eval(
        "(()=>{const v=document.querySelector('video');if(!v)return null;"
        f"{_mc_set_volume_js('v', str(pct))}"
        "return Math.round(v.volume*100)})()"
    )
    return isinstance(v, (int, float))


def _cdp_get_volume() -> int | None:
    v = _cdp_eval(
        "(()=>{const v=document.querySelector('video');"
        "return v?Math.round(v.volume*100):null})()"
    )
    return int(v) if isinstance(v, (int, float)) else None


def control_volume(*, absolute: int | None = None, delta: int | None = None) -> tuple[int, str]:
    """Set or nudge the portal's remembered cast volume, applying it live
    to whichever backend is currently playing (see _desired_volume)."""
    global _desired_volume
    if absolute is not None:
        _desired_volume = max(0, min(100, int(absolute)))
    elif delta is not None:
        _desired_volume = max(0, min(100, _desired_volume + int(delta)))
    else:
        return 1, "neither set nor delta provided"
    rc, out = _mpv_command(["set_property", "volume", _desired_volume])
    if rc == 0:
        return 0, f"mpv volume -> {_desired_volume}%"
    if rc != 404:
        return rc, f"mpv: {out}"
    if _cdp_set_volume(_desired_volume):
        return 0, f"cdp volume -> {_desired_volume}%"
    # Nothing currently playing to apply it to — still record the choice
    # (see _desired_volume) so the next cast starts at this level.
    return 1, "no active cast stream (remembered for next cast)"


def control_volume_get() -> tuple[int, int]:
    v = _mpv_get_property("volume")
    if v is not None:
        return 0, int(round(v))
    v = _cdp_get_volume()
    if v is not None:
        return 0, v
    # Idle screen — report the last value the slider set rather than an
    # error, so the portal's slider reflects what the *next* cast will
    # play at instead of going blank.
    return 0, _desired_volume


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
    # window activation involved — this can't hit the wrong tab. The
    # first time we see the <video> element, also force currentTime to 0
    # and apply the portal's last-set volume: a logged-in YouTube account
    # (required to dodge the bot wall — see resolve_youtube) auto-resumes
    # a previously-watched video's saved position, and a fresh <video>
    # element always starts at its own 100% rather than remembering the
    # slider, so without this every cast can start mid-video and at full
    # volume. __mcInit guards it to once per element so a later poll here
    # doesn't keep yanking playback back to 0.
    for _ in range(8):
        if player_state() in (1, 3):
            break
        _cdp_eval(
            "(()=>{const v=document.querySelector('video');if(!v)return null;"
            "if(!v.__mcInit){v.__mcInit=1;v.currentTime=0;}"
            f"{_mc_lock_volume_js('v', str(_effective_volume()))}"
            "v.play().catch(()=>{});return null})()",
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
# Cast dispatch + unified projector state (power / screen / media)
# ---------------------------------------------------------------------------
# This helper owns the one authoritative projector state. Request threads
# (/open, /screensaver, /power, /control) mutate _state; one poller thread
# (_state_poller) reads playback position from mpv / Firefox CDP once a
# second, watches for physical wake while sleeping, and publishes a
# snapshot. The container long-polls GET /v1/state and fans it out to
# clients over SSE (see services/mediacast/app.py, docs/api-v1.md).

# Soft sleep automatically once the screensaver has been running this
# long (minutes; 0 disables). Without it, the screensaver + keep-awake
# watchdog kept the projector lit forever.
SLEEP_AFTER_SCREENSAVER_MIN = float(os.environ.get("SLEEP_AFTER_SCREENSAVER_MIN", "180"))
STATE_POLL_INTERVAL = float(os.environ.get("MEDIACAST_STATE_POLL_INTERVAL", "1.0"))
# While sleeping / screen-off: how often to look for the projector being
# (re)connected via `xrandr --current` (cheap, no re-probe).
HOTPLUG_POLL_INTERVAL = float(os.environ.get("MEDIACAST_HOTPLUG_POLL_INTERVAL", "3.0"))
# A cast with no readable playback for this long (after this many
# consecutive empty polls) is considered over.
MEDIA_GRACE_S = 30.0
MEDIA_MISSING_POLLS = 5
# Don't restart an ended screensaver video more often than this.
SCREENSAVER_RESTART_GAP_S = 300.0

_state_lock = threading.Lock()
_state: dict = {
    "screensaver": None,       # theme key currently showing, or None
    "screensaver_since": 0.0,  # when it started (auto-sleep timer)
    "screen_off": False,       # display explicitly turned off via screen_off
    "screen_off_at": 0.0,
    "power": "on",             # "on" | "sleeping" (soft sleep)
    "sleep_at": 0.0,
    "woke_at": time.time(),    # last wake → restarts the idle-screensaver timer
    # What we put on the projector (None = nothing). Keys: source, url
    # (internal only — may carry a Jellyfin api_key, never published),
    # vid, item, title, subtitle, artwork_url, backend, started, missing.
    "media": None,
}
# Last playback reading, owned by the poller. "for" ties it to the media
# (its "started" stamp) it was read for, so a stale reading never leaks
# onto a newer cast.
_pb: dict = {}
_xstate: dict = {"dpms_off": None}


def _effective_volume() -> int:
    """The volume applied to the next/current player element.

    Silent by default while a screensaver is showing — it's ambient
    wallpaper video, not something anyone asked to hear — otherwise the
    portal's remembered cast volume (see _desired_volume). The slider
    still works during a screensaver: control_volume's direct set /
    _cdp_set_volume calls bypass this and move the real element volume;
    this only governs the level a *freshly appearing* video element
    gets locked to."""
    with _state_lock:
        if _state["screensaver"]:
            return 0
    return _desired_volume


def _yt_artwork(vid: str | None) -> str | None:
    # Served (and cached) by the container; no auth needed.
    return f"/api/v1/artwork/youtube/{vid}" if vid else None


def _media_meta(url: str, meta: dict | None) -> dict:
    """Build the tracked-media record for a cast. `meta` (from the
    container) can carry source/item/title/subtitle/artwork_url; anything
    missing is derived from the URL."""
    meta = meta if isinstance(meta, dict) else {}
    vid = _yt_video_id(url) if is_youtube_host(url) else ""
    item = meta.get("item")
    if not (isinstance(item, dict) and item.get("kind") and item.get("id")):
        item = {"kind": "youtube", "id": vid} if vid else {"kind": "url", "id": url}
    return {
        "source": meta.get("source") or ("youtube" if vid else "url"),
        "url": url,
        "vid": vid,
        "item": item,
        "title": meta.get("title") or None,
        "subtitle": meta.get("subtitle") or (None if vid else urlparse(url).netloc or None),
        "artwork_url": meta.get("artwork_url") or _yt_artwork(vid),
        "backend": "",
        "started": time.time(),
        "missing": 0,
    }


def _cast(url: str, want_backend: str = "", want_title: str | None = None,
          start: float = 0.0) -> tuple[int, str, str]:
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
        rc, out = open_in_mpv(url, use_ytdl=False, title=want_title, start=start)
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


# ---------------------------------------------------------------------------
# X11 input-idle + DPMS readout (wake detection)
# ---------------------------------------------------------------------------
# Physical wake = the X server's input-idle counter going *down* (any real
# mouse/keyboard event resets it). Read through the MIT-SCREEN-SAVER
# extension (libXss, same thing `xprintidle` uses) via ctypes — stdlib
# only. Verified on the box: `xset dpms force off/on` do NOT reset the
# counter, `xdotool mousemove_relative` (XTEST) does, and `xset s reset`
# does (so the keep-awake watchdog must stay quiet while dormant — it does).
# DPMSInfo (libXext) tells us whether the display is actually powered
# down, which also catches X's own idle blanking and anyone else turning it
# back on. Only ever used from the poller thread (Xlib isn't thread-safe
# without XInitThreads). Caveat: Xlib exit()s the process on a lost X
# connection; systemd's Restart= brings us back.

import ctypes  # noqa: E402  (kept next to its only user)


class _XssInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong), ("state", ctypes.c_int),
        ("kind", ctypes.c_int), ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong),
    ]


class _XMonitor:
    def __init__(self) -> None:
        self.ok = False
        self.d = None
        try:
            self.x11 = ctypes.CDLL("libX11.so.6")
            self.xss = ctypes.CDLL("libXss.so.1")
            self.xext = ctypes.CDLL("libXext.so.6")
        except OSError as exc:
            LOG.warning("wake detection: X libs unavailable (%s)", exc)
            return
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XssInfo)
        self.xss.XScreenSaverQueryInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XssInfo)]
        self.xext.DPMSInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ubyte)]
        self.ok = True

    def _ensure(self) -> bool:
        if not self.ok:
            return False
        if self.d is None:
            d = self.x11.XOpenDisplay(None)
            if not d:
                return False
            self.d = d
            self.root = self.x11.XDefaultRootWindow(d)
            self.info = self.xss.XScreenSaverAllocInfo()
        return True

    def idle_ms(self) -> int | None:
        if not self._ensure():
            return None
        if not self.xss.XScreenSaverQueryInfo(self.d, self.root, self.info):
            return None
        return int(self.info.contents.idle)

    def dpms_off(self) -> bool | None:
        if not self._ensure():
            return None
        level, enabled = ctypes.c_ushort(0), ctypes.c_ubyte(0)
        if not self.xext.DPMSInfo(self.d, ctypes.byref(level), ctypes.byref(enabled)):
            return None
        # level 0 = DPMSModeOn; standby/suspend/off all mean "dark".
        return bool(enabled.value) and level.value != 0


def _connected_outputs() -> set[str] | None:
    """RandR outputs currently reported connected (no re-probe:
    --current just reads the X server's state, which the NVIDIA driver
    updates on an HDMI hotplug interrupt)."""
    rc, out = _run(["xrandr", "--current"], timeout=5)
    if rc != 0:
        return None
    return {m.group(1) for m in re.finditer(r"^(\S+) connected", out, re.M)}


# ---------------------------------------------------------------------------
# Snapshot + publish (long-poll for the container)
# ---------------------------------------------------------------------------

_pub_cv = threading.Condition()
_pub: dict = {"version": 0, "state": None, "ts": 0}


def _pb_live(pb: dict) -> bool:
    """A reading that means something is actually loaded and not finished."""
    if pb.get("position") is None or pb.get("ended"):
        return False
    dur, pos = pb.get("duration"), pb.get("position")
    # A paused element sitting at its very end (YouTube end screen).
    if pb.get("paused") and dur and pos is not None and pos >= dur - 0.5:
        return False
    return True


def _round(v):
    return round(float(v), 1) if isinstance(v, (int, float)) else None


def _build_snapshot() -> dict:
    with _state_lock:
        st = dict(_state)
        media = dict(st["media"]) if st["media"] else None
    pb = dict(_pb)
    sleeping = st["power"] == "sleeping"
    ss = st["screensaver"]
    dark = sleeping or st["screen_off"] or bool(_xstate.get("dpms_off"))
    snap = {
        "power": "sleeping" if sleeping else "on",
        "screen": "off" if dark else "on",
        "activity": "idle", "source": None, "title": None, "subtitle": None,
        "artwork_url": None, "position_s": None, "duration_s": None,
        "volume": _desired_volume, "muted": bool(ss), "item": None,
        "screensaver": {
            "active": bool(ss),
            "theme": ss,
            "themes": [{"id": k, "name": label} for k, (label, _u) in SCREENSAVER_THEMES.items()],
            "idle_minutes": round(SCREENSAVER_IDLE_TIMEOUT / 60, 1),
        },
    }
    live = bool(media) and pb.get("for") == media.get("started") and _pb_live(pb)
    if ss and not sleeping:
        label, url = SCREENSAVER_THEMES.get(ss, (ss, ""))
        snap.update(activity="screensaver", source="screensaver",
                    title=(pb.get("title") if live else None) or label,
                    subtitle="Screensaver",
                    artwork_url=_yt_artwork(_yt_video_id(url)))
    elif media and not sleeping:
        snap.update(
            activity="paused" if (live and pb.get("paused")) else "playing",
            source=media["source"],
            title=media.get("title") or (pb.get("title") if live else None)
            or (urlparse(media["url"]).netloc if media["source"] == "url" else None),
            subtitle=media.get("subtitle"),
            artwork_url=media.get("artwork_url"),
            item=media.get("item"),
        )
        if media["source"] == "youtube" and live:
            # Live page data wins (autoplay-next swaps the video under us).
            snap["title"] = pb.get("title") or snap["title"]
            snap["subtitle"] = pb.get("author") or snap["subtitle"]
    if live and snap["activity"] != "idle":
        snap["position_s"] = _round(pb.get("position"))
        snap["duration_s"] = _round(pb.get("duration"))
    return snap


def _publish() -> None:
    snap = _build_snapshot()
    with _pub_cv:
        if snap != _pub["state"]:
            _pub["version"] += 1
            _pub["state"] = snap
            _pub["ts"] = int(time.time() * 1000)
            _pub_cv.notify_all()


def _wait_state(since: int, timeout: float) -> dict:
    with _pub_cv:
        _pub_cv.wait_for(lambda: _pub["version"] != since and _pub["state"] is not None,
                         timeout=timeout)
        return {"version": _pub["version"],
                "state": {**(_pub["state"] or {}), "updated_at": _pub["ts"]}}


# ---------------------------------------------------------------------------
# Power: soft sleep / screen off / wake
# ---------------------------------------------------------------------------

def _clear_media_locked() -> None:
    _state["media"] = None
    _state["screensaver"] = None
    _pb.clear()


def do_sleep(reason: str) -> None:
    """Soft sleep: stop playback + screensaver, DPMS off, suspend the
    idle-screensaver watchdog until something wakes us."""
    stopped = stop_active_playback()
    with _state_lock:
        _clear_media_locked()
        _state["power"] = "sleeping"
        _state["sleep_at"] = time.time()
        _state["screen_off"] = False
    rc, out = _run(["xset", "dpms", "force", "off"])
    LOG.info("sleep reason=%s stopped=[%s] xset rc=%s %s", reason, stopped, rc, out)
    _publish()


def do_screen_off() -> None:
    """Legacy screen-off: stop playback, DPMS off. Cleared by physical
    input or any cast, like sleep — but power stays "on"."""
    stopped = stop_active_playback()
    rc, out = _run(["xset", "dpms", "force", "off"])
    with _state_lock:
        _clear_media_locked()
        _state["screen_off"] = True
        _state["screen_off_at"] = time.time()
    LOG.info("screen off (stopped=[%s] xset rc=%s out=%r)", stopped, rc, out)
    _publish()


def _mark_awake_locked() -> bool:
    was = _state["power"] == "sleeping" or _state["screen_off"]
    _state["power"] = "on"
    _state["screen_off"] = False
    _state["woke_at"] = time.time()
    return was


def do_wake(reason: str) -> None:
    with _state_lock:
        was = _mark_awake_locked()
    out = wake_display()
    LOG.info("wake reason=%s (was dormant=%s) %s", reason, was, out)
    _publish()


# ---------------------------------------------------------------------------
# Screensaver
# ---------------------------------------------------------------------------

def _start_screensaver(theme: str) -> tuple[int, str, str]:
    entry = SCREENSAVER_THEMES.get(theme)
    if not entry:
        return 400, "", f"unknown screensaver theme: {theme!r}"
    _, url = entry
    # Mark the screensaver active *before* casting so _effective_volume()
    # is already muted by the time the video element first appears —
    # otherwise the opening seconds would play at the portal's cast volume.
    with _state_lock:
        _mark_awake_locked()
        _clear_media_locked()
        _state["screensaver"] = theme
        _state["screensaver_since"] = time.time()
        media = _media_meta(url, {"source": "screensaver"})
        _state["media"] = media
    _publish()
    rc, backend, out = _cast(url)
    with _state_lock:
        if rc != 0:
            _clear_media_locked()
        elif _state["media"] is media:
            media["backend"] = backend
    LOG.info("screensaver theme=%s rc=%s backend=%s", theme, rc, backend)
    _publish()
    return rc, backend, out


def _stop_screensaver() -> tuple[int, str]:
    with _state_lock:
        active = _state["screensaver"]
    if not active:
        return 0, "no screensaver running"
    rc, out = control_stop()
    with _state_lock:
        _clear_media_locked()
    _publish()
    return (0 if rc in (0, 404) else rc), out


def _activity_live() -> bool:
    """Is something actually loaded on the projector right now (playing
    or paused)? From the poller's cached reading — no extra CDP/IPC."""
    with _state_lock:
        media = _state["media"]
        started = media["started"] if media else None
    return started is not None and _pb.get("for") == started and _pb_live(_pb)


def _keepawake_watchdog() -> None:
    """Background thread: reset X's idle counter every KEEPAWAKE_POLL_INTERVAL
    while something is actually playing.

    The X screensaver/DPMS idle timeout only tracks mouse/keyboard input —
    it has no idea a long video is running, so an unattended cast longer
    than the timeout blanks the projector mid-playback. `xset s reset`
    restarts that countdown without real input.

    Must stay silent while sleeping / screen-off: X treats `xset s reset`
    as activity (it un-blanks a DPMS-off display *and* resets the idle
    counter our physical-wake detection watches)."""
    while True:
        time.sleep(KEEPAWAKE_POLL_INTERVAL)
        with _state_lock:
            dormant = _state["screen_off"] or _state["power"] == "sleeping"
        if dormant:
            continue
        if _activity_live():
            _run(["xset", "s", "reset"])


def _screensaver_watchdog() -> None:
    """Background thread: auto-start the default screensaver once the
    projector's sat with nothing actually playing for
    SCREENSAVER_IDLE_TIMEOUT — independent of whether anyone has a client
    open. Never fires while sleeping / screen-off; the idle timer restarts
    on every wake. An ended screensaver is cleared by the poller (which
    restarts it), so this re-arms instead of getting stuck."""
    if SCREENSAVER_IDLE_TIMEOUT <= 0:
        return
    idle_since: float | None = None
    while True:
        time.sleep(SCREENSAVER_POLL_INTERVAL)
        with _state_lock:
            dormant = _state["screen_off"] or _state["power"] == "sleeping"
            already = _state["screensaver"]
            woke_at = _state["woke_at"]
        if dormant or already or _activity_live():
            idle_since = None
            continue
        now = time.time()
        if idle_since is None:
            idle_since = now
            continue
        idle_since = max(idle_since, woke_at)
        if now - idle_since >= SCREENSAVER_IDLE_TIMEOUT:
            LOG.info("screensaver: idle for %.0fs, auto-starting %r",
                     now - idle_since, SCREENSAVER_DEFAULT)
            _start_screensaver(SCREENSAVER_DEFAULT)
            idle_since = None


# ---------------------------------------------------------------------------
# The poller: playback readout, end-of-media, auto-sleep, wake detection
# ---------------------------------------------------------------------------

def _read_playback(media: dict | None) -> dict:
    """One playback reading: mpv first (if its socket exists), else the
    Firefox <video> over CDP. With media=None (discovery) only CDP — no
    MPRIS subprocess."""
    if os.path.exists(MPV_IPC_SOCK):
        pb = mpv_playback_state()
        if pb.get("position") is not None:
            return {**pb, "backend": "mpv"}
    if media is not None and (media.get("backend") or "").startswith("mpv"):
        return {}
    fb = firefox_cdp_state() if media is None else firefox_playback_state()
    if fb and fb.get("position") is not None:
        return {**fb, "backend": "firefox"}
    return {}


def _poll_media(now: float, state: dict) -> None:
    media = state["media"]
    if media is None:
        return
    pb = _read_playback(media)
    with _state_lock:
        cur = _state["media"]
        if cur is not media:   # replaced/cleared while we were reading
            return
        _pb.clear()
        _pb.update(pb)
        _pb["for"] = media["started"]
        if _pb_live(pb):
            media["missing"] = 0
            vid = pb.get("video_id")
            if media["source"] == "youtube" and vid and vid != media.get("vid"):
                # YouTube autoplay moved on to another video.
                media.update(vid=vid, item={"kind": "youtube", "id": vid},
                             artwork_url=_yt_artwork(vid), title=None, subtitle=None)
            return
        if media["source"] == "url" and not (media.get("backend") or "").startswith("mpv"):
            return  # a plain web page: nothing to read, stays until replaced
        if now - media["started"] < MEDIA_GRACE_S:
            return
        media["missing"] += 1
        if media["missing"] < MEDIA_MISSING_POLLS:
            return
        ended_ss = _state["screensaver"]
        _clear_media_locked()
    LOG.info("media ended (source=%s screensaver=%s)", media["source"], ended_ss)
    if ended_ss and now - _poller_mem.get("ss_restart", 0) > SCREENSAVER_RESTART_GAP_S:
        _poller_mem["ss_restart"] = now
        LOG.info("screensaver %r video ended — restarting it", ended_ss)
        threading.Thread(target=_start_screensaver, args=(ended_ss,),
                         name="ss-restart", daemon=True).start()


_poller_mem: dict = {}


def _discover(now: float) -> None:
    """Nothing tracked: notice playback we didn't start (e.g. mpv left
    running across a helper restart). Only *playing* media counts — a
    paused leftover tab is not a cast."""
    if now - _poller_mem.get("discover", 0) < 10:
        return
    _poller_mem["discover"] = now
    pb = _read_playback(None)
    if not _pb_live(pb) or pb.get("paused"):
        return
    vid = pb.get("video_id") or ""
    url = f"https://www.youtube.com/watch?v={vid}" if vid else "about:unknown"
    media = _media_meta(url, {"source": "youtube" if vid else "url"})
    media["backend"] = pb.get("backend", "")
    media["title"] = None if vid else pb.get("title")
    media["subtitle"] = None
    with _state_lock:
        if _state["media"] is None and not _state["screensaver"]:
            _state["media"] = media
            _pb.clear()
            _pb.update(pb)
            _pb["for"] = media["started"]
            LOG.info("discovered running playback (%s)", media["backend"])


def _poll_dormant(now: float, state: dict, xmon: _XMonitor, idle: int | None) -> None:
    sleeping = state["power"] == "sleeping"
    since = state["sleep_at"] if sleeping else state["screen_off_at"]
    prev = _poller_mem.get("idle")
    settled = now - since > 2.5
    if settled and idle is not None and prev is not None and idle + 400 < prev:
        do_wake("input")
        return
    if settled and _xstate.get("dpms_off") is False and now - since > 4:
        # Display came back on without us (X un-blanks on real input, or
        # someone ran `xset dpms force on`).
        do_wake("dpms-on")
        return
    if now - _poller_mem.get("hotplug_t", 0) >= HOTPLUG_POLL_INTERVAL:
        _poller_mem["hotplug_t"] = now
        outs = _connected_outputs()
        before = _poller_mem.get("outputs")
        if outs is not None:
            if before is not None and outs - before and settled:
                LOG.info("hotplug: outputs %s -> %s", sorted(before), sorted(outs))
                _poller_mem["outputs"] = outs
                do_wake("hotplug")
                return
            if before is not None and outs != before:
                LOG.info("hotplug: outputs %s -> %s", sorted(before), sorted(outs))
            _poller_mem["outputs"] = outs


def _state_poller() -> None:
    xmon = _XMonitor()
    while True:
        try:
            now = time.time()
            idle = xmon.idle_ms()
            _xstate["dpms_off"] = xmon.dpms_off()
            with _state_lock:
                state = {k: _state[k] for k in (
                    "power", "screen_off", "sleep_at", "screen_off_at",
                    "screensaver", "screensaver_since", "media")}
            dormant = state["power"] == "sleeping" or state["screen_off"]
            if dormant:
                _poll_dormant(now, state, xmon, idle)
            else:
                _poller_mem.pop("outputs", None)
                if state["media"] is not None:
                    _poll_media(now, state)
                elif not state["screensaver"]:
                    _discover(now)
                if (state["screensaver"] and SLEEP_AFTER_SCREENSAVER_MIN > 0
                        and now - state["screensaver_since"] >= SLEEP_AFTER_SCREENSAVER_MIN * 60):
                    do_sleep(f"screensaver ran {SLEEP_AFTER_SCREENSAVER_MIN:.0f} min")
            _poller_mem["idle"] = idle
            _publish()
        except Exception:  # noqa: BLE001 — the poller must never die
            LOG.exception("state poll failed")
        time.sleep(STATE_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

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

    def _authed(self) -> bool:
        if _check_auth(self.headers.get("Authorization")):
            return True
        LOG.warning("auth failed from %s", self.client_address[0])
        self._json(401, {"error": "bad token"})
        return False

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/health":
            self._json(200, {"ok": True})
        elif u.path == "/yt-subscriptions":
            # Authenticated: hands the container the account's subscribed
            # channel ids (scraped via yt-dlp + browser cookies) so it can
            # build the feed from the real account, not a static file.
            if not self._authed():
                return
            ids = scrape_subscriptions()
            if ids is None:
                self._json(502, {"error": "subscriptions unavailable"})
                return
            self._json(200, {"channel_ids": ids})
        elif u.path == "/v1/state":
            # Long-poll: returns as soon as the state version differs from
            # `since` (immediately if it already does), else after `wait`
            # seconds with the unchanged state.
            if not self._authed():
                return
            q = parse_qs(u.query)
            try:
                since = int(q.get("since", ["-1"])[0])
                wait = max(0.0, min(60.0, float(q.get("wait", ["0"])[0])))
            except ValueError:
                self._json(400, {"error": "bad since/wait"})
                return
            self._json(200, _wait_state(since, wait))
        else:
            self._json(404, {"error": "not found"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _do_control(self, body: dict) -> None:
        action = body.get("action", "")
        if action in ("pause", "toggle"):
            # Legacy "pause" is a toggle (Home Assistant / OpenClaw rely on it).
            rc, out = control_pause()
        elif action == "set_pause":
            rc, out = control_set_pause(bool(body.get("paused")))
        elif action == "stop":
            rc, out = control_stop()
            with _state_lock:
                _clear_media_locked()
            if rc == 404:
                rc = 0  # nothing to stop is still "stopped"
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
            if rc == 1 and "remembered" in out:
                rc = 0  # nothing playing: still a successful volume change
        elif action == "status":
            # Legacy read-only status (pre-v1 clients): served from the
            # poller's cached snapshot, no extra CDP/IPC round-trip.
            snap = _build_snapshot()
            active = snap["position_s"] is not None
            self._json(200, {
                "status": "ok",
                "volume": snap["volume"],
                "active": active,
                "backend": (_pb.get("backend") if active else None),
                "mpv": active and _pb.get("backend") == "mpv",
                "position": snap["position_s"],
                "duration": snap["duration_s"],
                "paused": snap["activity"] == "paused" if active else None,
                "seekable": active,
                "title": snap["title"] if active else None,
            })
            return
        else:
            self._json(400, {"error": f"unknown action: {action!r}"})
            return

        LOG.info("control action=%s rc=%s detail=%s", action, rc, out)
        _publish()
        if rc == 404:
            self._json(404, {"error": "no active cast", "detail": out})
            return
        if rc != 0:
            self._json(502, {"error": f"{action} failed", "detail": out})
            return
        self._json(200, {"status": "ok", "detail": out})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in ("/control", "/screensaver", "/power", "/open"):
            self._json(404, {"error": "not found"})
            return
        if not self._authed():
            return
        body = self._read_body()
        if path == "/control":
            self._do_control(body)
            return
        if path == "/screensaver":
            theme = body.get("theme")
            if theme in (None, "", "off"):
                rc, out = _stop_screensaver()
                self._json(200 if rc == 0 else 502,
                           {"status": "ok", "screensaver": None} if rc == 0 else {"error": out})
                return
            rc, backend, out = _start_screensaver(str(theme))
            if rc == 400:
                self._json(400, {"error": out})
                return
            if rc != 0:
                self._json(502, {"error": f"{backend} failed", "rc": rc, "stderr": out})
                return
            self._json(200, {"status": "ok", "screensaver": theme})
            return
        if path == "/power":
            action = body.get("action")
            if action == "sleep":
                do_sleep("api")
            elif action == "wake":
                do_wake("api")
            elif action == "screen_off":
                do_screen_off()
            elif action == "poweroff":
                # WARNING (not INFO) — the single most consequential
                # action; worth standing out in the journal as an audit trail.
                LOG.warning("POWEROFF requested from %s — shutting the host down",
                            self.client_address[0])

                def _do_poweroff() -> None:
                    # Answer first: the response has to make it back through
                    # the container before the machine starts going away.
                    time.sleep(1.5)
                    # -n: fail fast instead of hanging if the sudoers grant
                    # is somehow missing.
                    rc, out = _run(["sudo", "-n", POWEROFF_BIN], timeout=30)
                    if rc != 0:
                        LOG.error("poweroff command failed rc=%s: %s", rc, out)

                threading.Thread(target=_do_poweroff, name="poweroff", daemon=True).start()
            else:
                self._json(400, {"error": f"unknown power action: {action!r}"})
                return
            self._json(200, {"status": "ok"})
            return

        # /open
        url = body.get("url", "")
        if not isinstance(url, str) or not _check_url(url):
            self._json(400, {"error": "bad url"})
            return
        # The caller can force the mpv-direct backend for a URL it has
        # already resolved to a playable media stream (e.g. the
        # container's Jellyfin integration hands us an HLS/stream URL).
        want_backend = body.get("backend", "")
        # Optional display title from the caller (e.g. the container's
        # Jellyfin integration passes the movie/episode name).
        want_title = (body.get("title") or "").strip() or None
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        if want_title and not meta.get("title"):
            meta["title"] = want_title
        try:
            start = float(body.get("start") or 0)
        except (TypeError, ValueError):
            start = 0.0
        # A real cast always wins over whatever screensaver / sleep /
        # screen-off state was active. Cleared *before* _cast() runs: it
        # kicks off youtube_go_fullscreen, which locks the new element's
        # volume via _effective_volume() almost immediately, once.
        media = _media_meta(url, meta)
        with _state_lock:
            _mark_awake_locked()
            _clear_media_locked()
            _state["media"] = media
        _publish()
        rc, backend, out = _cast(url, want_backend=want_backend,
                                 want_title=want_title, start=start)
        with _state_lock:
            if _state["media"] is media:
                if rc != 0:
                    _clear_media_locked()
                else:
                    media["backend"] = backend
        _publish()
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
    # need them, so fill in the standard per-user defaults if unset.
    uid = os.getuid()
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    os.environ.setdefault(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"
    )
    # Local YouTube anti-throttle proxy (loopback only). Started before
    # the control server so the first cast can already use it.
    start_proxy()
    _publish()
    # One poller owns playback readout + wake detection (see _state_poller).
    threading.Thread(target=_state_poller, name="state", daemon=True).start()
    # Idle screensaver watchdog — fires even with no client connected.
    threading.Thread(target=_screensaver_watchdog, name="screensaver", daemon=True).start()
    # Keep-awake watchdog — stops DPMS/blank from tripping mid-playback.
    threading.Thread(target=_keepawake_watchdog, name="keepawake", daemon=True).start()
    # 0.0.0.0 so the container reaches us via host.docker.internal.
    # Token is the trust boundary. See module docstring.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # Long-polls park a thread each; daemon threads so a restart is clean.
    server.daemon_threads = True
    LOG.info("mediacast-host listening on :%d (DISPLAY=%s)", PORT, os.environ.get("DISPLAY", "?"))
    server.serve_forever()


if __name__ == "__main__":
    main()
