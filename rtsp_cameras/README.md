# RTSP Camera Manager

Add RTSP camera instances to Home Assistant **using nothing but the stream URL**.

Give the camera a name, paste the URL, press **Test stream** to see whether it
works, take a look at the live picture with **Quick preview** — and use the
camera on any dashboard, because every instance turns into a regular
`camera` entity.

[![Open your Home Assistant instance and show the add-on store.](https://my.home-assistant.io/badges/supervisor_store.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FskydiveTom%2Fhome-assistant-rtsp-camera)

## Features

- **URL only** – no YAML, no ONVIF scan, no vendor app. `rtsp://`, `rtsps://`,
  `rtmp://` and `http(s)://` MJPEG streams are accepted.
- **Stream test** – `ffprobe` reports codec, resolution, frame rate, bit rate and
  the exact error when the stream fails.
- **Quick preview** – live view inside the add-on, as MJPEG (works everywhere) or
  HLS (stream copy for H.264 cameras, barely any CPU).
- **Camera entities** – a bundled integration turns every instance into a
  `camera.<name>` entity with native Home Assistant streaming, so the camera card
  works like it does for any other camera.
- **Four languages** – English, German, Spanish and Polish. The interface follows
  the language configured in Home Assistant, English is the default.
- **Safe by design** – the interface is only reachable through the authenticated
  Home Assistant ingress and credentials in stream URLs are masked in the list
  and in the log.

## Quick start

1. Add this repository to the add-on store and install **RTSP Camera Manager**.
2. Start the add-on and open **RTSP Cameras** in the sidebar (or *Open web UI*).
3. Press **Add camera**, give it a name, paste the RTSP URL and use **Test
   stream** to verify it.
4. Save. The add-on installs the matching integration into
   `/config/custom_components/rtsp_cameras` — restart Home Assistant once when
   asked and the `camera` entity is ready for your dashboard.

Detailed documentation lives in [DOCS.md](DOCS.md).

## Screenshots

The interface shows every camera as a monitor tile with live status, a lazy
loaded snapshot, the negotiated stream parameters and buttons for preview, test,
edit and delete.

## Support

- Repository: <https://github.com/skydiveTom/home-assistant-rtsp-camera>
- Issues: <https://github.com/skydiveTom/home-assistant-rtsp-camera/issues>

Licensed under the GNU General Public License v3.0.
