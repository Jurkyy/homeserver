#!/bin/bash
# Wi-Fi link watchdog (installed by bootstrap.sh install_host_power_policy).
#
# Why: this box is Wi-Fi only (Intel 7260, iwlwifi). On 2026-10-24 21:09 the
# link silently stopped passing traffic — still associated, no
# NetworkManager/wpa_supplicant event at all — DNS to the router timed out,
# librespot couldn't fetch the next track and the LAN couldn't reach the box
# until a reboot. NetworkManager only reacts to a *disassociation*, so this
# pings the default gateway and, when it stops answering, re-activates the
# connection (then, if that doesn't help, reloads the driver).
set -u
IFACE="${WIFI_WATCHDOG_IFACE:-wlp4s0}"
INTERVAL="${WIFI_WATCHDOG_INTERVAL:-20}"
FAILS_RECONNECT="${WIFI_WATCHDOG_FAILS_RECONNECT:-3}"   # ~1 min of dead link
FAILS_RELOAD="${WIFI_WATCHDOG_FAILS_RELOAD:-9}"         # ~3 min

fails=0
log() { echo "wifi-watchdog: $*"; }

while true; do
    sleep "$INTERVAL"
    gw=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
    if [ -z "$gw" ]; then
        # No default route on the Wi-Fi yet (resume / NM still connecting).
        fails=$((fails + 1))
    elif ping -c 3 -i 0.3 -W 2 -I "$IFACE" "$gw" >/dev/null 2>&1; then
        [ "$fails" -gt 0 ] && log "gateway $gw reachable again after $fails failed checks"
        fails=0
        continue
    else
        fails=$((fails + 1))
        log "gateway ${gw} unreachable (check $fails)"
    fi
    # Power save off is cheap to re-assert and a known iwlwifi stall trigger.
    iw dev "$IFACE" set power_save off 2>/dev/null || true
    if [ "$fails" -eq "$FAILS_RECONNECT" ]; then
        log "re-activating $IFACE via NetworkManager"
        nmcli device connect "$IFACE" || log "nmcli device connect failed"
    elif [ "$fails" -ge "$FAILS_RELOAD" ]; then
        log "still dead — reloading iwlwifi driver"
        modprobe -r iwlmvm iwlwifi && modprobe iwlwifi || log "driver reload failed"
        fails=$FAILS_RECONNECT   # next reload only after another ~2 min
    fi
done
