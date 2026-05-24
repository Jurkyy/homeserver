# Mopidy + Iris web player with Spotify, YouTube, and Local backends.
#
# Built from debian:trixie-slim because the upstream mopidy/mopidy
# Docker image was deprecated by the Mopidy team — pulls now 401 with
# "insufficient_scope: authorization failed". This gives us a known-good
# Debian base where apt provides the GStreamer audio pipeline that
# Mopidy expects.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# System deps:
#  - python3 + pip + python3-gst-1.0: Mopidy is Python; it speaks to
#    GStreamer through gst-python.
#  - gstreamer1.0-{alsa,plugins-{base,good,bad,ugly}}: audio pipeline —
#    ALSA sink for the Scarlett, plus MP3 / AAC / Opus decoders.
#  - alsa-utils: `aplay -l` inside the container when debugging which
#    card is visible.
#  - sudo + ca-certificates: Iris's restart hook + TLS for pip / Spotify.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        alsa-utils \
        ca-certificates \
        gstreamer1.0-alsa \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-tools \
        python3 \
        python3-gst-1.0 \
        python3-pip \
        sudo \
    && rm -rf /var/lib/apt/lists/*

# --break-system-packages because Debian's system Python is PEP 668
# externally-managed; safe to override inside a single-purpose container.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        Mopidy \
        Mopidy-Iris \
        Mopidy-Spotify \
        Mopidy-Local \
        Mopidy-YouTube \
        yt-dlp

# UID 105 matches Debian's mopidy package user; -G audio aligns with the
# host audio GID that docker-compose also adds via group_add.
RUN useradd -r -u 105 -m -d /var/lib/mopidy -s /usr/sbin/nologin -G audio mopidy \
    && mkdir -p /etc/mopidy /var/lib/mopidy/.cache /var/lib/mopidy/playlists \
    && chown -R mopidy:audio /var/lib/mopidy /etc/mopidy

# Narrow sudo rule for Iris's "Restart server" / "Local scan" buttons.
RUN echo "mopidy ALL=NOPASSWD: /usr/local/lib/python3.*/dist-packages/mopidy_iris/system.sh" \
        > /etc/sudoers.d/mopidy-iris

USER mopidy
EXPOSE 6680
CMD ["mopidy", "--config", "/etc/mopidy/mopidy.conf"]
