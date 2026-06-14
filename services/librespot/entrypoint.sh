#!/bin/sh
# librespot container entrypoint.
#
# Spawns a background keepalive that opens a throwaway TCP connection
# to Spotify's apresolve endpoint every few minutes, then exec's into
# librespot so signals (and PID 1 status) belong to it.
#
# Why: librespot 0.8.0 has been observed to fail its very first spirc
# init after a long zeroconf-idle period ("Service unavailable
# { client error (Connect) }") and exit with rc=1. Docker's
# restart-unless-stopped loop recovers automatically, but the user-
# visible "Connecting..." in the Spotify app times out (~30s) before
# the second attempt is ready, so the user has to manually retry. We
# can't reach into librespot's reqwest connection pool from outside,
# but we can keep the OS-level egress path (DNS resolver cache,
# NordVPN WireGuard tunnel state, NAT conntrack entries) warm so
# librespot's on-demand connect isn't a cold start.
#
# On librespot exit the container exits; the keepalive is in the
# same PID namespace so the kernel reaps it. A fresh restart re-runs
# this script and re-spawns the keepalive.

set -u

LIBRESPOT=/usr/local/bin/librespot
KEEPALIVE_HOST=apresolve.spotify.com
KEEPALIVE_PORT=443
KEEPALIVE_INTERVAL=240   # 4 min — under typical NAT conntrack TCP
                         # idle timeout (5 min) and DNS TTLs.

(
    # Delay the first probe so librespot's startup logs aren't
    # interleaved with ours, and so we don't hammer the network
    # before the box's own network stack is fully ready post-boot.
    sleep 15
    while true; do
        if ! nc -z -w 5 "$KEEPALIVE_HOST" "$KEEPALIVE_PORT" 2>/dev/null; then
            # Only log failures — success-spam would bury librespot's
            # own logs. A streak of failures here is a signal that
            # NordVPN egress is down, not just that librespot will
            # fail later.
            echo "librespot-keepalive: $KEEPALIVE_HOST:$KEEPALIVE_PORT probe failed"
        fi
        sleep "$KEEPALIVE_INTERVAL"
    done
) &

exec "$LIBRESPOT" "$@"
