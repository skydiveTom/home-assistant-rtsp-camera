# Changelog

All notable changes to this add-on are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.8] - 2026-10-05

### Fixed

- **The stop of the bundled DVRIP profile is a *return* now, because the device has no stop
  at all.** The `POINT` payload of 0.3.7 was measured again, this time on a *moving* axis
  with one DVRIP connection for park, sweep and stop (`.smoke/session_probe.py`), and it
  does not halt a sweep: this device answers it with `Ret: 118` - the `Parameter` of a
  payload is 0 based and carried the channel of the camera - and where the body *is*
  accepted (`Channel: 0`) the axis travels on. So does every other shape: a `Stop` of any
  kind (with `Step: 0`, with the whole parameter set of the SDK, without a parameter at
  all, with `AUX` off) is acknowledged with `Ret: 100` and ignored, `DirectionX` with
  `Step: 0` drives that axis to its zero position instead of halting it, and a `ZoomTile`
  of another axis does not touch it either. The reason is in the vendor SDK itself: its PTZ
  JSON builder references the fourteen moves and the keys of the parameter object and *no*
  "Stop" at all - the flag that stands for one is an argument of its API (`bStop`) - and
  the reference library of the family has no stop either. Every one of those runs ended at
  the bottom limit of the camera (`vs top` 59 to 70).

  What does end a sweep is a **preset**. A move of this profile therefore stores the
  position it starts from first, and its stop is the `GotoPreset` of that slot - a DVRIP
  command may carry several payloads now, and they are sent in that order:

  ```
  DVRIP {"Command":"SetPreset","Preset":200,"Channel":1} DVRIP {"Command":"DirectionLeft","Step":5,"Channel":1}
  DVRIP {"Command":"GotoPreset","Preset":200,"Channel":1}
  ```

  Measured on the camera of the test set (192.168.20.253, `balkon-2`): every other stop
  left the camera at its bottom limit, this pair brought it back to its parked position
  (`vs top` 2.9 to 5.5) - whether the slot was written right before the sweep or by an
  earlier run. The slot is `200`, far above the presets the add-on offers: `SetPreset` of
  that slot was accepted and worked, a slot that no run ever wrote answers in 0.1 s and
  moves nothing at all, and `ClearPreset` releases one again.
- **A camera that stored the commands of an older release is moved over to that pair**, its
  moves included - without the store a stop has nothing to return to. The shapes of 0.3.5
  to 0.3.7 are listed in `LEGACY_PROFILE_COMMANDS`, so nothing has to be configured by hand
  again.

### Changed

- A DVRIP command of a camera may carry more than one payload, separated by the method
  word, and the add-on as well as the integration send them in the order they are written.
  A hand written command with a single payload is unchanged.

## [0.3.7] - 2026-10-05

### Fixed

- **The stop of the bundled DVRIP profile was ignored while a move was running.** The
  payload of 0.3.6, `{"Command":"Stop","Step":0,"Channel":1}`, is answered with `Ret: 100`
  and it leaves a *standing* camera where it is, but a device of this family ignores it
  while a `DirectionLeft`/`DirectionUp` sweep is running - the axis travels on to its end.
  The DVRIP variant of the vendor app stops with the nested form below instead, with every
  axis at level zero:

  ```json
  {"Name":"OPPTZControl","OPPTZControl":{"Command":"Stop","Parameter":{"POINT":{"bottom":0,"left":0,"right":0,"top":0},"Step":0,"Channel":1}}}
  ```

  Measured on the DVR of the test set (192.168.20.253) with `.smoke/track.py balkon-2
  stop-point` (fresh frames out of the stream, one sample every 0.5 s, speed 1, a full tilt
  sweep of that device lasts about 5.2 s): a sweep that was stopped after 2.5 s ended after
  **2.96 s** with the nested payload - the camera halted in the middle of the axis - but
  after **5.16 s** without a stop and after **5.39 s** with the payload of 0.3.6, which is
  no effect at all. A payload that carries `Name` is handed to the device unchanged (see
  `dvrip.ptz_payload`), so the channel inside `Parameter` is the one of the camera.
- **The same stop is used where a stored command set is upgraded.** A camera that was
  configured before 0.3.7 keeps its commands in the camera file, so the new stop also
  replaces the template of the stop of 0.3.5 (`{"Command":"{direction}","Step":0,...}`,
  which the device read as a movement) and the one of 0.3.6
  (`{"Command":"Stop","Step":0,...}`) instead of only a single one of them. A hand written
  stop is left alone, as before.

### Verified

- **The stop was measured three ways on the device.** `.smoke/track.py <camera>
  stop-same-direction|stop-guessed-up|stop-command|stop-point|no-stop|long-down` replays a
  move and one variant of a stop for anyone who wants to see it again; `no-stop` and
  `stop-point` are the two runs quoted above, and `stop-command` is the payload of 0.3.6.
  The protocol notes of the harness, `.smoke/dvrip-protocol.md`, list the payloads and the
  answers of the device.

## [0.3.6] - 2026-10-05

### Fixed

- **A stop drove the camera to the very top instead of stopping it.** The direction of a
  stop was taken from the *first* code of the profile whenever the caller did not pass
  one, and for Xiongmai that is `up`: the payload became
  `{"Command":"DirectionUp","Step":0,...}`. On the DVR of the test set (192.168.20.253)
  that is not a stop but a movement - `Step: 0` with a direction drives that axis to its
  zero position, and the zero position of "up" is the top. Measured with the scratch
  tracking tool `.smoke/track.py` (fresh frames of the stream, the grey row profile of the
  picture, one sample every 2.5 s): after 7 s of `DirectionDown` the tilt
  read `+24 +24 +24` (the camera tilting down); the stop that named `DirectionUp` turned
  that into `-24 -24` and the camera settled **at the top** again (`mad` 1.1, the noise of
  a still camera), while the stop that names the moving direction (`DirectionDown`) and
  the device's own `{"Command":"Stop","Step":0}` both left it where it was (`tilt 0`,
  `mad` 2.1 for the next 7 s). Nothing about that is the camera: after 12 s of `down`,
  which is far past its bottom limit, it stays about 75 grey levels away from the top view
  for 20 s instead of travelling back. Two changes follow from it: a stop that does not name
  the axis that is moving is **refused** (`ptz_direction_required`) instead of guessed,
  and the axis of the last move is remembered - the *PTZ stop* button of a dashboard, the
  stop button of the panel and `move_mode: Stop` therefore stop what is moving. The
  bundled Xiongmai DVRIP profile stops with `{"Command":"Stop","Step":0,...}`, which needs
  no direction at all (it stops a pan as well: `mad` 30 and 22 while panning, 2.5 right
  after the stop).
- **A command set that was stored earlier keeps an old template.** The commands of a
  camera live in the camera file, so a fix inside a bundled template would never reach a
  camera that was configured before it. A stored command that is exactly an old template
  of this project is now replaced by the current one; hand written commands stay
  untouched.

### Added

- **The two arrows can move at their own speed.** The PTZ block gained *Speed up/down*
  and *Speed left/right* next to the general speed (both default to it, so a camera that
  does not set them moves exactly as before). The panel shows both fields in the camera
  editor and in the preview pad, and the pad sends the speed of the arrow it belongs to;
  the placeholders `{speed_vertical}` and `{speed_horizontal}` make the same available to
  hand written commands, and the bundled Xiongmai DVRIP profile uses them for its tilt and
  its pan commands. A `speed` that a `rtsp_cameras.ptz` call passes still counts for both
  axes, because such a call asks for the speed of the whole move.

### Verified

- **The stop was measured three ways on the device.** `.smoke/track.py <camera>
  stop-same-direction|stop-guessed-up|stop-command|no-stop` replays a move and one variant
  of a stop for anyone who wants to see it again; the scenarios `stop-guessed-up` (the old
  payload) and `stop-command` (the new one) are the two runs quoted above. The camera
  never moved on its own: 25 s without a command left the picture at `mad` 2.8-3.0 and
  `tilt 0`.

## [0.3.5] - 2026-10-05

### Fixed

- **The PTZ payload sent a negative preset, which made the camera ignore every
  command.** `Preset` is a number of 0 and up; the `-1` of the template made the device
  in question answer `Ret: 100` and do nothing at all. Measured on the Xiongmai camera
  of the test set, with the nested key of 0.3.4 in place and with fresh frames taken out
  of the stream *while* the command was held (the frame cache of Home Assistant had
  hidden the movement before): `Preset: -1` left all seven frames unchanged, `Preset: 0`
  panned the camera - a mean difference of 47 to 54 grey levels between two frames 0.7 s
  apart, against 7.3 to 7.9 for a camera that stands still - and the stop command
  brought it back to 8. The same 2x2 matrix over the key of the nested object and the
  value of `Preset` shows that both were needed: only `OPPTZControl` *and* `Preset: 0`
  moved the camera. The Java bean of the vendor SDK (`OPPTZControlBean$Parameter`)
  carries an `int Preset` as well, whose default is 0.

### Added

- **A hand written payload can be checked against the live device.** The scratch tool
  `.smoke/live_ptz_*.ps1` of the repository sends a built in profile or a payload that
  was written by hand, takes fresh frames from the snapshot endpoint of the add-on while
  the command is held and prints the frame to frame difference, so a payload can be
  judged without watching the camera.

### Verified

- **The camera pans through the add-on now, and stops again.** With 0.3.5 on the device
  (`balkon-2`, 192.168.20.253) the built in profile moves it - fresh frames of its stream,
  0.7 s apart, differ by 48 to 54 grey levels while the command is held, against 7.4 to 8
  with the camera still - and `Step: 0` brings that difference back to 8. The same was
  measured through the `rtsp_cameras.ptz` service of the integration, so the buttons of
  the dashboard, the service and the panel of the add-on all move the camera.
- **Both defects were needed, and both are in the payload.** The 2x2 matrix over the key
  of the nested object and the value of `Preset` is in `.smoke/dvrip-protocol.md`: only
  `OPPTZControl` together with `Preset: 0` moves the camera, which is why 0.3.4 alone (the
  key) was not enough.

## [0.3.4] - 2026-10-05

### Fixed

- **The DVRIP PTZ request nested the command under a key no device knows, so a
  Xiongmai camera acknowledged it and stood still.** The JSON of that family carries
  the message name twice - `{"Name": "OPPTZControl", "OPPTZControl": {...}}` - because
  the device looks the structure up by that name. In the vendor SDK the pair is one
  string: `MNetSDK::CProtocolNetIP::NewPTZControlPTL` of `libFunSDK.so` loads
  `OPPTZControl` once as the value of `Name` and once as the key of the nested object
  (the `adrp`/`add` pairs at 0xF197E8 and 0xF1981C both point at 0x58D3A9), and the
  Java layer keeps it as a single constant,
  `OPPTZControlBean.OPPTZCONTROL_JSONNAME = "OPPTZControl"`. The payload of the add-on
  nested it under `PTZControl` instead: the device knew the message `Name`, answered
  `Ret: 100` and ignored the command, which is why PTZ worked in the vendor app and not
  through the add-on. The nested key is the message name now - the same string in both
  places.

### Added

- **A hand written payload is sent as it is.** A `custom` profile command whose JSON
  already carries `Name` - the shape of a request captured from the vendor app - is
  passed through unchanged, with only the session of the login filled in. That is the
  way to drive a device whose commands the built in profile does not know.

## [0.3.3] - 2026-10-04

### Fixed

- **Choosing a vendor preset in the camera editor changes the commands now.** The
  *vendor / command set* select only replaced the hint line under it, so the command
  fields kept the commands of the preset that was selected before: a user who switched to
  `Xiongmai DVRIP (TCP 34567)` still had the HTTP commands in the form, saved them, and
  the camera never saw a DVRIP command. Picking a preset fills every command in now and
  clears the actions that preset does not define, so no command of the old vendor
  survives. *Fill commands from the profile* stays for the case that the address, the
  credentials or the channel were changed afterwards, and the `custom` profile - which
  defines no command at all - leaves the fields alone instead of throwing away a command
  that was written by hand.
- **The PTZ test mode asks DVRIP before the vendor CGIs.** Measured on a Xiongmai camera
  (192.168.20.253): its own `/cgi-bin/ptz.cgi` answers `200 OK` for *every* code - for a
  command name that does not exist as well - without moving anything, so the HTTP variant
  won the test mode and was stored as the main PTZ handling although its `200 OK` proves
  nothing. DVRIP is asked second now (ONVIF first), because it is the only variant whose
  answer comes out of the device itself: the device has to accept the login **and** answer
  the PTZ request. A stream URL of the Xiongmai shape asks its DVRIP variant before the
  Xiongmai HTTP codes as well.

### Verified

- **The DVRIP client of 0.3.2 is right: a real device accepts it.** Against the camera of
  the test set, the login with the credentials of the RTSP URL is answered with `Ret: 100`
  and a wrong password with `login_failed_203`, and the PTZ request is answered with the
  message id `1401` - header, framing, login types and the `XMMD5Encrypt` hash are all
  correct. This particular device acknowledges **every** PTZ command with `Ret: 100`,
  including a command name that does not exist, and stands still: still images taken from
  Home Assistant before and after each of eleven candidates (six DVRIP variants, five HTTP
  ones) show no movement at all. Its interface answers, its PTZ does not - which is a
  property of the device, not of the protocol (`.smoke/dvrip-protocol.md` has the
  measurements).

## [0.3.2] - 2026-10-02

### Fixed

- **DVRIP (TCP 34567) speaks the protocol of the devices now - the header and the
  password hash were both wrong, so a Xiongmai camera answered nothing at all.**
  Measured against the vendor SDK (`libFunSDK.so` of the Android FunSDK, see
  `DVRIP_MSG_HEAD_T`, `MNetSDK::CProtocolNetIP::InitMsg` and `XMMD5Encrypt`): the
  20 byte header carries the message type as a 16 bit value at **offset 14** and the
  length of the JSON payload as a 32 bit value at **offset 16** (`0xFF`, version
  `0x01`, session, sequence, two flag bytes). The add-on had the two fields swapped, so
  a device read `type 0`, waited for the payload of a message that never came and closed
  the connection - that is exactly the "accepted and closed again, no answer" the test
  mode kept reporting. The `PassWord` of the login is neither the password nor its
  double MD5 either: it is the eight character hash of `XMMD5Encrypt`, built from the
  MD5 digest (the two bytes of every pair are added, the sum is taken modulo 62 and
  written as `0-9A-Za-z`; the user name is not part of it). The login types of the
  family (`DVRIP-Web`, `DVRIP-Mobile`, `DVRIP-Xm030`) are tried in that order, so a
  device that rejects the one that is asked first is still reached, and answers that a
  device pads with NUL bytes or line ends are parsed.
- **A camera that says which family it belongs to through its stream URL is asked the
  matching PTZ variant first.** A Xiongmai device (stream URLs shaped like
  `…/user=admin&password=secret&channel=1&stream=0.sdp`) answers the *stop* command of
  the Dahua profile with `200 OK`, so the test mode stored the Dahua commands and every
  arrow of the panel moved nothing. The shape of the stream URL now decides which
  variant is asked first - `xiongmai` (and `xiongmai_dvrip`) before `dahua`,
  `/Streaming/Channels/…` before the rest, `?channel=…&subtype=…` for Dahua - so the
  commands that really move the camera are the ones that get stored.
- **A command that is answered with `200 OK` and a failure in the body is reported
  instead of being taken for a success.** Dahua and Xiongmai answer a rejected command
  (wrong code, wrong channel, credentials that do not fit) with `200 OK` and `Error`.
  The panel and Home Assistant report that as `ptz_failed` **with the answer of the
  camera next to it** instead of showing nothing while the camera stands still. A body
  that consists of nothing but a failure word (`Error`, `failed`, …) no longer counts as
  a variant that answered in the PTZ test mode either.
- **A command URL that is answered with the web page of the camera counts as a failure
  now.** Xiongmai cameras serve their web interface on port 80 and answer *every* path
  with `200 OK` and HTML - the CGI paths of the vendor profiles included. Measured on a
  live device: all seven variants of the test mode looked like a transport that
  answered, the first one (Dahua) was stored, and none of its commands moved anything.
  An answer that is a web page is now treated like `Error`: reported as `ptz_failed` and
  never a winner of the PTZ test mode.

## [0.3.1] - 2026-10-02

### Fixed

- **The live preview of an H.265/HEVC camera stayed black and reported
  `stream_failed`, although the camera and its stream were fine.** The transcoding HLS
  path left the encoder on the default keyframe interval of libx264 (250 frames). An HLS
  segment can only end on a keyframe, so at the configured `preview_fps` the first
  segment lasted up to 50 seconds and the playlist carried no playable segment at all.
  `preview_fps` now also sets the keyframe interval (`-g` and `-keyint_min` to
  *fps × 1 second*, `-sc_threshold 0`), so every segment is one second long and the
  preview starts playing.
- **A preview mode is no longer declared working when nothing can be played.** A source
  that opens but never delivers a frame was muxed into a single zero length segment
  (`#EXTINF:0.000000`); the playlist check asks for a segment with a real duration now.
  `POST /api/cameras/<id>/hls/start` and `POST /api/cameras/<id>/preview/detect` answer
  with `report` (what ffmpeg printed) and `attempts` (mode and RTSP transport of every
  try) instead of a bare `stream_failed`.
- **A failed stream test names its cause instead of the shutdown noise of ffmpeg**
  (`Task finished with error code: -22`, `Conversion failed!`): the last 40 lines of the
  process are kept and the first line that carries a reason is reported. An ffmpeg that
  keeps running without producing anything is reported as well - its output is read
  while the process lives, so a camera that never delivers a picture no longer stays a
  silent failure.
- **The playlist and the segments of a preview are read before the response is built.**
  ffmpeg rewrites `index.m3u8` every second while the response is on its way, so a
  `Content-Length` taken from the file on disk could outgrow the body and h11 aborted
  the connection with "Too little data for declared Content-Length" - a dead stream as
  far as the player is concerned. The answers are also sent with
  `Cache-Control: no-store`.
- **A PTZ command whose URL carries the credentials as user information**
  (`http://user:password@192.168.20.253/cgi-bin/ptz.cgi?...`, which is what a hand
  written custom command usually looks like) ended in
  `http.client.InvalidURL: nonnumeric port` and an HTTP 500. The credentials are moved
  into an `Authorization: Basic` header before the request is sent. A command URL that
  still cannot be sent is reported as `ptz_failed` (HTTP 502 with the reason) instead of
  an unhandled exception, and it is masked in the panel
  (`http://***:***@camera/...`) like every other command.

### Changed

- When the `codec` that ffprobe reported and the stream disagree (a camera that switched
  between H.264 and H.265 keeps its first report), the HLS preview tries the other
  handling (copy ↔ encode) before it gives up: an extra encode is cheaper than a preview
  that never arrives. The answer of a working preview names the handling it used
  (`transcode`).
- The browser note "MJPEG does not arrive here" expires after ten minutes instead of
  sending this browser to HLS for good, and the wait for the first MJPEG frame follows
  the configured `test_timeout` (at least eight seconds): the add-on itself waits for
  that frame before it answers, so the browser has to wait at least as long.

## [0.3.0] - 2026-09-30

### Added

- **PTZ test mode: the add-on finds out which PTZ variant works with the camera.**
  The camera editor has a new *PTZ test mode* block - enter the address of the camera
  (an **IP is enough**, `http://` is added, empty means the host of the RTSP URL) and
  press *Test every PTZ variant*. The address is asked variant by variant: ONVIF, the
  vendor CGIs (Dahua/Amcrest, Hikvision ISAPI, Axis VAPIX, Foscam, Xiongmai) and
  DVRIP on TCP 34567. The **first variant that answers is stored as the main PTZ
  handling of the camera** - profile, HTTP address, channel, port, ONVIF profile
  token and the credentials that were used - and the form follows, so a later *Save*
  cannot undo it. Home Assistant takes over the new commands within its scan
  interval, without a restart.
  - Nothing moves during the test: every HTTP and DVRIP variant is asked with its
    **stop** command, ONVIF with the read only queries `GetConfigurations`,
    `GetCapabilities(PTZ)` and `GetProfiles`.
  - The result list shows every variant with the reason: *answers*, *needs
    credentials* (a 401 means the interface exists, the password does not fit),
    *not supported*, *no ONVIF profile token*, *no answer (timeout)* or *not
    reachable*. A variant that only refuses the credentials is never applied
    automatically - the report says what to correct instead.
  - The presets of the camera survive a test; `apply: false` (the API field) reports
    the winner and changes nothing.
  - New API endpoint `POST /api/ptz/probe` (also at
    `POST /api/cameras/<id>/ptz/probe`), the panel uses it through the ingress.
- The camera entities of the integration expose the variant in use as the
  **`ptz_profile` attribute**, next to `ptz` and `ptz_presets` - a glance at the
  entity shows which command set was detected.

### Changed

- `app.ptz.async_send` keeps the answer of a camera (`async_send_detail`), because
  several vendor CGIs report a failure with `200 OK` and an error code in the body
  (`result=-1`, `result=-3`); the test mode now reports such a variant as *needs
  credentials* / *not supported* instead of *answers*.
- The credential masking of the PTZ editor is a shared helper (`mask_credentials`),
  so the commands shown in the test result are masked like the camera editor does it.

## [0.2.4] - 2026-09-25

### Changed

- **The brand icon now is the camera mark the add-on panel uses** (`.brand__mark` in
  `app/templates/index.html`): the stroked rounded rectangle body, the camera cone on
  the right, the filled lens and the two signal arcs, drawn in the accent cyan
  `#22d3ee`. The generator reproduces that SVG geometry on the 32x32 view box of the
  panel (including the arc centres, which follow from the chord and the radius), so
  the integrations dashboard and the add-on look like the same product.
- Both theme variants (`icon*.png` and `dark_icon*.png`) carry the same accent glyph:
  cyan is readable on light and dark backgrounds, so no separate dark artwork is
  needed and the icon never changes its look with the theme.
- The artwork was verified visually at 512 px before it was committed.

## [0.2.3] - 2026-09-25

### Added

- **The status strip of the panel now shows the version of the integration inside
  Home Assistant**, next to the add-on version. An outdated copy is impossible to miss:
  the chip turns amber and reads e.g. `0.1.22 → 0.2.3` (or *restart HA* after an
  install), and clicking it opens the Settings tab. Reason: the integration is what
  Home Assistant loads, so the brand icon, the PTZ services and the preset buttons
  only appear after the add-on has copied the new integration **and** Home Assistant
  was restarted.
- The add-on logs the comparison at startup: the version it installs *and* a warning
  `Integration 0.2.3 is in /config/custom_components/rtsp_cameras - restart Home
  Assistant to load it`, plus an error when the copy failed. Previously this was only
  visible in the panel.

## [0.2.2] - 2026-09-25

### Added

- **The integration now has its own icon** on the Home Assistant integrations
  dashboard (`/config/integrations/dashboard`) and on its integration page. Home
  Assistant serves local brand assets from a `brand` folder inside the integration
  (`Integration.has_branding`); the icon is drawn for both themes: `icon.png` /
  `icon@2x.png` (dark camera for light themes) and `dark_icon.png` /
  `dark_icon@2x.png` (light camera for dark themes), each 256x256 and 512x512 RGBA
  PNG. A camera body with a cyan lens ring and a rotation arc stands for the RTSP
  stream and for PTZ.
- The artwork is drawn by `scripts/make_brand_icons.py` (numpy + PyAV, deliberately
  no image library) and travels with the add-on image, because the add-on installs
  the integration into `/config`.

### Tests

- `rtsp_cameras/tests/test_branding.py` (7 tests): the folder exists, every image is
  a square 8 bit RGBA PNG of the expected size, the dark variant differs from the
  light one and both copies (repository and add-on) are byte identical.
- `ha_tests/test_branding.py`: uses Home Assistant's own loader and asserts that
  `has_branding` is true and that all four images sit where the brands view looks for
  them - so a renamed or missing folder fails in CI instead of in the browser.
  (The attribute is checked with `hasattr`, because not every Home Assistant release
  exposes it; the file check always runs.)
- `ruff` is now pinned in `requirements-dev.txt`: the unpinned version resolved to a
  newer release on CI than locally, which made the lint step disagree about import
  order in the new test file.

### Notes

- Existing installations show the icon on the integrations dashboard right away;
  browsers cache brand images, so a hard refresh (Ctrl+F5) may be needed after the
  update.

## [0.2.1] - 2026-09-25

### Added

- **DVRIP (Xiongmai "Sofia") PTZ on TCP 34567.** The Xiongmai DVRIP profile talks the
  binary protocol of DVRs and NVRs that have no web interface at all: a 20 byte
  header with a JSON payload, a login with the double MD5 hash of the password and
  `OPPTZControl` commands (`DirectionLeft`, `ZoomTile`, `GotoPreset`, … with a `Step`
  of 0 to stop). Every command logs in, sends and disconnects, so the add-on stays
  stateless. Verified end-to-end against a fake DVR: login `admin` with the hash of
  the stream URL password, `DirectionLeft` with `Step 5`, stop with `Step 0`.
- **ONVIF (SOAP) PTZ.** The ONVIF profile posts `ContinuousMove`, `Stop`,
  `GotoPreset` and `GotoHomePosition` envelopes to the PTZ service. A new *Discover
  the ONVIF token* button sends `GetProfiles` (with basic auth when needed) to the
  media service, takes the first profile token and fills the commands with it. SOAP
  requests are sent with `application/soap+xml`, vendor XML keeps `application/xml`.
- **Credentials and channel are inherited from the RTSP URL.** Both
  `rtsp://user:pass@host/…` and the query style of many DVRs
  (`…/user=admin&password=secret&channel=1&stream=0.sdp`) are parsed, so the login is
  never typed twice; explicit PTZ credentials still win. The REST API only reports
  whether credentials exist, never the password itself.
- A stop command without a direction (a plain `stop` from an automation) is filled
  with the first direction code of the profile, which keeps Dahua/Xiongmai and DVRIP
  stops valid.

### Tests

- 11 more add-on tests: DVRIP login/PTZ/refused login against a **fake DVRIP device**
  (real TCP handshake), ONVIF token discovery against a SOAP answering server, the
  SOAP content type, credential inheritance for both URL styles, DVRIP payload
  rendering and the stop fallback - 189 add-on tests in total.
- 4 more Home Assistant tests: DVRIP commands go through the TCP client (never
  HTTP), the automatic stop carries the direction with the scaled speed, a refused
  login raises a readable error and ONVIF commands are POSTed as SOAP.

## [0.2.0] - 2026-09-25

### Added

- **PTZ support (pan, tilt, zoom).** A camera with PTZ is configured in the add-on
  panel: expand *PTZ control*, tick the switch, pick a vendor preset and press *Fill
  commands from the profile*. The presets cover Axis (VAPIX), Dahua/Amcrest,
  Xiongmai/NETSurveillance (the family with
  `rtsp://host:554/user=admin&password=&channel=1&stream=0.sdp` URLs), Hikvision
  (ISAPI, `PUT` with an XML body) and Foscam; every command stays editable so unusual
  firmware works too (`GET`/`POST`/`PUT url [body]`, placeholders `{base}`,
  `{username}`, `{password}`, `{channel}`, `{speed}`, `{preset}`, `{direction}`,
  `{seconds}`). Presets are entered as `number=name`.
- **PTZ pad in the add-on preview.** Hold an arrow to move, release to stop (the
  direction is remembered for the stop command), plus zoom, home, stop and a preset
  chooser with the camera speed. *Test PTZ* sends only the stop command, so the test
  never moves the camera, and a camera without an HTTP interface is reported clearly.
- **Home Assistant services** `rtsp_cameras.ptz` and `rtsp_cameras.ptz_home`. The
  fields match `onvif.ptz` (`pan`, `tilt`, `zoom`, `speed` 0.01-1,
  `continuous_duration`, `preset`, `move_mode`), so an automation written for ONVIF
  keeps working after changing the domain; `action` accepts the plain add-on actions
  as well. `move_mode: GotoPreset` needs a preset, `move_mode: Stop` stops, and a
  direction moves for `continuous_duration` seconds (default 0.5) before it stops
  itself. Everything has real translations (English, Polish, German, Spanish) and a
  `services.yaml` for the UI.
- **One button per PTZ preset** (plus *PTZ stop*) on the camera device, so a
  dashboard can jump to a view with a single tap.

### Tests

- 18 add-on tests for the PTZ core and the API, including a fake camera HTTP server
  that records what the camera receives (start/stop with the direction, presets,
  `PUT` with body, error paths, credential encoding, masking).
- 11 real Home Assistant tests for the services, the ONVIF-style field mapping, the
  speed scale, the preset buttons, missing PTZ configuration and camera failures.
- End-to-end check in the container: a Xiongmai profile against a fake camera
  interface sends `action=start&code=DirectionLeft&arg2=6`, the stop carries
  `code=DirectionLeft`, the preset ends in `code=GotoPreset&arg2=2` and the
  published `cameras.json` contains the commands and the stop codes.

## [0.1.22] - 2026-09-24

### Added

- **The add-on now explains a "green" stream test that still cannot be
  previewed.** RTSP over UDP can answer the RTSP options request while no packet
  ever arrives: ffprobe then reports success with a codec but without resolution,
  frame rate or pixel format - *Test stream* is green and every preview stays
  black. That case is now detected (`hint: empty_stream_details`, only when the
  transport is not TCP) and shown under the test result and as a warning toast
  (`camera.hint_empty_stream_details`, all four languages) with the advice to use
  TCP - which the preview already does on its own since 0.1.19.
- Verified against a real camera (H.264, 1280x720, 30 fps) whose UDP transport
  delivers nothing: the stream test is green, `preview/detect` reports
  `No frame within 10 seconds` for MJPEG and `Output file does not contain any
  stream` for HLS over UDP and then successfully switches to TCP.
- New fake ffprobe mode `no_details` and three tests for the hint.

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
