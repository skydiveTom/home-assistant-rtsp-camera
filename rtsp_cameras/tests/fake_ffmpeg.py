"""Stand-in for ffmpeg used by the add-on test suite.

The behaviour is selected with the FAKE_FFMPEG_MODE environment variable:
snapshot (default), mjpeg, hls, mixed, fail, sleep.

``mixed`` emulates a camera whose frames cannot be decoded (MJPEG fails) while
HLS works, which is what the automatic preview detection has to cope with.
"""

from __future__ import annotations

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_FFMPEG_MODE", "snapshot")
ARGS_FILE = os.environ.get("FAKE_ARGS_FILE")
FRAMES = int(os.environ.get("FAKE_FRAMES", "3"))
SLEEP_SECONDS = float(os.environ.get("FAKE_SLEEP", "30"))

# Minimal JPEG: SOI, APP0/JFIF header, EOI marker.
JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
)


def record_arguments(command: list[str]) -> None:
    """Store the command line so tests can assert the ffmpeg options."""
    if not ARGS_FILE:
        return
    with open(ARGS_FILE, "w", encoding="utf-8") as handle:
        json.dump(command, handle)


def write_bytes(data: bytes) -> None:
    """Write raw bytes to stdout without any text encoding."""
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def output_format(command: list[str]) -> str:
    """Return the format ffmpeg was asked to write."""
    if "-f" in command:
        index = command.index("-f")
        if index + 1 < len(command):
            return command[index + 1]
    return ""


def input_transport(command: list[str]) -> str:
    """Return the RTSP transport ffmpeg was asked to use."""
    if "-rtsp_transport" in command:
        index = command.index("-rtsp_transport")
        if index + 1 < len(command):
            return command[index + 1]
    return ""


def write_hls(command: list[str]) -> int:
    """Create a tiny playlist plus one segment, like the real HLS muxer."""
    playlist = command[-1]
    directory = os.path.dirname(playlist)
    os.makedirs(directory, exist_ok=True)
    with open(playlist, "w", encoding="utf-8") as handle:
        handle.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1.0,\nsegment_000.ts\n")
    with open(os.path.join(directory, "segment_000.ts"), "wb") as handle:
        handle.write(b"\x47" * 188)
    time.sleep(SLEEP_SECONDS)
    return 0


def main() -> int:
    """Emulate ffmpeg for the requested mode."""
    command = sys.argv[1:]
    record_arguments(command)

    if MODE == "fail":
        sys.stderr.write("rtsp://camera.local:554/stream: Connection refused\n")
        return 1
    if MODE == "sleep":
        time.sleep(SLEEP_SECONDS)
        return 0
    if MODE == "snapshot":
        write_bytes(JPEG)
        return 0
    if MODE == "mjpeg":
        for _ in range(FRAMES):
            write_bytes(JPEG)
            time.sleep(0.05)
        return 0
    if MODE == "hls":
        return write_hls(command)
    if MODE == "mixed":
        if output_format(command) in ("mjpeg", "image2", "image2pipe"):
            sys.stderr.write(
                "rtsp://camera.local:554/stream: Invalid data found when processing input\n"
            )
            return 1
        return write_hls(command)
    if MODE == "udp-fail":
        # Emulates a camera whose UDP packets get lost: only TCP produces frames.
        if input_transport(command) == "udp":
            sys.stderr.write(
                "rtsp://camera.local:554/stream: RTP: dropping old packet received too late\n"
            )
            return 1
        if output_format(command) in ("mjpeg", "image2", "image2pipe"):
            for _ in range(FRAMES):
                write_bytes(JPEG)
                time.sleep(0.05)
            return 0
        return write_hls(command)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
