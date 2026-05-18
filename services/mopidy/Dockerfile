# Mopidy + Iris web player with Spotify, YouTube, and Local backends.
#
# Built on the official mopidy/mopidy image (the popular wernight/mopidy
# image has been stale since ~2020). We pip-install extensions on top so
# we get the latest fixes for Spotify auth (librespot) and YouTube
# (youtube-dl successor yt-dlp via Mopidy-YouTube[Music]).
FROM mopidy/mopidy:latest

USER root

# System deps: gstreamer ALSA sink + Spotify backend native deps + python3-pip.
# mopidy/mopidy already ships GStreamer plugins but we make sure the ALSA
# output sink is present and pip is usable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gstreamer1.0-alsa \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-good \
        python3-pip \
        alsa-utils \
    && rm -rf /var/lib/apt/lists/*

# Mopidy extensions. We use --break-system-packages because the upstream
# image's Python env is externally-managed (PEP 668) and Mopidy itself
# lives in the same site-packages.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        Mopidy-Iris \
        Mopidy-Spotify \
        Mopidy-Local \
        Mopidy-YouTube \
        yt-dlp

# Iris wants to run a couple of system commands (scan, restart) via sudo.
# The base image runs as the `mopidy` user; grant the narrow sudo rule
# Iris documents so its "Restart server" / "Local scan" buttons work.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sudo \
    && rm -rf /var/lib/apt/lists/* \
    && echo "mopidy ALL=NOPASSWD: /usr/local/lib/python3.*/dist-packages/mopidy_iris/system.sh" \
        > /etc/sudoers.d/mopidy-iris

USER mopidy

EXPOSE 6680
