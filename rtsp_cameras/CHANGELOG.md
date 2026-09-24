# Changelog

All notable changes to this add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.21] - 2026-09-24

### Fixed

- **Still images (camera card thumbnail, `camera.snapshot`, `camera/async_get_image`)
  returned nothing.** Home Assistant 2026.9 does not know
  `_attr_use_stream_for_stills` - `Camera.use_stream_for_stills` is a plain property
  that returns `False` - and the entity answered `async_camera_image` with `None`.
  Home Assistant therefore failed with `Unable to get image` even though live video
  worked. The entity now takes the still from its own live stream, waiting for the
  next **keyframe** (bounded to 8 s, then the last decoded frame is used) and uses
  an optional snapshot URL of the camera first, falling back to the stream when it
  does not answer.
- Found while testing against a real camera (H.264, 1280x720, 30 fps) on a live
  Home Assistant 2026.9: Home Assistant's own `stream` component decodes the
  camera and produces JPEG stills again.

### Added

- **Live camera tests** (`ha_tests/test_live_camera.py`). Export
  `RTSP_LIVE_CAMERA_URL` and the suite checks the whole chain against a real
  camera: the add-on file becomes `camera.<name>`, `stream_source` hands over the
  RTSP URL, the transport reaches `stream_options`, Home Assistant decodes a live
  frame and `camera.async_get_image` returns a JPEG. Without the variable the tests
  are skipped, so CI stays offline.
- Offline tests for the still path (`ha_tests/test_camera_stills.py`): keyframe
  wait, bounded timeout with fallback, snapshot URL priority and a broken snapshot
  URL.
- The test harness stub for `turbojpeg` now encodes real JPEGs with PyAV, so the
  still path is exercised end-to-end on machines without libjpeg-turbo.

## [0.1.20] - 2026-09-24

### Added

- **The RTSP transport now reaches the Home Assistant stream component.** The
  integration hands the transport of every camera over as
  `Camera.stream_options = {"rtsp_transport": ...}` (Home Assistant supports this
  since the generic camera transport option). Home Assistant therefore streams the
  same way the add-on does, which matters a lot for H.265 or high bitrate cameras:
  with UDP the picture in the Home Assistant camera card can stay black while the
  stream itself is fine. Changing the transport in the add-on panel updates the
  running entity on the next poll.

### Tests

- Two new real Home Assistant tests (15 in total) verify that the transport lands
  in `stream_options`, that Home Assistant accepts the value (`RTSP_TRANSPORTS`) and
  that a transport change reaches the entity.

## [0.1.19] - 2026-09-24

### Fixed

- **Preview stayed black although the stream test was green.** Reading a stream
  header with ffprobe says nothing about whether the frames arrive, and RTSP over
  **UDP** loses packets that only hurt once the stream is decoded. The preview now
  retries every mode over **TCP** when the configured transport produces nothing,
  and the successful transport is returned in the API (`transport`) and shown as a
  hint in the UI.
- **H.265 cameras were declared broken too early.** Previews of codecs that have to
  be decoded or transcoded now wait up to 30 seconds for the first frame instead of
  `test_timeout` only. The detection stays fast (10 s) so the panel does not hang.

### Added

- While the automatic preview detection runs on an H.265 camera the panel explains
  that the first frame can take a few seconds (`preview.detecting_slow`, all four
  languages).
- New fake ffmpeg mode `udp-fail` and three regression tests covering the TCP
  fallback of `preview/detect`, `mjpeg` and `hls/start`.

## [0.1.18] - 2026-09-24

### Fixed

- **Camera entities failed to be added in Home Assistant 2026.9** with
  `Error adding entity camera.<name> for domain camera with platform rtsp_cameras`
  and `TypeError: 'str' object is not callable` in `camera/webrtc.py`. Home
  Assistant changed `Camera.stream_source` from a property to an awaited method;
  the entity now implements `async def stream_source(self)`. Until this fix every
  camera stayed an orphaned registry entry ("no longer provided by rtsp_cameras")
  without a stream, which is why there was no preview in Home Assistant even
  though the add-on showed one.
- `ha_tests` now call `async_get_stream_source` - Home Assistant's own helper that
  awaits the method - so this API change cannot slip through again.

## [0.1.17] - 2026-09-24

### Fixed

- **A fixed stylesheet could stay hidden behind the browser cache** (the dropdown
  kept its white popup even after 0.1.15). The asset URLs now carry the add-on
  version (`app.css?v=0.1.17`, `app.js?v=0.1.17`) and static files are served with
  `Cache-Control: no-cache`, so every update is picked up with a normal reload.

## [0.1.16] - 2026-09-24

### Fixed

- The helper files for the add-on update (`actions.json`, `addon_update.json`) can
  no longer affect the camera entities: a problem with them is logged and ignored,
  so the camera platform keeps providing its entities (Home Assistant otherwise
  shows them as "no longer provided by rtsp_cameras").

## [0.1.15] - 2026-09-24

### Fixed

- **Dropdowns were unreadable on the dark interface.** The `<option>` list of a
  `<select>` is drawn by the browser, and without `color-scheme` the popup stayed
  white while the text kept the light colour of the dark theme. The panel now
  declares `color-scheme: dark` (and `light` inside the light theme) and styles
  `option`/`optgroup` with the panel palette, so the list follows the theme and
  the selected entry uses the accent colour.
- Selects in the header and the preview window keep the native arrow instead of
  hiding it, so it is obvious again that they are dropdowns.

## [0.1.14] - 2026-09-24

### Fixed

- **The update check now really works without a Supervisor token.** Home Assistant
  keeps no add-on list on disk, so the previous fallback could not report anything.
  The integration (which is running inside Home Assistant and therefore talks to
  the Supervisor itself) now publishes the state of the add-on's `update` entity
  into `rtsp_cameras/addon_update.json`; the panel reads that file for the
  installed and newest version. "Check for updates" asks the integration to refresh
  the entity (`homeassistant.update_entity`), and "Update add-on" still installs
  through `update.install` - both without a single Supervisor call from the
  add-on container.

## [0.1.13] - 2026-09-24

### Added

- **Updates also work without a Supervisor token.** The button now asks Home
  Assistant to install the update: the add-on writes a request into the folder it
  shares with the integration (`rtsp_cameras/actions.json`), the integration picks
  it up within its poll and calls the `update.install` service on the add-on's
  `hassio` update entity. Nothing else needs a token, so the button works on
  installations where Supervisor hands out no `SUPERVISOR_TOKEN`.
- The update card and the start-up log report **how Home Assistant has the add-on
  on record**: `hassio_api`, the role, `homeassistant_api` and the repository
  (read from `/config/.storage/hassio`, no API needed). That is the information
  deciding whether Supervisor hands out a token.
- The version information of the update check is read from the same storage file
  when the Supervisor API is unavailable, so the newest known version is still
  shown.

## [0.1.12] - 2026-09-24

### Fixed

- **A container without `SUPERVISOR_TOKEN` no longer breaks the update check.**
  The check reported `/available_updates: No Supervisor token available` and the
  button answered "Supervisor not reachable", because the container had no token
  at all (Supervisor injects it when the manifest has `hassio_api` enabled - an
  update or reinstall recreates the container and adds it). The check now:
  - accepts the legacy `HASSIO_TOKEN` variable as well,
  - falls back to the add-on list Home Assistant keeps in `/config/.storage/hassio`
    (`version`/`version_latest`), which needs no API access at all,
  - says exactly what is missing and what to do instead of a bare failure, and
    offers the update only when the add-on can actually start it,
  - logs the API environment at start (variable names only, never values).

## [0.1.11] - 2026-09-24

### Fixed

- **The update check was refused by Supervisor.** The add-on asked Supervisor to
  reload the add-on store and to report its latest version, but without a
  `hassio_role` the API answers a plain "refused" for those endpoints. The
  manifest now declares `hassio_role: manager`, and the reload tries
  `/store/reload`, `/addons/reload` and `/reload_updates` in turn.
- A refused or unreachable Supervisor is no longer reported as a bare failure:
  the interface and the add-on log now name the endpoint and the HTTP error, so
  it is obvious whether the check or the permission is the problem.
- When the add-on information does not carry the newest version, the check falls
  back to Supervisor's list of pending updates (`/available_updates`).

## [0.1.10] - 2026-09-24

### Changed

- The update check is now impossible to miss: the version chip in the header is a
  button that jumps to *Settings* and starts the check, and the status strip shows
  the add-on version with a warning LED and the newest version as soon as one is
  available. The version chip also shows an arrow (for example `v0.1.9 ↑`) while an
  update is waiting.

## [0.1.9] - 2026-09-24

### Added

- **Update check with one click.** The *Settings* tab shows the installed add-on
  version and a **Check for updates** button. It makes Supervisor re-read the
  add-on store and reports the newest published version; as soon as one exists, an
  **Update add-on** button (plus a banner at the top of every tab) starts the
  update through the Supervisor API. The interface reconnects by itself after the
  add-on restarted with the new version and then offers the usual
  "Restart Home Assistant" step, because the new version also refreshes the
  bundled integration.

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
