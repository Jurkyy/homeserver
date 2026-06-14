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
UPSTREAM_TIMEOUT = float(os.environ.get("MEDIACAST_UPSTREAM_TIMEOUT", "30.0"))
# A plain desktop UA — googlevideo serves the signed URL to any client;
# bounded ranges work with no special headers (verified with curl).
PROXY_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


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


def control_pause() -> tuple[int, str]:
    # Prefer mpv (true toggle via IPC); fall through to Firefox space
    # if no mpv is active. Both videos and most HTML5 players treat
    # space as play/pause.
    rc, out = _mpv_command(["cycle", "pause"])
    if rc != 404:
        return rc, f"mpv: {out}"
    return _no_cast_or(*_firefox_key("space"))


def control_seek(offset: int) -> tuple[int, str]:
    # mpv: relative seek in seconds (positive = forward). Firefox: send
    # j/l (YouTube-specific 10s skip) for ±10 and arrow keys for smaller
    # deltas; arrow keys are the HTML5 5s standard.
    rc, out = _mpv_command(["seek", offset, "relative"])
    if rc != 404:
        return rc, f"mpv seek {offset:+d}s: {out}"
    if abs(offset) >= 10:
        return _no_cast_or(*_firefox_key("l" if offset > 0 else "j"))
    return _no_cast_or(*_firefox_key("Right" if offset > 0 else "Left"))


def control_seek_abs(seconds: float) -> tuple[int, str]:
    # Absolute seek to a position in seconds. mpv-only: there's no
    # reliable absolute-seek keystroke for an arbitrary Firefox page, so
    # the seek bar is an mpv feature (covers YouTube + mrpflix, both of
    # which play through mpv). A Firefox-only cast reports no active cast.
    rc, out = _mpv_command(["seek", seconds, "absolute"])
    if rc == 404:
        return 404, "no active cast"
    return rc, f"mpv seek -> {seconds:.0f}s: {out}"


def mpv_playback_state() -> dict:
    """Current {position, duration, paused, title} from mpv, or Nones if idle.

    Reads a few properties over one IPC connection. Matches each reply by
    request_id so an interleaved mpv event line can't be mistaken for the
    answer. Any failure (no socket, stale socket, unparseable) degrades
    to all-None, which the UI reads as "nothing playing".
    """
    import socket as _sock

    empty = {"position": None, "duration": None, "paused": None, "title": None}
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
            # mpv-alive is derived from whether we actually read a
            # position (a stale socket file alone doesn't count).
            _, vol = control_volume_get()
            pb = mpv_playback_state()
            self._json(200, {
                "status": "ok",
                "volume": vol,
                "mpv": pb["position"] is not None,
                "position": pb["position"],
                "duration": pb["duration"],
                "paused": pb["paused"],
                # Only meaningful while mpv is actually playing; the UI
                # ignores it otherwise (a stale socket can still hold an
                # old media-title).
                "title": pb["title"] if pb["position"] is not None else None,
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

        dpms = wake_display()

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
            "cast url_host=%s backend=%s rc=%s dpms=[%s] focus=[%s] fs=[%s]",
            urlparse(url).netloc, backend, rc, dpms, focus, fs,
        )
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
    # Local YouTube anti-throttle proxy (loopback only). Started before
    # the control server so the first cast can already use it.
    start_proxy()
    # 0.0.0.0 so the container reaches us via host.docker.internal.
    # Token is the trust boundary. See module docstring.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("mediacast-host listening on :%d (DISPLAY=%s)", PORT, os.environ.get("DISPLAY", "?"))
    server.serve_forever()


if __name__ == "__main__":
    main()
