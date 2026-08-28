# HTTPS on the LAN (https://homeserver.local, optionally https://home)

## Current status: `https://homeserver.local` works right now

No router changes, no DHCP changes, no per-device DNS setup. It rides
entirely on mDNS (avahi), which every modern OS already listens for on
the LAN — including guest phones, automatically, the moment they join
Wi-Fi.

The only thing left is trusting Caddy's internal CA on a device if you
want the padlock with no warning (Step 2 below). Skipping that is
fine too — the site still loads, the browser just shows a certificate
warning to click through.

### Why this exists / how it works

A bare name like `home`, or a `.local` name like `homeserver.local`,
can never get a certificate from a public CA (Let's Encrypt etc.) —
the CA/Browser Forum baseline requirements forbid issuing for names
with no publicly-registered domain. So Caddy issues one from its own
internal CA instead (`services/caddy/Caddyfile`, `tls internal` on the
`home` / `homeserver.local` site block). That CA's root just needs
installing on each device that should see the padlock rather than a
warning.

## Step 1 — visit it

`https://homeserver.local/` from any LAN device. `homeserver.local` is
published by the box's avahi-daemon (`install_avahi` in
`bootstrap.sh`), pinned to the LAN interface.

## Step 2 — install the CA root cert on a device (optional, kills the warning)

Download the cert: visit **`http://homeserver.local/ca.crt`** (or
`http://192.168.0.22/ca.crt`) in the device's browser and save it.
Plain HTTP deliberately — no chicken-and-egg with the very cert it's
used to validate. It's also committed at `services/caddy/ca/ca.crt`
for reference; this file is public key material, not a secret.

**Windows**
1. Double-click the downloaded `ca.crt`.
2. **Install Certificate** → **Local Machine** (needs admin) → **Place
   all certificates in the following store** → **Trusted Root
   Certification Authorities** → **Next** → **Finish**.

**macOS**
1. Double-click `ca.crt` — opens Keychain Access, adds it to the
   **login** (or **System**) keychain.
2. Find it (search "Caddy"), double-click it, expand **Trust**, set
   **When using this certificate** to **Always Trust**.
3. Close and enter your password to confirm.

**Android**
1. Settings → **Security** (or **Security & privacy**) → **More
   security settings** → **Encryption & credentials** → **Install a
   certificate** → **CA certificate**.
2. Pick the downloaded file, confirm the warning.
3. Chrome and most system apps use this store directly. (Firefox for
   Android uses its own trust store — same idea, under Firefox's
   Settings → **Privacy & Security** → **Certificates**.)

**iOS / iPadOS**
1. Opening `http://homeserver.local/ca.crt` in Safari prompts to
   install a configuration profile — **Allow**, then install it from
   **Settings → Profile Downloaded**.
2. This alone isn't enough — iOS installs it but doesn't trust it for
   TLS yet. Go to **Settings → General → About → Certificate Trust
   Settings** and enable full trust for the "Caddy Local Authority"
   certificate.

**Linux**
1. System store (covers curl, most non-Firefox apps):
   ```
   sudo cp ca.crt /usr/local/share/ca-certificates/caddy-home.crt
   sudo update-ca-certificates
   ```
2. Firefox keeps its own store — either import `ca.crt` under
   `about:preferences#privacy` → **Certificates** → **View
   Certificates** → **Authorities** → **Import**, or (Chrome/Chromium,
   which does use the system store on Linux) nothing further needed
   after step 1.

## Notes

- The internal CA's root cert is valid 10 years (issued 2026-07-10,
  expires 2036-05-18) and lives in the `caddy` container's persistent
  `services/caddy/data` volume — it survives container
  recreate/restart. It would only need re-distributing if that
  volume is ever wiped.
- Devices that skip Step 2 aren't broken — they just see a warning
  instead of a padlock. `http://homeserver/` (Tailscale/mDNS) and
  `http://<lan-ip>/` keep working exactly as before, untouched by any
  of this.

---

## Optional: also get the bare address `https://home`

Not yet done — pending a decision, since it has a real tradeoff.

`homeserver.local` above already solves "works automatically for
guests, no router changes" — the only thing bare `home` adds is
dropping the `.local` suffix. Getting there needs something on the LAN
to answer "home" with `192.168.0.22`, and this router (a "Three" hub,
ZTE MC888 firmware) has **no DNS configuration of any kind** in its
admin UI — confirmed by checking the LAN/DHCP page and every other
settings tab (DDNS, SNTP, NAT, SIP ALG, VPN Client, DMZ, UPnP, Domain
Filtering, Port Forwarding, Port Filtering — none of these are DNS).
This is a known limitation; the Three community works around it the
same way Pi-hole users on this exact hub do: disable the router's
built-in DHCP server and run your own.

The plan, if you want to proceed:

1. On the homeserver: extend the existing `dnsmasq` (already running
   for DNS) to also run as the LAN's DHCP server — same IP pool
   (`192.168.0.2`–`192.168.0.253`, minus `.22` for itself), same
   24h lease time, gateway `192.168.0.1` (the router still does
   NAT/routing), DNS `192.168.0.22` (itself).
2. On the router: **Router** page → set **DHCP server** to
   **Disabled** → Save.
3. Reconnect devices (or wait for lease renewal) to pick up the new
   DHCP server.

Trade-off: existing/connected devices are unaffected if the
homeserver is ever off (leases last 24h). A **brand-new device**
joining Wi-Fi during an outage won't get an IP address at all until
the homeserver's back — the router's own DHCP is disabled, so there's
no fallback. Given `homeserver.local` already covers the "no setup for
guests" requirement, this is now a smaller win for a real
availability trade-off — say the word if you still want it.
