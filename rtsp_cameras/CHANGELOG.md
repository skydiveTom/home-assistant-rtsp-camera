# Changelog

All notable changes to this add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.8] - 2026-09-24

### Added

- **The preview mode is detected automatically.** The new default `auto` tests
  MJPEG first and HLS second when you open a preview and keeps the mode that
  works for that camera - only that one is offered from then on. The result is
  stored per camera and survives a restart; `mjpeg` and `hls` still work as
  explicit overrides. If MJPEG works inside the add-on but its frames never
  reach the browser (a buffering reverse proxy), the interface falls back to HLS
  on its own.
- **Stream URL for Home Assistant.** Every camera can carry a second URL, for
  example the H.264 sub stream of an H.265 camera. Home Assistant uses it for
  the camera entity, the add-on keeps testing and previewing the main URL.
- The add-on publishes the probed codec, and the integration logs a warning for
  streams that browsers cannot play (H.265/HEVC), because those camera cards
  stay black in most browsers.
- The integration offers **Diagnostics** (Settings → Devices & services → RTSP
  Camera Manager → Download diagnostics) with the watched file, the cameras and
  their (masked) URLs.

### Fixed

- Add-on options were ignored when the data folder was not the default `/data`
  (relevant for local runs and tests).

## [0.1.7] - 2026-09-23

### Fixed

- **Cameras now really appear in Home Assistant.** The camera entity derived
  from both `CoordinatorEntity` and `Camera`, but only the coordinator base
  class was initialised. `CoordinatorEntity.__init__` does not call `super()`,
  so the camera never got its access token, image cache and stream bookkeeping -
  Home Assistant discarded every camera while setting up the platform. The
  add-on panel showed streams and previews, but no `camera.*` entity existed.
- The data update coordinator is created with an explicit `config_entry=entry`.
  Relying on the implicit ContextVar is reported as an error by the 2026.9 core.

### Added

- `ha_tests/` runs the integration against a real Home Assistant core
  (`pytest-homeassistant-custom-component`, the same version CI pins) and checks
  that cameras published by the add-on become `camera` entities, that cameras
  added later show up without a restart and that deleted cameras disappear.
- Log messages now name the watched file, the number of cameras found and the
  entities that were registered, so a missing camera can be diagnosed from the
  Home Assistant log alone.

## [0.1.6] - 2026-09-23

### Fixed

- **Live previews failed on some ffmpeg builds with** `Option rw_timeout not
  found`, while the stream test worked (ffprobe ignores unknown options, ffmpeg
  refuses to start). `-rw_timeout` is no longer passed: RTSP inputs get
  `-timeout`, and every call is bounded on the Python side anyway.
- A failing MJPEG preview now shows the raw ffmpeg message in the preview window
  instead of only "the preview stopped before the first frame".

### Added

- A test that keeps `-rw_timeout` out of the command lines, and one that checks
  that ffmpeg's error text reaches the client.

## [0.1.5] - 2026-09-23

### Fixed

- **Stream tests failed with** `Failed to set value '-loglevel' for option
  'nostdin': Option not found`. FFmpeg 8 rejects `-nostdin` when other options
  follow it. The option is gone; the child processes now get `stdin=DEVNULL`,
  which has the same effect on every ffmpeg version.
- **Snapshots, the MJPEG preview and the HLS preview did not pass the stream URL
  to ffmpeg**, so ffmpeg had nothing to read. The input (`-i <url>`) is included
  now, and the test suite asserts the URL, the scale filter and the encoder flags
  on every command line.
- Pressing *Quick preview* for a camera that is not saved yet now says
  "Save the camera first to use the live preview" instead of a confusing
  "Enter the RTSP URL" message.

### Added

- Tests that inspect the real ffmpeg command lines (recorded by the fake
  binaries), so a missing input URL or a wrong option can no longer slip through.

## [0.1.4] - 2026-09-23

### Added

- Prebuilt, multi-arch app image published to the GitHub Container Registry
  (`ghcr.io/skydivetom/rtsp-cameras`) by the new *Build add-on image* workflow.
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
