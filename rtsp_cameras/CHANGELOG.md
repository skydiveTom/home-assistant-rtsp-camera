# Changelog

All notable changes to this add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] - 2026-09-23

### Added

- Prebuilt, multi-arch app image published to the GitHub Container Registry
  (`ghcr.io/skydiveTom/rtsp-cameras`) by the new *Build add-on image* workflow.
  Supervisor now pulls the image instead of building it on your Home Assistant
  host, so installing and updating takes seconds instead of minutes.
- Repository tests that keep the workflow and the manifest in sync: the published
  image name must match the `image` key of the manifest, the image is pushed as a
  multi-arch manifest and the add-on version is always published as an image tag.

### Changed

- The build workflow uses the current Home Assistant builder actions
  (`home-assistant/builder/actions/*` 2026.09.0) with Docker BuildKit, including
  an ARM runner for the `aarch64` image.

## [0.1.3] - 2026-09-23

### Fixed

- The add-on container failed to start with
  `s6-overlay-suexec: fatal: can only run as pid 1`. The manifest did not declare
  `init: false`, so Supervisor placed Docker's own init (tini) in front of the
  s6-overlay init that ships with the Home Assistant base images - and that init
  must run as PID 1.
- The start script no longer aborts when `bashio` cannot read the add-on version.

### Changed

- A test now guards the `init: false` flag so the manifest cannot regress.

## [0.1.2] - 2026-09-23

### Changed

- The integration setup now tells you where cameras are added: in the add-on
  panel (*RTSP Cameras* → *Add camera*). The integration itself only reads the
  JSON file the add-on writes.
- Every camera device page links to the *Adding a camera* chapter of the
  documentation.
- A test now guarantees that `translations/en.json` (the file Home Assistant
  loads) always matches the English source `strings.json`.

## [0.1.1] - 2026-09-23

### Fixed

- Adding the integration no longer fails with *"the folder that should contain
  this file does not exist"* when the add-on has not created
  `rtsp_cameras/cameras.json` yet: the folder is created automatically and the
  file is picked up as soon as the add-on writes it.
- An unusable camera file path (empty, or pointing at an existing folder) is now
  reported with its own message, and a folder that cannot be created gets a
  separate, actionable error.

### Changed

- The camera file location is resolved by one shared helper, so the config flow,
  the options flow and the coordinator always agree on the same path.
- *"Camera file does not exist yet, waiting for the add-on"* is logged as an
  informational message instead of a warning.

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
