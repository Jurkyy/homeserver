# Install Guide — Fresh Debian on the Homeserver

Step-by-step for wiping Windows 10 and installing Debian 13 as the only OS,
then bringing the full stack online via `bootstrap.sh`.

If you want **dual-boot** with Windows instead, see [SETUP.md](SETUP.md).

---

## Phase 0 — Credentials to grab in advance

You can run bootstrap without any of these (it'll skip what's missing and stay
re-runnable), but having them ready saves a second pass.

| What | Where |
|---|---|
| **NordVPN access token** | <https://my.nordaccount.com/dashboard/nordvpn/access-tokens/> → Generate new token |
| **Spotify Client ID / Secret** | <https://developer.spotify.com/dashboard> → Create app. Redirect URI **must use HTTPS** (`https://localhost:6680`) — Spotify rejects plain http. |
| **Spotify Premium username / password** | Your account (Premium required for Mopidy-Spotify) |
| **Tailscale auth key** | <https://login.tailscale.com/admin/settings/keys> → Generate auth key |
| **Anthropic API key** | <https://console.anthropic.com> (only if running OpenClaw) |
| **Discord bot token + client ID** | <https://discord.com/developers/applications> (only if running OpenClaw) |

---

## Phase 1 — Prep the Windows box (before booting the USB)

1. **Back up anything you want to keep.** The install wipes the disk.
2. **Disable BitLocker** if it's on (Settings → Privacy & Security → Device
   encryption → Off, or PowerShell as admin: `manage-bde -status` to check,
   `manage-bde -off C:` to start decryption). Wait for it to finish.
3. **Disable Fast Startup** — Control Panel → Power Options → "Choose what the
   power buttons do" → "Change settings that are currently unavailable" →
   uncheck **Turn on fast startup**.
4. **Identify your disks.** PowerShell:
   ```powershell
   Get-Disk
   ```
   Note the number of disks, sizes, and which is NVMe vs SATA. Phone-photo it.
5. **Find your boot-menu key.** Reboot and watch the splash:
   - Dell / Lenovo: usually `F12`
   - HP / MSI: usually `F11`
   - Asus: usually `F8`
   - Some boards: `Esc`
6. **(Optional) Disable Secure Boot** in BIOS (Del or F2 to enter). Debian 13
   supports it, but turning it off avoids one class of weird failures.
7. Shut down fully. Plug the USB in.

---

## Phase 2 — Flash the USB (from your dev machine)

```bash
# Download the netinst ISO (~700MB)
curl -L -o ~/Downloads/debian-13.5.0-amd64-netinst.iso \
  https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.5.0-amd64-netinst.iso

# Verify checksum (optional but recommended)
cd ~/Downloads
curl -LO https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing 2>&1 | grep netinst
# Expect: debian-13.5.0-amd64-netinst.iso: OK

# Find the USB device — plug in stick, run lsblk, the new device is yours
lsblk

# Flash (replace /dev/sdX with your USB — NOT a partition like /dev/sdX1)
sudo dd if=~/Downloads/debian-13.5.0-amd64-netinst.iso of=/dev/sdX bs=4M \
  status=progress conv=fsync
```

GUI alternative: `balena-etcher` or GNOME Disks → "Restore Disk Image."

---

## Phase 3 — Boot from USB & install Debian

1. Plug USB into the homeserver, power on, mash the boot-menu key
2. Pick the USB stick
3. **Graphical Install**

### Installer screens that matter

| Screen | Choice |
|---|---|
| Hostname | `homeserver` |
| Domain name | blank or `local` |
| Root password | **leave empty** (forces sudo for the user — better security) |
| User account | create your user (becomes the sudo user) |
| Partitioning method | **Guided — use entire disk** |
| Select disk to partition | The disk you want Debian on. **All data on it is destroyed.** If you have multiple disks and Debian should only own one, pick carefully here. |
| Partitioning scheme | **All files in one partition** |
| Write changes to disk? | Review the summary, then **Yes** |
| Software selection | Check **`Debian desktop environment`** + **`… Xfce`** + **`SSH server`** + **`standard system utilities`**. Uncheck everything else (GNOME, KDE, web server, print server). XFCE is the lightweight DE for the projector-attached Jellyfin/Kodi use case. |
| GRUB bootloader location | The whole disk you just partitioned (e.g. `/dev/nvme0n1`), **not** a partition (`/dev/nvme0n1p1`) |

Reboot when prompted. Pull the USB stick out during reboot.

---

## Phase 4 — First boot, then SSH in

1. Log in on the console as the user you created
2. Find the LAN IP:
   ```bash
   ip a | grep inet
   ```
3. From your laptop:
   ```bash
   ssh <user>@<lan-ip>
   ```

Everything from here happens over SSH.

---

## Phase 5 — Clone and run bootstrap

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/Jurkyy/homeserver.git ~/homeserver
cd ~/homeserver
sudo ./bootstrap.sh
```

The script will run through, in order:

1. System update + dev tools (neovim, eza, bat, zoxide, ripgrep, fzf, …)
2. mise + Python 3.12 + uv
3. SSH, Docker, Tailscale
4. Storage HDD detect/mount at `/mnt/storage` (skip-friendly if only one disk)
5. Prompt for **all `.env` keys** — NordVPN token + country, Spotify, Tailscale,
   Anthropic, Discord. Press Enter to skip any you don't have; re-run later.
6. Shell aliases, projects dir
7. Connect Tailscale (browser auth, or pre-supplied auth key)
8. Install NordVPN, build allowlist (LAN + Tailscale CGNAT + SSH), connect
9. Set Tailscale hostname to `homeserver`
10. Install mDNS multicast carve-out (systemd unit that punches a hole in
    NordVPN's kill-switch so librespot can announce Spotify Connect on
    the LAN)
11. Install projector session (lightdm autologin, Firefox ESR + uBlock +
    SponsorBlock policies, mediacast-host systemd --user unit, DPMS idle
    timeout, auto-generate `MEDIACAST_TOKEN`)
12. `docker compose up -d` → Home Assistant, Jellyfin, Navidrome, librespot,
    Caddy, OpenClaw, Mosquitto, mediacast

---

## Phase 6 — Verify

```bash
# Re-login so the docker + audio + nordvpn group memberships take effect
exit
ssh <user>@<lan-ip>

# Containers all running?
docker compose ps

# Welcome page reachable?
curl -I http://<lan-ip>/

# Audio group GID — librespot needs this to access /dev/snd.
# Should print 29 on Debian 13. If different, set LIBRESPOT_AUDIO_GID in
# .env and restart: docker compose up -d librespot
getent group audio | cut -d: -f3

# Scarlett 2i2 visible to ALSA? Plug it in via USB first.
# Expect a line like:
#   card 2: USB [Scarlett 2i2 USB], device 0: USB Audio [USB Audio]
# If the card name is not "USB", set LIBRESPOT_ALSA_DEVICE in .env to
# plughw:CARD=<that-name>,DEV=0 and restart librespot.
aplay -l | grep -i scarlett

# NordVPN status — should be Connected
nordvpn status

# mDNS carve-out active? (active = it re-applied the ip rule + iptables
# ACCEPT after the last nordvpnd start)
systemctl is-active nordvpn-mdns-carveout.service

# Spotify Connect announcement actually leaving the box on the LAN iface?
# Restart librespot first to force re-announcement, then sniff. You
# should see traffic between 192.168.x.22.5353 and 224.0.0.251.5353
# carrying _spotify-connect._tcp.local. records.
docker compose restart librespot &
sudo tcpdump -nn -i <lan-iface> -c 10 'udp port 5353'
```

From your laptop's browser on the same LAN:

- `http://<lan-ip>/` → Caddy welcome page
- `http://<lan-ip>:8123` → Home Assistant
- `http://<lan-ip>:8096` → Jellyfin

And from the Spotify app (mobile/desktop/web) on any device logged into
your account on the same LAN:

- Device picker → **Homeserver** → press play → audio out the Scarlett.

Projector cast — test that the mediacast pipeline reaches Firefox on
the projector:

```bash
# Bring the projector + HDMI up by sending a real URL.
TOKEN=$(grep ^MEDIACAST_TOKEN= ~/homeserver/.env | cut -d= -f2)
curl -X POST http://<lan-ip>:8765/cast \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'
# Expect: {"status":"ok"} and the video playing on the projector
# within ~2s. uBlock should silence the pre-roll, SponsorBlock should
# skip the sponsor segment.

# Host helper alive? (run as the bootstrap user, not root)
systemctl --user status mediacast-host

# Then set up the Android side per docs/projector-cast.md — HTTP
# Shortcuts share-menu target that fires the same POST.
```

---

## Common gotchas

- **"<user> is not in the sudoers file."** You set a root password during
  install — that path skips the auto-add to the `sudo` group. Fix:
  ```bash
  su -                              # root password from installer
  /usr/sbin/usermod -aG sudo <user>
  exit && exit                      # full SSH logout
  # ssh back in, then `sudo -v` to confirm
  ```
  (Leaving the root password blank during install avoids this entirely.)
- **NordVPN kills LAN access.** If clients can't reach the homeserver after
  bootstrap, the LAN allowlist auto-detection failed for an unusual subnet
  (anything other than /24 or /16). Add manually:
  ```bash
  sudo nordvpn allowlist add subnet 192.168.x.0/24   # your actual LAN
  ```
- **Spotify catalog looks wrong.** NordVPN exit country drives Spotify's
  region. Pin it: edit `.env`, set `NORDVPN_COUNTRY=Netherlands` (or whatever),
  then `sudo nordvpn disconnect && sudo nordvpn connect`.
- **librespot can't reach ALSA.** GID mismatch — see Phase 6 audio-group check.
- **Music plays but you hear nothing.** librespot is hitting the wrong card
  (probably onboard HDA instead of the Scarlett). Run `aplay -l` on the
  host, take the card name from the Scarlett line, and set
  `LIBRESPOT_ALSA_DEVICE=plughw:CARD=<name>,DEV=0` in `.env`. Restart with
  `docker compose up -d librespot`. Also confirm the Scarlett's front-panel
  monitor knob is up and the speakers are powered on (always check the
  physical chain first).
- **"Homeserver" doesn't appear in the Spotify device picker.** The mDNS
  announcement isn't reaching the LAN. Most likely the NordVPN carve-out
  isn't loaded — `systemctl is-active nordvpn-mdns-carveout.service`
  should return `active`. If it's not, `sudo systemctl restart
  nordvpn-mdns-carveout` re-applies the routing/iptables fix.
- **Tailscale dies after NordVPN connect.** Allowlist missing the Tailscale
  CGNAT range. Bootstrap adds it automatically, but to fix manually:
  ```bash
  sudo nordvpn allowlist add subnet 100.64.0.0/10
  ```
