#!/usr/bin/env bash
set -euo pipefail

bashio::log.info "Starting RTSP Camera Manager $(bashio::addon.version 2>/dev/null || echo unknown)"

cd /app
export PYTHONUNBUFFERED=1
exec python3 -m app