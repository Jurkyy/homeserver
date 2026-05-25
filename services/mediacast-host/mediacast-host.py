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

import hmac
import json
import logging
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LOG = logging.getLogger("mediacast-host")

PORT = int(os.environ.get("MEDIACAST_HOST_PORT", "8766"))
TOKEN = os.environ.get("MEDIACAST_TOKEN", "")
FIREFOX_BIN = os.environ.get("MEDIACAST_FIREFOX_BIN", "firefox-esr")
# Subprocess wall-clock budget. Firefox `--new-tab` returns once it has
# handed the URL to the running instance, which is usually fast; bound
# it so a wedged Firefox doesn't hold the HTTP response open.
SUBPROC_TIMEOUT = float(os.environ.get("MEDIACAST_SUBPROC_TIMEOUT", "10.0"))


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run cmd, return (exit_code, combined_stdout_stderr). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROC_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return 127, f"binary not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {SUBPROC_TIMEOUT}s: {' '.join(cmd)}"
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

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/open":
            self._json(404, {"error": "not found"})
            return
        if not _check_auth(self.headers.get("Authorization")):
            LOG.warning("auth failed from %s", self.client_address[0])
            self._json(401, {"error": "bad token"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad json: {exc}"})
            return
        url = body.get("url", "")
        if not _check_url(url):
            self._json(400, {"error": "bad url"})
            return

        dpms = wake_display()
        rc, ff_out = open_in_firefox(url)
        focus = focus_firefox()

        LOG.info(
            "cast url_host=%s firefox_rc=%s dpms=[%s] focus=[%s]",
            urlparse(url).netloc, rc, dpms, focus,
        )
        if rc != 0:
            self._json(502, {"error": "firefox failed", "rc": rc, "stderr": ff_out})
            return
        self._json(200, {"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MEDIACAST_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not TOKEN:
        raise SystemExit("MEDIACAST_TOKEN unset — refusing to start with an open endpoint")
    # 0.0.0.0 so the container reaches us via host.docker.internal.
    # Token is the trust boundary. See module docstring.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("mediacast-host listening on :%d (DISPLAY=%s)", PORT, os.environ.get("DISPLAY", "?"))
    server.serve_forever()


if __name__ == "__main__":
    main()
