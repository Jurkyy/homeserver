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
# linker.
RUN apk add --no-cache \
        alsa-lib-dev \
        musl-dev \
        pkgconfig

# Rust on the musl target defaults to producing a fully static-pie
# binary, which then expects libasound.a — but Alpine doesn't ship
# alsa-lib-static. Flip crt-static off so we get a dynamically-linked
# musl binary that uses libasound.so instead (which we install in the
# runtime stage).
ENV RUSTFLAGS="-C target-feature=-crt-static"

# --no-default-features strips the pulseaudio / rodio / portaudio /
# jackaudio backends we don't use (and the extra system deps they
# would drag in). Then we add back the features we *do* need:
#
#   alsa-backend             — audio out through libasound to the
#                              Focusrite Scarlett 2i2.
#   with-libmdns             — pure-Rust mDNS responder so the
#                              Spotify app on the LAN can discover
#                              the device. We deliberately don't
#                              run avahi-daemon on the host alongside
#                              this: two mDNS responders racing on
#                              UDP 5353 means neither answers
#                              _spotify-connect._tcp queries
#                              reliably, and avahi refused to publish
#                              on our wlp4s0 wifi interface anyway
#                              (operstate DORMANT trips its filter).
#                              See bootstrap.sh — install_avahi has
#                              been removed and a route carve-out
#                              for multicast on the LAN interface
#                              keeps NordVPN from swallowing it.
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
        --features alsa-backend,with-libmdns,rustls-tls-webpki-roots


FROM alpine:3.20

# alsa-lib: runtime shared lib for the alsa backend.
# alsa-utils: lets us `aplay -l` inside the container when debugging
#   which sound card actually got mapped in.
# ca-certificates: librespot talks to Spotify's API over TLS.
# libgcc: provides libgcc_s.so.1 — Rust's panic-unwind path links
#   against it. Without this the binary fails to load with
#   "Error relocating ... _Unwind_*: symbol not found".
RUN apk add --no-cache \
        alsa-lib \
        alsa-utils \
        ca-certificates \
        libgcc \
    && adduser -D -u 1000 librespot

COPY --from=builder /usr/local/cargo/bin/librespot /usr/local/bin/librespot

# Access to /dev/snd comes from the host audio GID injected at runtime
# via docker-compose's group_add (default 29 on Debian). The librespot
# user here has no primary audio group on purpose — relying on
# group_add keeps the image portable across host distros with
# different audio GIDs.
USER librespot

ENTRYPOINT ["/usr/local/bin/librespot"]
