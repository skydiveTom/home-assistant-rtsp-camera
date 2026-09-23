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
| `rtsp_cameras/tests/` | Unit tests for the API, storage, ffmpeg layer, translations and repository layout. |

## Features

- **URL only** – no YAML, no ONVIF discovery, no vendor app. `rtsp://`, `rtsps://`,
  `rtmp://` and `http(s)://` MJPEG streams are supported.
- **Test stream** – `ffprobe` reports codec, profile, resolution, frame rate, bit
  rate and the raw error message, even for URLs you have not saved yet.
- **Quick preview** – live view inside the add-on as MJPEG (works in every
  browser) or HLS (stream copy for H.264 cameras, so a Raspberry Pi stays idle).
- **Real camera entities** – `camera.<name>` with the RTSP URL as the stream
  source, so Home Assistant handles the live view with its own stream component
  (HLS/WebRTC) and can generate still images.
- **Live in seconds** – adding, renaming, disabling or deleting a camera is picked
  up by Home Assistant within seconds, without a restart and without YAML.
- **Four languages** – English (default), German, Spanish and Polish. The
  interface follows the language configured in Home Assistant.
- **Safe by design** – reachable only through the authenticated Home Assistant
  ingress, credentials of stream URLs are masked in the interface and in the log.
- **Prebuilt image** – published as a multi-arch image
  (`ghcr.io/skydiveTom/rtsp-cameras`) by GitHub Actions, so installing and
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

