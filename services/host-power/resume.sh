#!/bin/sh
# Post-resume fixups (installed by bootstrap.sh install_host_power_policy as
# /usr/local/sbin/homeserver-resume, run by homeserver-resume.service after
# every suspend). Only the mediacast night scheduler / deep_sleep API
# suspends this box; on resume make sure Wi-Fi comes back without power
# save and give librespot (Spotify Connect) a fresh session — its sockets
# and zeroconf announcement don't reliably survive a suspend.
# mediacast-host notices the resume itself (CLOCK_BOOTTIME jump).
iw dev wlp4s0 set power_save off 2>/dev/null
# Wait (≤90 s) for a default route + gateway answer first, so librespot
# doesn't start into a dead network.
i=0
while [ $i -lt 45 ]; do
    gw=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
    [ -n "$gw" ] && ping -c1 -W1 "$gw" >/dev/null 2>&1 && break
    i=$((i + 1)); sleep 2
done
echo "homeserver-resume: network up after $i checks; restarting librespot"
docker restart librespot >/dev/null 2>&1 || echo "homeserver-resume: librespot restart failed"
