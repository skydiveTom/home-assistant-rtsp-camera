# Changelog

All notable changes to this add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-23

### Added

- Add camera instances using only the stream URL (RTSP, RTSPS, RTMP, HTTP MJPEG),
  with a name, RTSP transport and enabled flag.
- **Test stream** button using `ffprobe`: codec, resolution, frame rate, bit rate
  and the raw error message, also for URLs that are not saved yet.
- **Quick preview** inside the add-on: MJPEG for every browser and HLS with stream
  copy for H.264 cameras (low CPU).
- Bundled Home Assistant integration that turns every instance into a `camera`
  entity with native Home Assistant streaming.
- Automatic installation and update of the integration into
  `/config/custom_components/rtsp_cameras` plus a one-click Home Assistant restart
  (optional automatic restart with `ha_restart_after_install`).
- Periodic stream health checks with a live status per camera.
- Web interface in English, German, Spanish and Polish; it follows the language of
  Home Assistant and falls back to English.
- Snapshot endpoint for still images and lazy loaded camera thumbnails.
- Credential masking in the interface and in every log message.
- Test suite covering the API, the storage, the ffmpeg layer, the translations and
  the repository consistency.
