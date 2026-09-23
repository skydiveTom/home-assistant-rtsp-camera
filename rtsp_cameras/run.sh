#!/usr/bin/env bash
set -euo pipefail

cd /app
export PYTHONUNBUFFERED=1

# The Home Assistant base images ship bashio as a wrapper (CMD ["bashio", "/run.sh"])
# which loads the library before this script runs. Fall back to plain output when
# the script is started directly (for example with docker exec).
log() {
    if declare -F bashio::log.info >/dev/null 2>&1; then
        bashio::log.info "$1"
    else
        echo "[rtsp_cameras] $1"
    fi
}

version="unknown"
if declare -F bashio::addon.version >/dev/null 2>&1; then
    version="$(bashio::addon.version 2>/dev/null || echo unknown)"
fi

log "Starting RTSP Camera Manager ${version}"

exec python3 -m app