# Homeserver

A self-hosted home server stack centred on **mediacast** — a LAN portal
(web UI + REST API) and native **Android app** that cast URLs, YouTube and
Jellyfin to the projector on the box's HDMI output — alongside **librespot**
(Spotify Connect → Focusrite Scarlett 2i2), **Caddy** (LAN reverse proxy,
`https://home`), **Home Assistant** + Mosquitto (home automation),
**Jellyfin** / Navidrome (media), **OpenClaw** (AI Discord bot), and host
**Tailscale** for remote access.

## Prerequisites

- Linux box (Debian/Ubuntu or Arch-based) with SSD for OS
- Secondary HDD for bulk storage (media, data, backups) — auto-detected by bootstrap
- Tailscale account for secure remote access
- Discord bot token (from Discord Developer Portal)
- Anthropic API key (from console.anthropic.com)

## Quick Start

**On a fresh Arch install**, just run:

```bash
# Install git first (if not installed)
sudo pacman -Sy git

# Clone and run
git clone https://github.com/Jurkyy/homeserver.git ~/homeserver
cd ~/homeserver
sudo ./bootstrap.sh
```

The bootstrap script will:
- Update system and install dev tools (neovim, eza, bat, ripgrep, fzf, etc.)
- Install mise, Python 3.12, uv (for running Python projects)
- Install SSH, Docker, Tailscale
- **Detect and mount your secondary HDD** at `/mnt/storage`
- Set up storage dirs (media, backups, docker, projects)
- Configure shell aliases, prompt for API keys, start all services

That's it. One script does everything.

## Service URLs

| Service        | Port  | URL                          | Description                    |
|----------------|-------|------------------------------|--------------------------------|
| OpenClaw       | 18789 | http://localhost:18789       | AI bot control UI              |
| Home Assistant | 8123  | http://localhost:8123        | Home automation dashboard      |
| Jellyfin       | 8096  | http://localhost:8096        | Media streaming interface      |
| Navidrome      | 4533  | http://localhost:4533        | Local music streaming          |
| librespot      | n/a   | (Spotify app device picker)  | Spotify Connect target → Scarlett 2i2  |
| mediacast      | 8765  | http://homeserver.local/ | Phone/PC → projector URL cast (web UI + share-menu) |
| Caddy (proxy)  | 80/443 | https://home/, http://&lt;lan-ip&gt;/ | Reverse proxy to the mediacast portal; serves `/ca.crt` |

## Project Structure

```
homeserver/
├── bootstrap.sh        # Fresh box setup script (idempotent, re-runnable)
├── docker-compose.yml  # All containers defined
├── .env.example        # Template for secrets
├── INSTALL.md          # Fresh-box install walkthrough
├── SETUP.md            # Detailed setup notes
├── android/            # mediacast Android app (Kotlin/Compose, see android/README.md)
├── docs/
│   ├── api-v1.md           # mediacast API contract (backend ↔ web portal + Android app)
│   ├── projector-cast.md   # Projector casting setup + troubleshooting
│   └── https-home-setup.md # https://home via Caddy's internal CA + dnsmasq
├── dotfiles/
│   └── aliases         # Shell aliases (installed to ~/.aliases)
├── scripts/            # backup / update / deploy / harden-ssh / setup-remote
└── services/
    ├── mediacast/      # Portal + API container (FastAPI, static web UI)
    ├── mediacast-host/ # Host helper (X11/Firefox/mpv driver) + systemd --user units
    ├── librespot/      # Spotify Connect container (ALSA → Scarlett 2i2)
    ├── caddy/          # Caddyfile + internal CA root
    ├── homeassistant/
    │   └── config/     # HA configuration (tracked YAML)
    ├── mosquitto/      # MQTT broker config
    ├── jellyfin/       # Jellyfin config + transcode cache
    ├── navidrome/      # Navidrome data
    ├── openclaw/       # OpenClaw agent config + workspace
    └── projects/       # systemd units for deployed side projects
```

## Helper Scripts

- **Backup configs**: `./scripts/backup.sh`
- **Update services**: `./scripts/update.sh` (use `--force` to force recreate)

## Projector: cast URLs from your phone

The host's HDMI drives a projector. The `mediacast` container wakes
the projector display and opens any URL you give it in a pre-launched
Firefox (with uBlock Origin + SponsorBlock) on the auto-logged-in
Xfce session.

Three ways to send a URL, all hitting the same backend:

- **Web UI**: open `http://homeserver.local/` in any browser (PC or phone,
  Kiwi/Chrome/Safari/Firefox), paste, hit Cast. LAN-only, no login —
  great for guests. (Resolved via mDNS by avahi-daemon — every modern
  OS supports `.local` names without extra config. The bare LAN IP
  works too as a fallback.)
- **Android share menu**: install HTTP Shortcuts once, then any app's
  share button gets a "Cast to projector" option.
- **Scripts / WAN**: `POST :8765/cast` with a bearer token from `.env`.

No keyboard on the projector screen needed for everyday casts; an
attached USB keyboard/mouse covers streaming-site logins.

Setup and troubleshooting: [docs/projector-cast.md](docs/projector-cast.md).

## Music: Spotify Connect via librespot

Spotify playback goes through the `librespot` container, which
advertises itself on the LAN as a Spotify Connect target via mDNS
(zeroconf). The audio path is **librespot → ALSA → USB → Focusrite
Scarlett 2i2 → 1/4" TRS → speakers**.

To use it, open Spotify on any device logged into your account, tap
the device picker, and select **Homeserver**. (Requires Spotify
Premium — Connect doesn't work on free tier.) The container exposes
no web UI; the Spotify app is the remote.

Bits worth knowing:

- mDNS discovery requires the controlling device to be on the same
  LAN as the homeserver (or a Spotify Connect-aware proxy in between).
- librespot publishes its Connect record through the host's
  avahi-daemon (`install_avahi` in `bootstrap.sh`, pinned to the LAN
  interface).
- ALSA card name is set via `LIBRESPOT_ALSA_DEVICE` in `.env`. Default
  is `plughw:CARD=USB,DEV=0`; verify with `aplay -l` on the host.

## Remote Access

Once Tailscale is connected, access services via your Tailscale IP:
- `http://100.x.x.x:8123` - Home Assistant
- `http://100.x.x.x:8096` - Jellyfin
- `http://100.x.x.x/` (or `http://homeserver/` via MagicDNS) - mediacast portal

(Spotify Connect is LAN-only — the librespot device is announced over
mDNS, which doesn't cross subnets, so it only shows up in the Spotify
device picker when the controlling device is on the home LAN.)

## VPN

NordVPN is **no longer used**. Its datacenter exit IP triggered YouTube's
"confirm you're not a bot" wall, broke container DNS, and its kill-switch
blocked Tailscale and LAN mDNS. `install_nordvpn` / `install_mdns_carveout`
are kept in `bootstrap.sh` for reference but are not called. Remote access
is via host Tailscale only.

## Project Deployment

Deploy git repos (Python bots, etc.) from your dev machine to the server and run them as systemd services.

```bash
# Deploy a project
./scripts/deploy.sh ~/dev/polymarket-insider-bot

# Deploy and install as a systemd service
./scripts/deploy.sh ~/dev/polymarket-insider-bot --service

# Deploy to a different host
./scripts/deploy.sh ~/dev/my-project --host myserver
```

Projects are synced to `~/projects/<name>/` on the server. The deploy script runs `mise install` and `mise run setup` (or `uv sync`) automatically.

**Example: Polymarket Insider Bot**

```bash
./scripts/deploy.sh ~/dev/polymarket-insider-bot --service

# Check status
ssh homeserver 'systemctl status polymarket-insider-bot'

# View logs
ssh homeserver 'journalctl -u polymarket-insider-bot -f'
```

## Documentation

For detailed setup instructions, troubleshooting, and configuration guides, see [SETUP.md](SETUP.md)
and [INSTALL.md](INSTALL.md). The mediacast API contract is [docs/api-v1.md](docs/api-v1.md);
the Android client lives in [android/](android/README.md).

### librespot (Spotify Connect)

The `librespot` container is a Spotify Connect target. The user's
Spotify app — mobile, desktop, or web — sees it in the device picker
and streams audio to it directly from Spotify's servers. ALSA
passthrough via `/dev/snd` puts the audio on the host's sound card.
Default path: USB → **Focusrite Scarlett 2i2** → 1/4" TRS → speakers.

**Spotify Premium is required** — Spotify Connect doesn't work on free
accounts. No credentials live in `.env`: the Spotify app hands
librespot a session token when you pick the device.

Knobs in `.env` (see `.env.example` for the full notes):

- `LIBRESPOT_NAME` — display name shown in the Spotify device picker
  (default `Homeserver`).
- `LIBRESPOT_AUDIO_GID` — host audio group GID so the container can
  access `/dev/snd` (Debian 13 = 29, Arch = 996; default 29). Find
  yours with `getent group audio | cut -d: -f3`.
- `LIBRESPOT_ALSA_DEVICE` — ALSA output device. Defaults to
  `plughw:CARD=USB,DEV=0`, which targets the Focusrite Scarlett 2i2
  (class-compliant USB; Linux names the card `USB`). Confirm with
  `aplay -l` on the host. If you swap interfaces, this is the only
  knob — no rebuild needed, just `docker compose up -d librespot`.
