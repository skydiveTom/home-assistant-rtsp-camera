# RTSP Camera Manager — documentation

## What it does

The add-on is a small camera manager for Home Assistant:

1. it stores camera instances (name + stream URL) in a JSON file,
2. it lets you **test** and **preview** every stream before you rely on it,
3. it publishes the list so the bundled integration can turn each instance into a
   `camera` entity.

```
  you ──▶ add-on UI (ingress) ──▶ /data/cameras.json
                                     │
                                     ▼ (published copy)
                     /config/rtsp_cameras/cameras.json ──▶ custom_integration
                                                              └─▶ camera.<name>
```

## Installation

1. **Add the repository**

   Settings → Add-ons → Add-on store → ⋮ → *Repositories*:

   ```
   https://github.com/skydiveTom/home-assistant-rtsp-camera
   ```

2. **Install the add-on** *RTSP Camera Manager* and start it.
3. **Open the interface** via the sidebar entry *RTSP Cameras*.
4. **Restart Home Assistant** once after the first start: the add-on copies the
   integration into `/config/custom_components/rtsp_cameras` and Home Assistant
   only picks that up on restart. The banner in the interface offers a button.

## Adding a camera

Cameras are added **in the add-on**, not inside Home Assistant:

1. Open the add-on panel **RTSP Cameras** (sidebar entry, or *Settings → Add-ons →
   RTSP Camera Manager → Open web UI*).
2. Press **Add camera**.
3. Give it a name, paste the stream URL and use **Test stream** and *Quick
   preview* to verify it.
4. Press **Save camera**. The `camera` entity shows up in Home Assistant within a
   few seconds, because the integration re-reads the camera file every 10 seconds.

The integration is the read-only counterpart: it turns every entry of the camera
file into one `camera` entity. That is why there is no "add camera" dialog in
Home Assistant - cameras are managed in exactly one place.

| Field | Notes |
| --- | --- |
| **Name** | Becomes the entity name, e.g. `Front door` → `camera.front_door`. |
| **RTSP URL** | For example `rtsp://user:password@192.168.1.10:554/stream1`. Credentials may be part of the URL. |
| **RTSP transport** | `tcp` works for almost every camera, some devices need `udp` or `http`. |
| **Enabled** | Disabled cameras stay in the add-on but are hidden from Home Assistant. |

Buttons:

- **Test stream** – runs `ffprobe` and shows codec, resolution, FPS, bit rate and
  the raw error message when it fails. Works before you save the camera.
- **Quick preview** – live MJPEG or HLS picture in a modal. The preview process
  runs inside the add-on and stops as soon as you close the window (at the latest
  after 45 seconds without playback requests).

## The camera entity in Home Assistant

After the restart you get one device per camera:

- `camera.<name>` with the **stream source** pointing directly at your RTSP URL,
  so Home Assistant handles the live view with its own stream component
  (HLS/WebRTC, exactly like the built-in Generic Camera).
- Still images are generated from the stream by Home Assistant, so the add-on does
  not have to run for a snapshot.
- Attributes show the add-on camera id, the RTSP transport and the file the
  definition came from.

Changes in the add-on (add, rename, disable, delete) are picked up within
seconds, because the integration watches the JSON file. No restart is needed for
camera changes – only for installing or updating the integration itself.

### Installing the integration without the add-on

1. Copy `custom_components/rtsp_cameras` from this repository into
   `/config/custom_components/rtsp_cameras` (or install it with HACS as an
   integration).
2. Restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **RTSP Camera Manager** and
   accept the default file path (`rtsp_cameras/cameras.json` inside your config
   directory).

The folder does not have to exist yet: it is created automatically, and the file
is picked up as soon as the add-on (or you) fills it.

If you manage the integration yourself, set the add-on option
`install_integration` to `false` so the add-on does not overwrite it.

## Add-on options

| Option | Default | Description |
| --- | --- | --- |
| `language` | `auto` | Interface language. `auto` follows Home Assistant, then the browser, then English. |
| `default_rtsp_transport` | `tcp` | Pre-selected transport for new cameras. |
| `install_integration` | `true` | Copy the bundled integration into `/config/custom_components`. |
| `ha_restart_after_install` | `false` | Restart Home Assistant automatically after installing/updating the integration. |
| `health_check_interval` | `60` | Seconds between automatic stream checks (`0` disables them). |
| `test_timeout` | `15` | Seconds to wait for a camera when testing or previewing. |
| `preview_mode` | `mjpeg` | `mjpeg` re-encodes every frame (universal), `hls` copies H.264 streams when possible (low CPU). |
| `preview_max_height` | `1080` | Height limit for snapshots and previews. |
| `preview_fps` | `5` | Frame rate of the MJPEG preview and of transcoded HLS. |
| `redact_credentials_in_logs` | `true` | Mask user name and password inside stream URLs before logging. |

## Files

| Path | Purpose |
| --- | --- |
| `/data/cameras.json` | Add-on owned camera list (persistent). |
| `/data/integration_state.json` | Remembers that a restart is pending after an install. |
| `/config/rtsp_cameras/cameras.json` | Published copy read by the integration. |
| `/config/custom_components/rtsp_cameras/` | The integration itself. |
| `/tmp/rtsp_cameras_preview/` | Temporary HLS segments of running previews. |

## Troubleshooting

**The entity does not appear**

- Restart Home Assistant after the first start (the integration must be loaded).
- Check Settings → Devices & services for *RTSP Camera Manager*; if you install
  the integration manually, add it there.
- Look at the add-on log for `Installing the Home Assistant integration`.

**Adding the integration complains about the camera file folder**

That was a bug in versions before 0.1.1: the folder of `cameras.json` had to exist
already. Update the integration (add-on 0.1.1 and newer) and the folder is created
automatically. If the message still appears, the path points at something Home
Assistant cannot create - use the default `rtsp_cameras/cameras.json`.

**`Test stream` fails**

- `Server returned 401` – wrong user name or password in the URL.
- `Connection refused` / `timeout` – wrong address or port, or the camera is not
  reachable from the Home Assistant host.
- `No video stream found` – the URL points at an audio-only or wrong path (many
  cameras use `/stream1`, `/Streaming/Channels/101`, `/h264` …).
- Try the other **RTSP transport** values (`udp`, `http`).

**The preview stays black**

- H.264/H.265 streams work best in the HLS mode; switch the mode in the preview
  window.
- Browsers limit parallel MJPEG connections per host; close other previews.

**Home Assistant shows the camera as unavailable**

- Home Assistant itself must be able to reach the RTSP URL (it streams the video,
  the add-on does not proxy it). Verify the URL with **Test stream**.

## Security notes

- The web interface is served through the Home Assistant ingress, so it is only
  reachable for users who are logged in to Home Assistant.
- Stream credentials are masked in the camera list and in every log message
  (option `redact_credentials_in_logs`).
- The add-on mounts `/config` (needed to install the integration and to publish
  the camera file) and requests no privileged capabilities. The container port is
  not published on the host; ingress is the only way in.
- The published file contains your stream URLs **including credentials**, because
  Home Assistant needs them to open the stream. It lives in your Home Assistant
  configuration directory, next to `configuration.yaml`.

## Development & testing

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux: .venv/bin/python
.venv/Scripts/python -m pytest                                # unit tests
.venv/Scripts/python -m ruff check .                          # lint
```

The integration exists twice: the source of truth is
`custom_components/rtsp_cameras` at the repository root (that is what HACS
installs) and the add-on ships a copy in
`rtsp_cameras/custom_components/rtsp_cameras`, because the add-on folder is the
Docker build context. Keep both in sync with `scripts/sync_integration.ps1`; the
test suite fails when the two copies differ.

Run the add-on locally without a container:

```bash
RTSP_ADDON_DATA_DIR=./local-data \
RTSP_ADDON_CONFIG_DIR=./local-config \
RTSP_ADDON_TEMP_DIR=./local-tmp \
python -m app
```

## Credits

- Uses [ffmpeg](https://ffmpeg.org/) for stream testing, snapshots and previews.
- Vendors [hls.js](https://github.com/video-dev/hls.js) (Apache-2.0) for the HLS
  preview in browsers without native support. `app/static/hls.min.js` is an
  unmodified copy of hls.js 1.5.20.

