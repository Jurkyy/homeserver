# librespot — a Rust implementation of Spotify Connect. The container
# advertises itself as a Spotify Connect target via mDNS zeroconf; the
# user picks it from the device list inside the Spotify app (mobile or
# desktop) and audio streams here from Spotify's servers directly. No
# Spotify credentials are baked into the container — auth piggy-backs
# on the user's existing app session. Requires Spotify Premium.
#
# Two-stage build so we don't ship the Rust toolchain in the runtime
# image. We build statically against musl in the rust:alpine builder
# and copy only the resulting binary plus libasound into a tiny alpine
# runtime.
FROM rust:alpine AS builder

# alsa-lib-dev provides the libasound headers librespot links against
# for the --features alsa-backend code path; musl-dev + pkgconfig
# are what the cargo build script expects to find a C compiler /
# linker. avahi-dev brings libavahi-client headers for the with-dns-sd
# feature (librespot calls into the host's avahi-daemon over the
# unix bus instead of binding UDP 5353 itself). pulseaudio-dev provides
# the libpulse-simple headers + pkgconfig the pulseaudio-backend links
# against — output now goes through the host PulseAudio server (which
# owns the Scarlett 2i2) so it can mix with other PA streams (e.g. the
# turntable loopback) instead of grabbing the ALSA card exclusively.
RUN apk add --no-cache \
        alsa-lib-dev \
        avahi-dev \
        musl-dev \
        pkgconfig \
        pulseaudio-dev

# Rust on the musl target defaults to producing a fully static-pie
# binary, which then expects libasound.a — but Alpine doesn't ship
# alsa-lib-static. Flip crt-static off so we get a dynamically-linked
# musl binary that uses libasound.so instead (which we install in the
# runtime stage).
ENV RUSTFLAGS="-C target-feature=-crt-static"

# --no-default-features strips the rodio / portaudio / jackaudio
# backends we don't use (and the extra system deps they would drag
# in). Then we add back the features we *do* need:
#
#   pulseaudio-backend       — audio out through libpulse to the host
#                              PulseAudio server. This is the backend
#                              we actually run (--backend pulseaudio in
#                              compose): PA owns the Scarlett 2i2 and
#                              mixes Spotify with other PA streams (the
#                              turntable loopback) instead of the raw
#                              ALSA card being held exclusively by one
#                              source at a time.
#   alsa-backend             — kept as a fallback path (audio out
#                              through libasound directly to the card);
#                              not used while --backend pulseaudio is
#                              set, but cheap to retain.
#   with-avahi               — publish _spotify-connect._tcp via the
#                              host's avahi-daemon over D-Bus instead
#                              of librespot binding UDP 5353 itself.
#                              Letting avahi own 5353 means we can
#                              ALSO advertise the box's hostname.local
#                              via mDNS so LAN clients reach
#                              `http://homeserver.local/`. Earlier
#                              builds used with-libmdns (pure-Rust)
#                              and that race for 5353 forced us to
#                              remove avahi-daemon entirely — fixed
#                              now that one process owns the port.
#   rustls-tls-webpki-roots  — TLS for the Spotify control channel,
#                              backed by Rust's rustls plus the
#                              bundled webpki root certs. Picked
#                              over native-tls because it doesn't
#                              need openssl-dev at build time, and
#                              over rustls-tls-native-roots because
#                              we don't want to depend on the
#                              container's CA store.
#
# --locked uses librespot's published Cargo.lock. Without it cargo
# picks the latest semver-compatible version of every transitive dep
# at build time, which broke 0.8.0 against current vergen-lib (two
# resolved versions, trait impls didn't match → librespot-core build
# script wouldn't compile).
RUN cargo install librespot \
        --locked \
        --no-default-features \
        --features alsa-backend,pulseaudio-backend,with-avahi,rustls-tls-webpki-roots


FROM alpine:3.20

# alsa-lib: runtime shared lib for the alsa backend.
# alsa-utils: lets us `aplay -l` inside the container when debugging
#   which sound card actually got mapped in.
# ca-certificates: librespot talks to Spotify's API over TLS.
# libgcc: provides libgcc_s.so.1 — Rust's panic-unwind path links
#   against it. Without this the binary fails to load with
#   "Error relocating ... _Unwind_*: symbol not found".
# libpulse: runtime shared lib (libpulse-simple.so / libpulse.so) for
#   the pulseaudio backend — the client that talks to the host's
#   PulseAudio server over the bind-mounted unix socket.
RUN apk add --no-cache \
        alsa-lib \
        alsa-utils \
        avahi-libs \
        ca-certificates \
        dbus-libs \
        libgcc \
        libpulse \
    && adduser -D -u 1000 librespot

COPY --from=builder /usr/local/cargo/bin/librespot /usr/local/bin/librespot

# Entrypoint wrapper keeps the OS egress path warm with a periodic
# outbound probe so librespot's first user-triggered connect after a
# long zeroconf-idle isn't a cold start. See entrypoint.sh for the
# why; in short, librespot 0.8.0's reqwest client has been seen to
# fail its first spirc init with "Service unavailable" after the
# process has been idle, exiting and triggering a docker restart
# that the user perceives as a hung "Connecting..." in their Spotify
# app. The keepalive sidesteps that.
COPY entrypoint.sh /usr/local/bin/librespot-entrypoint.sh
RUN chmod +x /usr/local/bin/librespot-entrypoint.sh

# Access to /dev/snd comes from the host audio GID injected at runtime
# via docker-compose's group_add (default 29 on Debian). The librespot
# user here has no primary audio group on purpose — relying on
# group_add keeps the image portable across host distros with
# different audio GIDs.
USER librespot

ENTRYPOINT ["/usr/local/bin/librespot-entrypoint.sh"]
