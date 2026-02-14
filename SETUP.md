# Detailed Setup Guide

## Dual-Boot Setup (Windows + Linux)

If you have a fresh Windows install and want to run the homeserver on Arch Linux alongside it, follow these steps to shrink Windows and install Arch on the freed space.

### 1. Prepare Windows

Do all of this **before** booting the USB.

**Shrink the Windows partition:**

1. Press `Win+X` > **Disk Management**
2. Right-click the main partition (usually `C:`) > **Shrink Volume**
3. Shrink it down to ~100GB (102400 MB). The remaining space becomes unallocated — that's where Arch goes.

**Disable Fast Startup** (prevents Windows from locking the disk):

1. Control Panel > Power Options > **Choose what the power buttons do**
2. Click **Change settings that are currently unavailable**
3. Uncheck **Turn on fast startup**
4. Save changes

**Disable Secure Boot:**

1. Reboot into BIOS/UEFI (usually `Del`, `F2`, or `F12` at POST)
2. Find Secure Boot under Security or Boot settings and **disable** it
3. Save and exit

### 2. Create Bootable USB

1. Download the Arch ISO from [archlinux.org/download](https://archlinux.org/download/)
2. Flash it to a USB drive using [Ventoy](https://ventoy.net/) (recommended — lets you put multiple ISOs on one drive) or [Rufus](https://rufus.ie/)
3. Boot from the USB (mash `F12`, `F2`, or `Del` at POST to get the boot menu)

### 3. Install Arch

Use `archinstall` for a guided install — no need to do it the hard way for a server.

```bash
archinstall
```

Key selections:

| Setting | Value |
|---|---|
| **Disk** | Select the free/unallocated space. **Do NOT touch the Windows partitions.** |
| **Filesystem** | ext4 |
| **Bootloader** | GRUB (it will detect Windows automatically) |
| **Profile** | minimal (no desktop environment — this is a server) |
| **Network** | Enable NetworkManager or systemd-networkd |
| **User** | Create a user account with sudo access |

### 4. Post-Install: Verify Dual Boot

GRUB should auto-detect Windows and add it to the boot menu. If Windows doesn't show up:

```bash
sudo grub-mkconfig -o /boot/grub/grub.cfg
```

Set Linux as the default and give yourself time to pick Windows if needed:

```bash
sudo sed -i 's/^GRUB_DEFAULT=.*/GRUB_DEFAULT=0/' /etc/default/grub
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=5/' /etc/default/grub
sudo grub-mkconfig -o /boot/grub/grub.cfg
```

Reboot and verify both OSes boot correctly.

### 5. Continue With Bootstrap

Once Arch is running, clone the repo and run the bootstrap script:

```bash
git clone <your-repo-url> homeserver
cd homeserver
sudo ./bootstrap.sh
```

Then continue with the rest of this guide below.

---

## Hardware Recommendations

- **CPU**: Any modern x86_64 processor (Intel/AMD)
- **RAM**: 4GB minimum, 8GB+ recommended
- **Storage**:
  - 32GB+ for OS and configs
  - External drive for media (mounted at `/mnt/media`)
- **Network**: Ethernet recommended for reliability

## Pre-Setup Checklist

- [ ] Fresh Linux installation (Ubuntu 22.04+ or Arch Linux)
- [ ] Network connectivity
- [ ] External drive ready for media storage
- [ ] Anthropic API key from [console.anthropic.com](https://console.anthropic.com)
- [ ] Discord bot created at [Discord Developer Portal](https://discord.com/developers/applications)
- [ ] Tailscale account at [tailscale.com](https://tailscale.com)

## Step-by-Step Setup

### 1. Mount External Drive

```bash
# Find your drive
lsblk

# Create mount point
sudo mkdir -p /mnt/media

# Mount the drive (replace /dev/sdX1 with your partition)
sudo mount /dev/sdX1 /mnt/media

# Add to /etc/fstab for automatic mounting
echo "/dev/sdX1 /mnt/media ext4 defaults 0 2" | sudo tee -a /etc/fstab
```

### 2. Run Bootstrap Script

```bash
cd homeserver
sudo ./bootstrap.sh
```

This will:
- Install Docker and Docker Compose
- Install Tailscale
- Install basic tools (git, curl, htop, vim)
- Create `.env` from template
- Prompt for API keys

### 3. Configure Services

Edit `.env` with your actual credentials:

```bash
nano .env
```

### 4. Start Services

```bash
docker compose up -d
```

## Service Configuration

### OpenClaw (Discord Bot)

1. **Create Discord Application**:
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Click "New Application"
   - Go to "Bot" section, click "Add Bot"
   - Copy the bot token to `DISCORD_BOT_TOKEN` in `.env`
   - Copy the Application ID to `DISCORD_CLIENT_ID` in `.env`

2. **Invite Bot to Server**:
   - Go to OAuth2 > URL Generator
   - Select scopes: `bot`, `applications.commands`
   - Select permissions: `Send Messages`, `Read Message History`, etc.
   - Use generated URL to invite bot

3. **Access Control UI**: http://localhost:18789

### Home Assistant

1. **Initial Setup**:
   - Open http://localhost:8123
   - Create admin account
   - Set location and units
   - Discover devices on your network

2. **Configuration files** are stored in `services/homeassistant/config/`

### Jellyfin

1. **Initial Setup**:
   - Open http://localhost:8096
   - Create admin account
   - Add media libraries (point to `/media` which maps to `/mnt/media`)

2. **Add Libraries**:
   - Movies: `/media/movies`
   - TV Shows: `/media/tv`
   - Music: `/media/music`

### Tailscale

1. **Get Auth Key**:
   - Go to [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys)
   - Generate an auth key (reusable recommended)
   - Add to `TAILSCALE_AUTHKEY` in `.env`

2. **Connect Host** (optional, for direct access):
   ```bash
   sudo tailscale up
   ```

3. **Approve Device** in Tailscale admin console

## Troubleshooting

### Services Not Starting

```bash
# Check container logs
docker compose logs -f <service-name>

# Check container status
docker compose ps

# Restart a specific service
docker compose restart <service-name>
```

### Permission Issues

```bash
# Fix config directory permissions
sudo chown -R 1000:1000 services/

# Re-login after adding to docker group
newgrp docker
```

### Network Issues

```bash
# Check if ports are in use
ss -tulpn | grep -E '8123|8096|18789'

# Check Tailscale status
tailscale status
```

### External Drive Not Mounted

```bash
# Check if mounted
mountpoint /mnt/media

# Manual mount
sudo mount /dev/sdX1 /mnt/media

# Check fstab entry
cat /etc/fstab | grep media
```

## Backup and Restore

### Backup

```bash
./scripts/backup.sh
```

Backups are stored in `backups/` directory, keeping the last 7.

### Restore

```bash
# Stop services
docker compose down

# Extract backup
tar -xzf backups/config_backup_YYYYMMDD_HHMMSS.tar.gz

# Start services
docker compose up -d
```

## Security Notes

- **Never commit `.env`** to git (it's in `.gitignore`)
- **Use Tailscale** for remote access instead of exposing ports
- **Keep services updated**: `./scripts/update.sh`
- **Regular backups**: Consider adding `./scripts/backup.sh` to cron

### Cron Example

```bash
# Edit crontab
crontab -e

# Add daily backup at 2 AM
0 2 * * * /path/to/homeserver/scripts/backup.sh
```

## Updating Services

```bash
# Pull latest images and recreate
./scripts/update.sh

# Force recreate even if image unchanged
./scripts/update.sh --force
```
