# Homeserver for Android

Native remote for the projector media server (mediacast API v1, see `../docs/api-v1.md`).
Kotlin + Jetpack Compose (Material 3), package `dev.woto.homeserver`.

## Features
- **Remote** — live state over SSE (auto-reconnect), artwork with blurred backdrop, seek bar with local
  interpolation, play/pause, ±10 s / ±30 s, stop, volume; hardware volume keys change projector volume while
  the app is open; Spotify Connect now-playing card ("Open" jumps to Spotify); screensaver themes; Sleep/Wake,
  Power off (confirmed); connection indicator.
- **YouTube** — subscriptions feed, pull-to-refresh, search, channel filter, hide Shorts, tap to cast.
- **Movies (mrpflix / Jellyfin)** — continue watching, next up, library rows, search, detail with
  Play/Resume, series → seasons → episodes with watched/progress indicators, optional low-bandwidth playback.
- **Share target** — share any link (e.g. from the YouTube app) to "Cast to projector"; it casts and toasts
  without opening the UI.
- **Media notification / lock screen** — media3 session mirroring the projector (play/pause/seek/stop, volume
  rocker on the lock screen). Toggle in Settings; stops when idle or sleeping.
- **Quick Settings tile** (Projector sleep/wake) and a **home-screen widget** (now playing + play/pause).

## Build
Requires JDK 21 (mise: `java = "temurin-21"`, see `mise.toml`) and the Android SDK (platform 36).

```sh
cd android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties   # once
./gradlew testDebugUnitTest assembleDebug lint
```

APK: `app/build/outputs/apk/debug/app-debug.apk` (`./gradlew assembleRelease` gives a minified APK signed
with the debug key: `app/build/outputs/apk/release/app-release.apk`).

## Install
```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
(or copy the APK to the phone and open it, allowing installs from unknown sources).

## Pairing
1. Open the web portal (`http://homeserver.local` or `http://192.168.0.22`) and choose **Pair phone**.
2. In the app tap **Scan pairing QR code** (QR = `mediacast://pair?url=…&token=…`). Tapping such a link on the
   phone works too.
3. Or enter the URL and token (`MEDIACAST_TOKEN` from the server's `.env`) manually.

The app checks `/api/v1/health`, then an authenticated `/api/v1/state`, and stores both in DataStore.
Re-pair or forget the token in **Settings**. Cleartext HTTP is allowed (LAN only) and user-installed CAs
are trusted (Caddy internal CA for `https://home`).

## Tests
- Unit tests: JSON models, SSE parsing, pairing URI, API client (MockWebServer).
- `ScreenshotTest` renders screens with Robolectric to `app/build/shots/` for visual review.
- Live read-only smoke test against a real server:
  `MEDIACAST_URL=http://192.168.0.22 MEDIACAST_TOKEN=… ./gradlew testDebugUnitTest --tests '*LiveSmokeTest*'`
