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
| Mopidy / Iris  | 6680  | http://localhost:6680/iris/  | LAN web music player (Spotify/YT/local) |
| Caddy (proxy)  | 80    | http://music.local           | LAN reverse proxy &rarr; Mopidy/Iris    |

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

## Local DNS

The bootstrap installs `avahi-daemon` on the host and publishes a
`music.local` CNAME alias over mDNS. A Caddy reverse proxy container
fronts the stack on port 80 and routes `music.local` &rarr; Mopidy/Iris.

- **macOS / Linux / Android (with an mDNS-aware app):** just open
  [http://music.local](http://music.local) on the LAN.
- **Windows:** `.local` resolution requires
  [Bonjour Print Services](https://support.apple.com/kb/DL999) (free
  Apple install). Without it, add a line to
  `C:\Windows\System32\drivers\etc\hosts`:
  ```
  192.168.x.x   music.local
  ```
  (replace with the homeserver's LAN IP).
- **Tailscale fallback:** Caddy listens on `:80`, so
  `http://<tailscale-ip>` reaches the welcome page and `music.local`
  works inside the tailnet if the resolver knows about it. If not, hit
  Mopidy directly at `http://<tailscale-ip>:6680/iris/`.

## Remote Access

Once Tailscale is connected, access services via your Tailscale IP:
- `http://100.x.x.x:8123` - Home Assistant
- `http://100.x.x.x:8096` - Jellyfin
- `http://100.x.x.x:6680/iris/` - Mopidy / Iris

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
  `music.local`, Jellyfin, Home Assistant, etc. on the homeserver's LAN
  IP without going through Nord.
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

### Mopidy / Iris web player

Mopidy with the Iris web UI gives you a browser-based music player that
streams Spotify, YouTube, and your local library, and plays audio out of
the homeserver's physical sound-card jack (ALSA passthrough via
`/dev/snd`). Open `http://<homeserver>:6680/iris/` from any device on
the LAN.

**Spotify Premium is required** for Mopidy-Spotify to stream tracks —
free accounts cannot authenticate against librespot. Set the following
in `.env` (see `.env.example` for full notes):

- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — Spotify Developer App
  (shared with the Home Assistant Spotify integration). When creating
  the app at <https://developer.spotify.com/dashboard>, the Redirect URI
  must use HTTPS — Spotify rejects plain `http://localhost`. Use
  `https://localhost:6680` (the URL isn't actually loaded by Mopidy,
  Spotify just validates the scheme).
- `SPOTIFY_USERNAME` / `SPOTIFY_PASSWORD` — your Spotify account
  credentials (a Spotify-issued password, not Facebook/Google login)
- `MOPIDY_AUDIO_GID` — host audio group GID so the container can access
  `/dev/snd` (Debian 13 = 29, Arch = 996; default 29). Find yours with
  `getent group audio | cut -d: -f3`.
