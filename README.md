# home-assistant-rtsp-camera

[![Tests](https://github.com/skydiveTom/home-assistant-rtsp-camera/actions/workflows/tests.yml/badge.svg)](https://github.com/skydiveTom/home-assistant-rtsp-camera/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Add RTSP camera instances to Home Assistant **using only the stream URL**.

Give the camera a name, paste the URL, press **Test stream** to find out whether
it works and use **Quick preview** for a live look — then place the resulting
`camera` entity on any dashboard.

[![Open your Home Assistant instance and show the add-on store.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FskydiveTom%2Fhome-assistant-rtsp-camera)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=skydiveTom&repository=home-assistant-rtsp-camera&category=integration)

## Where do I add cameras?

In the add-on: open the **RTSP Cameras** panel (sidebar entry, or *Settings →
Add-ons → RTSP Camera Manager → Open web UI*), press **Add camera**, name it,
paste the RTSP URL and check it with *Test stream* and *Quick preview* before you
save it.

The integration deliberately has no "add camera" dialog: it only turns the camera
file written by the add-on into `camera` entities, so cameras are managed in
exactly one place — the add-on panel.

## Contents of this repository

| Path | What it is |
| --- | --- |
| `rtsp_cameras/` | The Home Assistant **add-on**: web interface, stream test, preview, camera storage. |
| `custom_components/rtsp_cameras/` | The **custom integration** that turns every camera into a `camera` entity (this is what HACS installs). |
| `rtsp_cameras/custom_components/` | A copy of the integration shipped inside the add-on, because the add-on folder is the Docker build context. |
| `scripts/sync_integration.ps1` | Keeps both integration copies identical (the test suite enforces it). |
| `scripts/setup_ha_venv.ps1` | Builds the throw-away `.venv-ha` that runs `ha_tests` (on Windows with the `fcntl`/`resource` stand-ins written into it). |
| `scripts/publish_releases.ps1` | Publishes the GitHub release of every tagged version that has none, with the changelog section of that version as its notes (`-DryRun` prints the plan). |
| `rtsp_cameras/tests/` | Unit tests for the API, storage, ffmpeg layer, translations and repository layout. |
| `ha_tests/` | Integration tests that run a real Home Assistant core and prove cameras become `camera` entities. |

## Features

- **URL only** – no YAML, no ONVIF discovery, no vendor app. `rtsp://`, `rtsps://`,
  `rtmp://` and `http(s)://` MJPEG streams are supported.
- **Test stream** – `ffprobe` reports codec, profile, resolution, frame rate, bit
  rate and the raw error message, even for URLs you have not saved yet.
- **Quick preview** – live view inside the add-on. The mode is detected
  automatically: MJPEG is tried first, HLS second, and only the one that works is
  kept for that camera.
- **Real camera entities** – `camera.<name>` with the RTSP URL as the stream
  source, so Home Assistant handles the live view with its own stream component
  (HLS/WebRTC) and can generate still images. A second, optional stream URL (for
  example the H.264 sub stream of an H.265 camera) can be published to Home
  Assistant when a camera card stays black.
- **PTZ control** – a camera with pan/tilt/zoom gets a PTZ section in the panel with
  ready made command sets (Axis, Dahua/Amcrest, Xiongmai, Hikvision, Foscam,
  **ONVIF (SOAP)** or **DVRIP on TCP 34567** for DVRs without a web interface - plus
  your own commands), a pad in the preview and presets. Credentials are **inherited
  from the RTSP URL**, so they are never typed twice. The **PTZ test mode** finds the
  right variant for you: enter the IP of the camera (or nothing at all), one button
  asks every variant and the one that answers becomes the main handling - without
  moving the camera, because the test only sends stop commands. The Xiongmai family answers
  **no stop payload**: the vendor SDK has no stop command either - its `stop` flag is the
  same direction command again, which this firmware reads as "drive to the end of the
  axis". A released arrow is therefore ended by a **capture** - the position of the release
  is stored in a preset of the add-on and the camera returns to it. A camera of this family
  therefore gets **step by step arrows**: the position of the moment is stored as a step of
  an axis, a short press of an arrow walks one rung of that ladder and the step field walks
  several - a bounded move, without the travel to a limit. **Holding** such an arrow does not
  start that travel either: the panel walks the ladder rung by rung for as long as the key is
  down, and a release ends the walk at the rung the camera is on (a camera whose stop really
  halts an axis keeps the continuous move and its capture). The **camera entity in Home
  Assistant follows the same rule**: a move of such a camera walks the ladder for
  `continuous_duration` seconds - rung by rung, never a sweep to the limit - while a
  camera whose stop halts keeps its own move and stop. A rung is a **whole position**, so a
  step that switches axes carries the other axis back to the crossing of the two ladders: this
  family has no bounded *relative* move, so a rung can only be written where the camera already
  stands and a tilt "in place" would need one. The step is sent anyway and the panel names the
  axis it dragged and how far - refusing it instead left both pairs of arrows dead as soon as
  the user alternated them. The pad keeps the **coordinates of the camera** on both ladders
  and shows them under the arrows: every move rewrites the pair - a step writes the rung it
  reached *and* the crossing rung the other axis is carried to - and the entity of Home
  Assistant carries the same pair as `ptz_step`. The rung at which a ladder stands on the other
  one is data of the camera (`step_crossings` of the PTZ block) and not the middle of a list: a
  rung stored later is appended to a ladder and moves its middle, so the add-on walked two
  rungs at once with a ladder that grew that way. Home Assistant
  receives `rtsp_cameras.ptz` (same fields as `onvif.ptz`), `rtsp_cameras.ptz_home` and a
  button per preset. A camera that is mounted upside down, on its side or behind a mirror is
  **mirrored per axis** - one button each in the preview pad (*Mirror left/right*, *Mirror
  up/down*), stored with the camera, so the pad, the entity of Home Assistant and every
  automation follow the arrow of the picture.
- **The codec of a stream, without the vendor app** – a camera whose sub stream sends
  **H.265** stays black in Chrome, Edge and Firefox, because Home Assistant copies the
  stream without transcoding. The camera editor reads the encoders **over ONVIF**, and over
  the vendor protocol of the Xiongmai family for a firmware that ships no media service,
  and switches the one of the sub stream to H.264 - the configuration the camera handed out
  is sent back with one word changed, and the answer is the codec the camera reports
  afterwards.
- **Live in seconds** – adding, renaming, disabling or deleting a camera is picked
  up by Home Assistant within seconds, without a restart and without YAML.
- **Self-updating** – the Settings tab checks for a new add-on version on demand
  (Supervisor store reload) and installs it with one button.
- **Four languages** – English (default), German, Spanish and Polish. The
  interface follows the language configured in Home Assistant.
- **Safe by design** – reachable only through the authenticated Home Assistant
  ingress, credentials of stream URLs are masked in the interface and in the log.
- **Prebuilt image** – published as a multi-arch image
  (`ghcr.io/skydivetom/rtsp-cameras`) by GitHub Actions, so installing and
  updating pulls the image instead of building it on your Home Assistant host.

## Installation

### 1. The add-on (recommended, does everything)

1. Click the *Add repository* button above or add

   ```
   https://github.com/skydiveTom/home-assistant-rtsp-camera
   ```

   in Settings → Add-ons → Add-on store → ⋮ → *Repositories*.

2. Install **RTSP Camera Manager**, start it and open **RTSP Cameras**.
3. Add your first camera: name, RTSP URL, **Test stream**, save.
4. Restart Home Assistant once when the banner asks for it — the add-on installs
   the integration into `/config/custom_components/rtsp_cameras` and the camera
   entities appear afterwards.

### 2. Only the integration (HACS or manual)

1. Use the HACS button above, add the repository as an *Integration*, or copy
   `custom_components/rtsp_cameras` into your `/config/custom_components/`.
2. Restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **RTSP Camera Manager** and
   accept the default camera file path (`rtsp_cameras/cameras.json` inside your
   configuration directory).

When you install the integration yourself, set the add-on option
`install_integration` to `false` so the add-on does not overwrite your copy.

### 3. Tests without a camera

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

The suite uses fake `ffmpeg`/`ffprobe` binaries, so no camera is needed.

The camera entities themselves are covered by a second suite that boots a real
Home Assistant core (the same version the CI pins) and checks that cameras
published by the add-on end up as `camera` entities:

```bash
python -m venv .venv-ha
.venv-ha/Scripts/python -m pip install pytest-homeassistant-custom-component av
.venv-ha/Scripts/python -m pytest -c ha_tests/pytest.ini ha_tests
```

On Windows those three commands do not give you an environment the suite can
use: before the first test runs, Home Assistant imports the Unix-only `fcntl`
and `resource` modules, so `.venv-ha` has to carry a `sitecustomize.py` that
stands in for them. `scripts/setup_ha_venv.ps1` creates the environment *and*
writes that file; `-ShimOnly` rewrites only the file, in an environment that
already exists:

```powershell
pwsh -File scripts/setup_ha_venv.ps1
```

## How it works

```
browser ──▶ add-on ingress UI
              │  writes cameras
              ▼
        /data/cameras.json ──(published copy)──▶ /config/rtsp_cameras/cameras.json
                                                          │ polls every 10 s
                                                          ▼
                                       custom_components/rtsp_cameras
                                                          └─▶ camera.<name>
                                                              (stream_source = rtsp URL)
```

- The add-on owns the camera list and never proxies your video: Home Assistant
  opens the RTSP stream itself.
- The add-on only runs `ffmpeg` for its own test, snapshot and preview features,
  and it stops every preview process when you close the window.

## Documentation

- Add-on documentation: [`rtsp_cameras/DOCS.md`](rtsp_cameras/DOCS.md)
- Changelog: [`rtsp_cameras/CHANGELOG.md`](rtsp_cameras/CHANGELOG.md)
- Polish readme: [`README.pl.md`](README.pl.md)

## Support

Bugs and feature requests:
<https://github.com/skydiveTom/home-assistant-rtsp-camera/issues>

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).

The add-on bundles [hls.js](https://github.com/video-dev/hls.js) 1.5.20
(Apache-2.0) for the HLS preview in browsers without native support, and uses
[ffmpeg](https://ffmpeg.org/) inside its container.

