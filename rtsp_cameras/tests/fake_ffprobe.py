"""Stand-in for ffprobe used by the add-on test suite.

The behaviour is selected with the FAKE_PROBE_MODE environment variable:
ok (default), fail, invalid, no_video, sleep.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_PROBE_MODE", "ok")
ARGS_FILE = os.environ.get("FAKE_ARGS_FILE")
SLEEP_SECONDS = float(os.environ.get("FAKE_SLEEP", "30"))

PROBE_OUTPUT = {
    "streams": [
        {
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "25/1",
            "pix_fmt": "yuv420p",
            "bit_rate": "2048000",
        }
    ],
    "format": {"format_name": "rtsp", "duration": "N/A"},
}


def record_arguments() -> None:
    """Store the command line so tests can assert the ffprobe options."""
    if not ARGS_FILE:
        return
    with open(ARGS_FILE, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)


def main() -> int:
    """Emulate ffprobe for the requested mode."""
    record_arguments()

    if MODE == "sleep":
        time.sleep(SLEEP_SECONDS)
        return 0
    if MODE == "fail":
        sys.stderr.write(
            "rtsp://user:secret@camera.local:554/stream: Server returned 401 Unauthorized\n"
        )
        return 1
    if MODE == "invalid":
        sys.stdout.write("this is not json")
        return 0
    if MODE == "no_video":
        sys.stdout.write(json.dumps({"streams": [], "format": {"format_name": "rtsp"}}))
        return 0

    sys.stdout.write(json.dumps(PROBE_OUTPUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
