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

Some installations do not give the add-on a `SUPERVISOR_TOKEN` (the update card and
the log then say so). Everything still works, because the *integration* does the
talking:

- **Check for updates** writes a request into `/config/rtsp_cameras/actions.json`;
  the integration asks Home Assistant to refresh the add-on's update entity
  (`homeassistant.update_entity`) and publishes the result into
  `/config/rtsp_cameras/addon_update.json`, which the panel displays.
- **Update add-on** writes another request; the integration lets Home Assistant
  install it through the regular `update.install` service.

Supervisor, the integration and Home Assistant do the work - the add-on container
needs no token at all. If a new version does not show up, update in
*Settings → Add-ons → RTSP Camera Manager* and reload the add-on store first
(*⋮ → Reload*), which refreshes the manifest from the repository.

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
- Also use **TCP** as the RTSP transport: the integration passes it on to Home
  Assistant (`stream_options`), so Home Assistant does not lose UDP packets either
  (a video that only stutters or stays black in the card while the add-on preview
  works is a typical symptom).
- `h264` → look at the Home Assistant log for `stream`/`camera` errors, then
  download the integration **Diagnostics** (Settings → Devices & services → RTSP
  Camera Manager → ⋮ → Download diagnostics), which lists the checked file, the
  cameras and their (masked) stream URLs.
- Still images (thumbnail, `camera.snapshot`) are taken from the live stream by the
  integration - it waits up to 8 seconds for a keyframe. If a camera offers an HTTP
  snapshot URL, enter it in the add-on: that snapshot is used first and is faster.

**PTZ (pan, tilt, zoom)**

A camera with PTZ is configured in the add-on panel: open the camera, expand *PTZ
control*, tick *This camera supports PTZ*, pick the vendor preset and press *Fill
commands from the profile*. The preset fills the HTTP commands of the camera - they
stay editable, so unusual firmware can be handled too, and `{speed}`, `{preset}`,
`{direction}` stay dynamic. Presets are entered one per line as `number=name`.

| Preset | Commands used |
| --- | --- |
| Axis (VAPIX) | `/axis-cgi/com/ptz.cgi?move=…` |
| Dahua / Amcrest | `/cgi-bin/ptz.cgi?action=start&code=…` |
| Xiongmai / NETSurveillance | `/cgi-bin/ptz.cgi?…&code=DirectionLeft…` |
| Hikvision (ISAPI) | `PUT /ISAPI/PTZCtrl/channels/1/continuous` with an XML body |
| Foscam | `/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveUp…` |
| ONVIF (SOAP) | `POST …/onvif/ptz_service` with a `ContinuousMove`, `Stop`, `GotoPreset` or `GotoHomePosition` envelope |
| Xiongmai DVRIP | `DVRIP {"Command":"DirectionLeft","Step":…}` over **TCP 34567**, for DVRs without a web interface |
| Custom | your own `GET/POST/PUT url [body]` commands |

**Credentials are taken from the stream URL** when the PTZ block does not define its
own - both `rtsp://user:pass@host/…` and the query style of many DVRs
(`…/user=admin&password=secret&channel=1&stream=0.sdp`) are understood, including the
channel. Only cameras with a different PTZ login need the fields filled in.

For **ONVIF** press *Discover the ONVIF token* next to the commands: the add-on sends
`GetProfiles` to the media service, takes the first profile token and fills the
commands with it. For **DVRIP** the port field (default 34567) is used; the add-on
logs in with the credentials of the stream URL, which the devices expect as a double
MD5 hash.

*Test PTZ* sends the **stop** command, so the test never moves the camera. Cameras
without any control interface (for example a device that only speaks RTSP and a
proprietary port that does not answer DVRIP) cannot be moved - the test reports that
clearly.

Home Assistant gets:

- `rtsp_cameras.ptz` - move with the **same fields as `onvif.ptz`** (`pan`, `tilt`,
  `zoom`, `speed` 0.01-1, `continuous_duration`, `preset`, `move_mode`), plus
  `action` for the plain actions (`left`, `zoom_in`, `home`, …). A direction moves
  for `continuous_duration` seconds (default 0.5) and is stopped automatically.
- `rtsp_cameras.ptz_home` - go to the home position.
- A **button per preset** (and one *PTZ stop*) on the camera device, so a dashboard
  can jump to a view with one tap.

```yaml
service: rtsp_cameras.ptz
target:
  entity_id: camera.front_door
data:
  tilt: UP
  continuous_duration: 1
```

The preview window of the add-on shows a PTZ pad; hold an arrow to move, release to
stop.

**Brand assets (the icon in Home Assistant)**

Home Assistant serves local brand images from `custom_components/rtsp_cameras/brand`
(the integrations dashboard requests `icon.png` for the light and `dark_icon.png` for
the dark theme). The PNGs in that folder are the artwork actually shown; they are
generated by `scripts/make_brand_icons.py` (numpy + PyAV, no image library needed) and
guarded by `rtsp_cameras/tests/test_branding.py` (size, PNG type, both themes) and
`ha_tests/test_branding.py` (Home Assistant's own loader finds them).

**Testing against a real camera (developers)**

The end-to-end tests can talk to a camera on your network instead of the fakes:

```bash
export RTSP_LIVE_CAMERA_URL="rtsp://user:pass@192.168.1.28:554/stream1"
python -m pytest -c ha_tests/pytest.ini ha_tests/test_live_camera.py -q
```

They check that the camera published by the add-on becomes `camera.<name>`, that
Home Assistant gets the RTSP URL and the transport, that its `stream` component
decodes a live frame and that `camera.async_get_image` returns a JPEG. Without the
variable the tests are skipped, so CI (and any other machine) stays offline.

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

**`Test stream` is green but the preview stays black**

That happens when the stream test reports a codec **without** resolution and frame
rate: the camera answered the RTSP request, but the transport delivered no video
packets - RTSP over **UDP** behaves like that on many networks. The add-on points it
out under the test result and switches the preview to **TCP** by itself; set the
camera to `tcp` as well so the health checks and Home Assistant use it too.

**The preview stays black**

- The stream test can be green while the preview fails: ffprobe only reads the
  stream header, the preview has to **decode** the frames. RTSP over **UDP** loses
  packets easily, so the add-on retries the preview over **TCP** automatically - the
  panel then shows `UDP lost too many packets ...` as a hint.
- H.264/H.265 streams work best in the HLS mode; switch the mode in the preview
  window.
- A camera in **H.265/HEVC** has to be decoded (and for Home Assistant also
  transcoded), so the first frame can take a few seconds - 1080p50 footage may need
  a stronger host. Enter the H.264 sub stream as *Stream URL for Home Assistant*.
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

