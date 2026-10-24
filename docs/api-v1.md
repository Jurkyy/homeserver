# mediacast API v1

Contract between the mediacast backend (`services/mediacast`, `services/mediacast-host`)
and its clients (Android app in `android/`, the web portal at `/`).
The backend owns this file; any change must be reflected in both clients.

## Base URL & transport

- `http://homeserver.local` or `http://192.168.0.22` (Caddy `:80` → mediacast:8765).
- All routes live under `/api/v1`. JSON in, JSON out. Errors: `{"error": "<human message>", "code": "<slug>"}` with a 4xx/5xx status (also for validation errors).

## Auth

- Every `/api/v1/*` route requires `Authorization: Bearer <token>`, **except**:
  `GET /api/v1/health`, `GET /api/v1/artwork/...`, `GET /api/v1/jellyfin/image/...` (images are harmless and loaded by image libraries).
- Token = `MEDIACAST_TOKEN` from `.env`. The web portal at `/` gets it injected into the page and sends it as a header
  (a custom header forces a CORS preflight, which the server does not allow → blocks CSRF from other sites).
- No CORS headers are sent.
- **Pairing:** the web portal has a "Pair phone" section showing a QR code encoding
  `mediacast://pair?url=<url-encoded base url>&token=<token>`. The app also accepts manual entry of URL + token.

## State

`GET /api/v1/state` → `State`

`GET /api/v1/events` → Server-Sent Events. Sends `event: state` + `data: <State JSON>` immediately on connect and
on every change; a `: ping` comment every 15 s. Clients reconnect with backoff.
It also carries `event: feed` (see [YouTube](#youtube)); clients must ignore event types they don't know.

```jsonc
State = {
  "power": "on" | "sleeping",
  "screen": "on" | "off",
  "activity": "idle" | "playing" | "paused" | "screensaver",
  "source": "youtube" | "jellyfin" | "url" | "screensaver" | null,   // what is on the projector
  "title": string | null,
  "subtitle": string | null,          // channel / "Series · S1E3" / site
  "artwork_url": string | null,       // relative ("/api/v1/...") or absolute; no auth needed
  "position_s": number | null,
  "duration_s": number | null,
  "volume": number,                   // 0-100, cast volume (not Spotify)
  "muted": boolean,
  "item": { "kind": "youtube" | "jellyfin" | "url", "id": string } | null,
  "screensaver": {
    "active": boolean,
    "theme": string | null,
    "themes": [{ "id": string, "name": string }],
    "idle_minutes": number            // idle time before auto screensaver
  },
  "spotify": {                        // librespot Connect receiver "Homeserver"; null if never seen
    "active": boolean,                // a client is connected / session alive
    "playing": boolean,
    "title": string | null, "artist": string | null, "album": string | null,
    "artwork_url": string | null,
    "position_s": number | null, "duration_s": number | null,
    "updated_at": number              // unix ms of last librespot event
  } | null,
  "night": {                          // overnight deep sleep (real suspend); null/absent on older hosts
    "enabled": boolean,
    "start": string, "end": string,   // local "HH:MM", e.g. "23:00" / "08:00"
    "in_window": boolean,             // local time is inside start..end right now
    "pending": boolean,               // bedtime reached but postponed (busy / cancelled) — will re-check
    "next_check_at": number | null,   // unix ms of the next idle check (null = none scheduled)
    "countdown_until": number | null  // unix ms: a "going to sleep" countdown is running and suspends then
  } | null,
  "updated_at": number                // unix ms
}
```

Notes:
- While something plays, the backend pushes a new state roughly once a second (position changes). `position_s` is as of
  the push; clients may tick it locally between pushes.
- `spotify.position_s` is as of `spotify.updated_at` (librespot only reports on play/pause/seek/track change):
  extrapolate `position_s + (now - updated_at)/1000` while `playing`.
- `screen` reflects the real DPMS state, so it also reads `"off"` when X blanked the display on its own idle timer.
- If the host helper is unreachable, `GET /state` returns 502 `host_unreachable`.

## Playback

- `POST /api/v1/cast` `{ "url": string }` → `{ "ok": true }` — YouTube / any URL to the projector.
- `POST /api/v1/control` `{ "action": "toggle"|"play"|"pause"|"stop"|"seek"|"seek_to"|"volume"|"volume_delta", "value"?: number }`
  → `{ "ok": true }`. `seek`/`volume_delta` are relative (s / percent points), `seek_to` absolute seconds, `volume` 0-100.
  Controls the cast player only; Spotify is controlled from the Spotify app.
  Missing `value` for a value action → 400 `missing_value`; nothing on the projector → 409 `no_active_cast`
  (`stop` and `volume*` never 409: stopping nothing succeeds, volume is remembered for the next cast).
- `POST /api/v1/screensaver` `{ "theme": string | null }` → `{ "ok": true }` — `null` stops it.

## Power

`POST /api/v1/power` `{ "action": "sleep" | "wake" | "screen_off" | "deep_sleep" | "bedtime_cancel" | "poweroff" }` → `{ "ok": true }`

- `sleep` (soft sleep): stop playback, stop screensaver, DPMS off, suspend the idle-screensaver watchdog, `power = "sleeping"`.
- Wake happens on: `wake`, any cast / screensaver / jellyfin play, physical mouse/keyboard input on the server,
  or the projector being (re)connected / switched on (HDMI hotplug). Wake = DPMS on, `power = "on"`, `screen = "on"`, idle timer restarts.
- `screen_off`: stops playback and turns the display off (DPMS) but leaves `power = "on"` (kept for compatibility);
  cleared by the same wake triggers as sleep.
- Soft sleep is also entered automatically once the screensaver has run `SLEEP_AFTER_SCREENSAVER_MIN` minutes (default 180).
- `poweroff`: shuts the machine down after ~1.5 s.
- `deep_sleep`: real system suspend (S3) ~2 s after the response, with an RTC wake alarm at the next `night.end`.
  While suspended the box is unreachable (Spotify Connect, this API, SSH). It comes back on the alarm, a mouse /
  keyboard event (the USB mouse has wakeup enabled) or the power button. A user wake comes back `power = "on"`;
  the RTC wake at `night.end` comes back soft-sleeping (`power = "sleeping"`, projector stays dark).
- `bedtime_cancel`: cancel a running bedtime countdown and postpone tonight's deep sleep to the next hourly check
  (`night.pending` stays true, `night.next_check_at` moves). No-op outside the night window.

### Night deep sleep

Configured on the host helper (`NIGHT_DEEP_SLEEP=1`, `NIGHT_START=23:00`, `NIGHT_END=08:00`, `NIGHT_RECHECK_MIN=60`).

- At `NIGHT_START` the host checks whether the box is idle: no cast playing or paused with content (the screensaver
  does not count) and Spotify not playing. Soft sleep counts as idle.
- Idle → a 60 s countdown (`night.countdown_until`) with an on-projector overlay "Going to sleep — move the mouse or
  press Wake to cancel", then `deep_sleep`. Mouse/keyboard input, `wake`, `bedtime_cancel` or any cast cancels it and
  postpones to the next check.
- Busy → a short "Bedtime 🌙 — I'll sleep once you're done" overlay over what's playing, `night.pending = true`, and a
  re-check every `NIGHT_RECHECK_MIN` minutes until idle.
- Resuming inside the window (someone moved the mouse) re-arms the same flow after the normal idle period, never instantly.
- The idle screensaver never auto-starts inside the night window.

## YouTube

`GET /api/v1/youtube/feed?refresh=0|1&shorts=0|1` (default `shorts=1`: Shorts included, flagged by `is_short`; `shorts=0` drops them) →
`{ "items": [ { "id", "url", "title", "channel", "channel_id", "published" (ISO 8601), "thumbnail", "duration_s"?: number|null, "is_short": boolean } ], "updated_at": number, "changed_at": number }`
— `updated_at`: unix ms of the last successful rebuild; `changed_at`: unix ms of the last time the set of videos changed (0 = unknown).

The server rebuilds the feed in the background every `YT_FEED_REFRESH_MIN` minutes (default 5), so clients never need
to poll. `refresh=1` rebuilds now (or waits for an in-flight rebuild instead of starting a second one); channels whose
RSS copy is still within YouTube's `max-age` (~15 min) are served from cache.

When a rebuild changes the set of videos, `GET /api/v1/events` emits

```
event: feed
data: {"updated_at": number, "new_count": number, "removed_count": number}
```

`updated_at` equals the feed's `changed_at`. The same event (counts 0) is sent once right after connect, so a
(re)connecting client can compare it with the `changed_at` of the feed it holds and refetch only if it advanced.
Clients should refetch silently and compute "N new" themselves (ids not seen before).

## Jellyfin (mrpflix)

`JfItem = { "id", "name", "type" ("Movie"|"Series"|"Season"|"Episode"|"CollectionFolder"|...), "series_name"?, "season"?: number, "episode"?: number,
"year"?: number, "overview"?: string, "runtime_s"?: number, "played": boolean, "played_pct"?: number, "resume_s"?: number,
"image_url": string (relative, no auth), "backdrop_url"?: string, "child_count"?: number }`

- `GET /api/v1/jellyfin/views` → `{ "items": JfItem[] }` (libraries)
- `GET /api/v1/jellyfin/items?parent_id=&search=&start=0&limit=60&types=Movie,Series` → `{ "items": JfItem[], "total": number }`
- `GET /api/v1/jellyfin/resume` → `{ "items": JfItem[] }` (continue watching)
- `GET /api/v1/jellyfin/nextup` → `{ "items": JfItem[] }`
- `GET /api/v1/jellyfin/item/{id}` → `JfItem & { "children"?: JfItem[] }` (series → seasons, season → episodes)
- `GET /api/v1/jellyfin/image/{id}?type=Primary|Backdrop&max_width=600` → image bytes
- `POST /api/v1/jellyfin/play` `{ "id": string, "resume": boolean, "low_quality"?: boolean }` → `{ "ok": true }`.
  `resume` starts at the item's saved position; `low_quality` caps the stream bitrate (~1.2 Mbps) for a starved link.
  Backend reports playback start/progress/stop to Jellyfin so resume and "next up" stay correct.

## Internal

- `POST /api/v1/spotify/event` (token) — librespot's `--onevent` hook posts
  `{ "event", "track_id"?, "name"?, "artists"? (newline-separated), "album"?, "covers"? (newline-separated URLs), "duration_ms"?, "position_ms"? }`.
  Not for clients.

## Health

`GET /api/v1/health` → `{ "ok": true, "version": string }`
