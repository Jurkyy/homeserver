# Homeserver

A self-hosted home server stack featuring OpenClaw (AI Discord bot), Home Assistant (home automation), Jellyfin (media streaming), and Tailscale (secure networking).

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
| mediacast      | 8765  | http://homeserver.local/ | Phone/PC → projector URL cast (web UI + bookmarklet + share-menu) |
| Caddy (proxy)  | 80    | http://&lt;lan-ip&gt;/         | LAN welcome page                       |

## Project Structure

```
homeserver/
├── bootstrap.sh        # Fresh box setup script
├── docker-compose.yml  # All services defined
├── .env.example        # Template for secrets
├── SETUP.md            # Detailed setup notes
├── dotfiles/
│   └── aliases         # Shell aliases (installed to ~/.aliases)
├── scripts/
│   ├── backup.sh       # Backup all configs
│   ├── deploy.sh       # Deploy projects to server
│   └── update.sh       # Update services
└── services/
    ├── homeassistant/
    │   └── config/     # HA configuration
    ├── jellyfin/
    │   ├── config/     # Jellyfin configuration
    │   └── cache/      # Transcoding cache
    ├── openclaw/
    │   └── config/     # OpenClaw configuration
    └── projects/
        └── polymarket-insider-bot.service
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
  great for guests. Comes with a drag-to-bookmarks "Cast" bookmarklet
  that posts the current tab's URL in one tap. (Resolved via mDNS by
  avahi-daemon — every modern OS supports `.local` names without
  extra config. The bare LAN IP works too as a fallback.)
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
- The `bootstrap.sh` `install_mdns_carveout` step installs a systemd
  unit that adds an `ip rule` + iptables ACCEPT for outbound multicast
  on the LAN interface — without it, NordVPN's kill-switch swallows
  the announcements.
- ALSA card name is set via `LIBRESPOT_ALSA_DEVICE` in `.env`. Default
  is `plughw:CARD=USB,DEV=0`; verify with `aplay -l` on the host.

## Remote Access

Once Tailscale is connected, access services via your Tailscale IP:
- `http://100.x.x.x:8123` - Home Assistant
- `http://100.x.x.x:8096` - Jellyfin
- `http://100.x.x.x/` - Caddy welcome page (LAN service directory)

(Spotify Connect is LAN-only — the librespot device is announced over
mDNS, which doesn't cross subnets, so it only shows up in the Spotify
device picker when the controlling device is on the home LAN.)

## VPN (NordVPN, always-on)

The bootstrap installs the official NordVPN CLI and configures it as an
**always-on, whole-host VPN**. All outbound traffic from the homeserver
— including every Docker container that uses the host's network stack —
exits through Nord. Autoconnect is enabled, so the VPN comes up on every
boot before user services start.

A few practical notes:

- **Spotify catalog follows your exit country.** Pin `NORDVPN_COUNTRY` in
  `.env` (e.g. `Netherlands`, `Germany`, `United_States`) if your Spotify
  account is region-locked or you want a specific catalog. Leave blank to
  let Nord pick the fastest server.
- **LAN access is preserved.** The bootstrap detects your LAN subnet
  (e.g. `192.168.1.0/24`) and adds it to NordVPN's allowlist before the
  kill-switch engages. Phones/laptops on the same network can still hit
  the Caddy welcome page, Jellyfin, Home Assistant, etc. on the
  homeserver's LAN IP without going through Nord.
- **mDNS multicast carve-out.** NordVPN's allowlist is per-subnet — it
  doesn't cover `224.0.0.0/4` (the mDNS group), so the kill-switch
  drops librespot's Spotify Connect announcements by default.
  `install_mdns_carveout` (run after `install_nordvpn` in bootstrap)
  installs a small systemd unit that re-adds the `ip rule` + iptables
  ACCEPT after every boot or `nordvpnd` restart.
- **Tailscale keeps working.** The Tailscale CGNAT range (`100.64.0.0/10`)
  is also allowlisted, so remote access via your tailnet is unaffected.
- **Kill-switch is on.** If the Nord tunnel ever drops, non-LAN /
  non-Tailscale traffic is blocked rather than leaking out the bare WAN.

Operate it with:

```bash
# Temporarily disable (e.g. for a service that breaks behind VPN):
sudo nordvpn disconnect
sudo nordvpn set autoconnect off    # also stop it coming back on reboot

# Re-enable always-on:
sudo nordvpn set autoconnect on
sudo nordvpn connect

# Check state:
nordvpn status
nordvpn settings
nordvpn allowlist
```

If you left `NORDVPN_TOKEN` blank during bootstrap, the client is
installed but not logged in. Generate a token at
<https://my.nordaccount.com/dashboard/nordvpn/access-tokens/>, add it to
`.env`, then run:

```bash
sudo nordvpn login --token "$NORDVPN_TOKEN"
sudo nordvpn set killswitch on
sudo nordvpn set autoconnect on
sudo nordvpn connect
```

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

For detailed setup instructions, troubleshooting, and configuration guides, see [SETUP.md](SETUP.md).

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
