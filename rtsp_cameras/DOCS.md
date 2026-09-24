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

## Prebuilt image

The app is published as a multi-arch container image
(`ghcr.io/skydivetom/rtsp-cameras`, `amd64` and `aarch64`) by the
*Build add-on image* workflow, and the manifest references it. Install and update
therefore **pull** the image instead of building it on your Home Assistant host -
an update takes seconds.

If you fork this repository and want Supervisor to build your own image, remove
the `image` key from `rtsp_cameras/config.yaml` and push your version: Supervisor
then builds locally again (and the workflow publishes the image under your own
GitHub namespace).

## Updating the add-on

Two ways, both end with a restart of Home Assistant:

1. **From inside the add-on** – open the *Settings* tab of the add-on panel and
   press **Check for updates**. It asks Supervisor to re-read the add-on store and
   shows the newest published version. When a newer one exists, press
   **Update add-on** (also offered as a banner on every tab): Supervisor updates
   the container, the page reconnects by itself, and the new version refreshes the
   bundled integration and asks for the Home Assistant restart.
2. **From Home Assistant** – *Settings → Add-ons → RTSP Camera Manager*, press
   **Update** (or use the ⋮ menu → *Check for updates* to refresh the store first).

Without the button Supervisor only looks for new versions on its own schedule, so
a fresh release can take a while to show up.

### When the add-on has no Supervisor access

Some installations do not give the add-on a `SUPERVISOR_TOKEN` (the update card
and the log then say so, and the card also shows how Home Assistant has the add-on
on record). The update button still works: the add-on writes the request into
`/config/rtsp_cameras/actions.json`, the integration reads that file on its next
poll and lets Home Assistant install the update through the regular
`update.install` service. Supervisor, the integration and Home Assistant then do
the work - no token needed.

If even that does not help, update in *Settings → Add-ons → RTSP Camera Manager*
and reload the add-on store first (*⋮ → Reload*), which refreshes the manifest
from the repository.

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
| **Stream URL for Home Assistant** | Optional. Used by the `camera` entity instead of the URL above - ideal for the H.264 sub stream of an H.265 camera, because Home Assistant camera cards only play H.265 in a few browsers. |
| **RTSP transport** | `tcp` works for almost every camera, some devices need `udp` or `http`. |
| **Enabled** | Disabled cameras stay in the add-on but are hidden from Home Assistant. |

Buttons:

- **Test stream** – runs `ffprobe` and shows codec, resolution, FPS, bit rate and
  the raw error message when it fails. Works before you save the camera.
- **Quick preview** – live picture in a modal. The preview process runs inside
  the add-on and stops as soon as you close the window (at the latest after 45
  seconds without playback requests).

### Which preview mode is used?

The add-on option *Preview mode* defaults to `auto`: the first time you open the
preview of a camera, MJPEG is tried first and HLS second. The mode that produced
a picture is stored on that camera and is the only one used afterwards - so a
camera whose MJPEG stream cannot be decoded (or whose frames are swallowed by a
buffering reverse proxy) simply switches to HLS and stays there. Set the option
to `mjpeg` or `hls` to skip the detection and force one mode.

## The camera entity in Home Assistant

After the restart you get one device per camera:

- `camera.<name>` with the **stream source** pointing directly at your RTSP URL,
  so Home Assistant handles the live view with its own stream component
  (HLS/WebRTC, exactly like the built-in Generic Camera).
- Still images are generated from the stream by Home Assistant, so the add-on does
  not have to run for a snapshot.
- Attributes show the add-on camera id, the RTSP transport, the codec and the file
  the definition came from.

**Nothing plays in the Home Assistant card (black picture or spinner)**

Home Assistant proxies the stream with its own `stream` component and only
remuxes it - it does not transcode. Streams in **H.265/HEVC** therefore stay
black in most browsers (Safari, and Edge with the HEVC extension, are the
exceptions). Check the codec in the camera tile of the add-on:

- `hevc`/`h265` → enter the **H.264 sub stream** of the camera in *Stream URL for
  Home Assistant* (for example `/Streaming/Channels/102` on Hikvision or
  `subtype=1` on Dahua), or switch the camera itself to H.264.
- `h264` → look at the Home Assistant log for `stream`/`camera` errors, then
  download the integration **Diagnostics** (Settings → Devices & services → RTSP
  Camera Manager → ⋮ → Download diagnostics), which lists the checked file, the
  cameras and their (masked) stream URLs.

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
| `preview_mode` | `auto` | `auto` tests MJPEG first and HLS second and keeps the working mode per camera. `mjpeg` re-encodes every frame (universal), `hls` copies H.264 streams when possible (low CPU). |
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

**Cameras still do not show up as entities after a restart**

Versions before 0.1.7 never created a camera entity. The entity inherited from
both the data coordinator and the camera base class, but only the coordinator was
initialised, so the camera had no access token, no image cache and no stream
bookkeeping - Home Assistant dropped it while setting up the platform. Update the
add-on to 0.1.7 or newer and restart Home Assistant.

From 0.1.7 on the Home Assistant log states exactly what is going on:

```text
Found 2 camera(s) in /config/rtsp_cameras/cameras.json: front_door, garage
Registered 2 camera entity/entities: Front Door, Garage
```

- `Camera file ... does not exist yet` - the path configured in the integration
  is not the file the add-on publishes. Compare it with the *Settings* tab of the
  add-on (default: `/config/rtsp_cameras/cameras.json`).
- `Found 0 camera(s)` - no camera is enabled, or the file was written by another
  tool with a different structure.

**The update check says "no SUPERVISOR_TOKEN"**

The add-on talks to Supervisor with the `SUPERVISOR_TOKEN` environment variable,
which Supervisor injects when the manifest enables `hassio_api`. If the container
does not have it (for example because it was created before that flag made it into
the running version), the update button still works: the add-on writes the request
into `/config/rtsp_cameras/actions.json` and the integration lets Home Assistant
install the update through the regular `update.install` service. The card also
shows how Home Assistant has the add-on on record (`hassio_api`, role,
repository).

To give the container a token again, reload the add-on store
(*Settings → Add-ons → ⋮ → Reload*) and update or reinstall the add-on - Supervisor
then recreates the container and injects the token. The add-on log prints what it
sees at startup, for example:

```text
WARNING [app.main] No Supervisor token in this container (variables: none, socket: False).
```

**The add-on stops with `s6-overlay-suexec: fatal: can only run as pid 1`**

That happened in versions before 0.1.3: the manifest did not declare `init: false`,
so Supervisor started the container with Docker's own init (tini) as PID 1 and the
s6-overlay init of the Home Assistant base image refused to run. Update the add-on
to 0.1.3 or newer - the flag is part of the manifest now.

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

