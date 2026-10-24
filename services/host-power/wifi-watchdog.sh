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
#
# Second failure mode (2026-10-24, AP 5 GHz BSSID on DFS ch 120): unicast to
# the gateway worked but *no broadcast/multicast frame* from any other host
# arrived — phones' ARP for us never got through (LAN "Destination Host
# Unreachable"), mDNS / Spotify Connect discovery broke. The iwlwifi netdev
# multicast counter is always 0, so it can't be used; instead, every
# ~MCAST_EVERY checks we sniff for one broadcast/multicast frame not sent by
# us (a home LAN emits ARP/mDNS/SSDP every few seconds). Several silent
# windows in a row → re-activate the connection.
set -u
IFACE="${WIFI_WATCHDOG_IFACE:-wlp4s0}"
INTERVAL="${WIFI_WATCHDOG_INTERVAL:-20}"
FAILS_RECONNECT="${WIFI_WATCHDOG_FAILS_RECONNECT:-3}"   # ~1 min of dead link
FAILS_RELOAD="${WIFI_WATCHDOG_FAILS_RELOAD:-9}"         # ~3 min

MCAST_EVERY="${WIFI_WATCHDOG_MCAST_EVERY:-5}"            # ~every 100 s
MCAST_WINDOW="${WIFI_WATCHDOG_MCAST_WINDOW:-45}"        # s to wait for one frame
MCAST_FAILS="${WIFI_WATCHDOG_MCAST_FAILS:-3}"           # silent windows → reconnect

fails=0
mcast_fails=0
n=0
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
        n=$((n + 1))
        if [ $((n % MCAST_EVERY)) -eq 0 ] && command -v tcpdump >/dev/null 2>&1; then
            mac=$(cat "/sys/class/net/$IFACE/address" 2>/dev/null)
            if timeout "$MCAST_WINDOW" tcpdump -i "$IFACE" -c 1 -nn -q -p \
                    "(ether broadcast or ether multicast) and not ether src $mac" >/dev/null 2>&1; then
                [ "$mcast_fails" -gt 0 ] && log "broadcast/multicast frames arriving again"
                mcast_fails=0
            else
                mcast_fails=$((mcast_fails + 1))
                log "no broadcast/multicast frame from the LAN in ${MCAST_WINDOW}s ($mcast_fails/$MCAST_FAILS)"
                if [ "$mcast_fails" -ge "$MCAST_FAILS" ]; then
                    log "broadcast path dead — re-activating $IFACE via NetworkManager"
                    nmcli device connect "$IFACE" || log "nmcli device connect failed"
                    mcast_fails=0
                fi
            fi
        fi
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
