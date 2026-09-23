ARG BUILD_VERSION
ARG BUILD_ARCH

FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24

ARG BUILD_VERSION="0.1.0"
ARG BUILD_ARCH="amd64"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg/ffprobe are used to test, snapshot and preview the RTSP streams
RUN apk add --no-cache --update ffmpeg

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --requirement /app/requirements.txt

COPY app /app/app

# Shipped copy of the Home Assistant integration, installed into /config at runtime
COPY custom_components /opt/rtsp_camera_integration/custom_components

COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
    io.hass.name="RTSP Camera Manager" \
    io.hass.description="Add RTSP cameras to Home Assistant using only the stream URL" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.source="https://github.com/skydiveTom/home-assistant-rtsp-camera" \
    org.opencontainers.image.licenses="GPL-3.0"

CMD ["bashio", "/run.sh"]