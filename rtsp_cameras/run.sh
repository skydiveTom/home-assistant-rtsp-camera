#!/usr/bin/env bash
set -euo pipefail

bashio::log.info "Starting RTSP Camera Manager $(bashio::addon.version)"

cd /app
export PYTHONUNBUFFERED=1
exec python3 -m app