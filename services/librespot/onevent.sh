#!/bin/sh
# librespot --onevent hook: forward each player event to mediacast so the
# portal / Android app can show Spotify now-playing (state.spotify).
#
# librespot (0.8 / pinned dev rev) passes the event in env vars: PLAYER_EVENT always; then
# depending on the event TRACK_ID, NAME, ARTISTS / COVERS (newline-
# separated), ALBUM, DURATION_MS, POSITION_MS, VOLUME (volume_changed, 0-65535). librespot runs on the host
# network, so mediacast's published port is reachable on loopback.
# Best-effort: never block or fail librespot over this.

[ -n "${MEDIACAST_TOKEN:-}" ] || exit 0
URL="${MEDIACAST_EVENT_URL:-http://127.0.0.1:8765/api/v1/spotify/event}"

num() { case "$1" in ''|*[!0-9]*) echo null ;; *) echo "$1" ;; esac; }

body=$(jq -n \
    --arg event "${PLAYER_EVENT:-}" \
    --arg track_id "${TRACK_ID:-}" \
    --arg name "${NAME:-}" \
    --arg artists "${ARTISTS:-}" \
    --arg album "${ALBUM:-}" \
    --arg covers "${COVERS:-}" \
    --argjson duration_ms "$(num "${DURATION_MS:-}")" \
    --argjson position_ms "$(num "${POSITION_MS:-}")" \
    --argjson volume "$(num "${VOLUME:-}")" \
    '{event:$event, track_id:$track_id, name:$name, artists:$artists,
      album:$album, covers:$covers, duration_ms:$duration_ms,
      position_ms:$position_ms, volume:$volume}
     | with_entries(select(.value != "" and .value != null))') || exit 0

curl -fsS --max-time 3 -o /dev/null \
    -H "Authorization: Bearer $MEDIACAST_TOKEN" \
    -H "Content-Type: application/json" \
    --data "$body" "$URL" 2>/dev/null || true
exit 0
