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

## Remote Access

Once Tailscale is connected, access services via your Tailscale IP:
- `http://100.x.x.x:8123` - Home Assistant
- `http://100.x.x.x:8096` - Jellyfin

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
