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
# for the --features alsa-backend code path; musl-dev + pkgconfig are
# what the cargo build script expects to find a C compiler / linker.
RUN apk add --no-cache \
        alsa-lib-dev \
        musl-dev \
        pkgconfig

# --no-default-features strips the pulseaudio / rodio / portaudio /
# jackaudio backends we don't use (and that pull in extra system deps).
# --features alsa-backend keeps the one backend that reaches the
# Focusrite Scarlett 2i2 via the host's /dev/snd.
RUN cargo install librespot \
        --no-default-features \
        --features alsa-backend


FROM alpine:3.20

# alsa-lib: runtime shared lib for the alsa backend.
# alsa-utils: lets us `aplay -l` inside the container when debugging
#   which sound card actually got mapped in.
# ca-certificates: librespot talks to Spotify's API over TLS.
RUN apk add --no-cache \
        alsa-lib \
        alsa-utils \
        ca-certificates \
    && adduser -D -u 1000 librespot

COPY --from=builder /usr/local/cargo/bin/librespot /usr/local/bin/librespot

# Access to /dev/snd comes from the host audio GID injected at runtime
# via docker-compose's group_add (default 29 on Debian). The librespot
# user here has no primary audio group on purpose — relying on
# group_add keeps the image portable across host distros with
# different audio GIDs.
USER librespot

ENTRYPOINT ["/usr/local/bin/librespot"]
