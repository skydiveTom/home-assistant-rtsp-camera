"""ffmpeg based helpers: stream testing, snapshots and preview streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .models import redact_credentials, redact_url

_LOGGER = logging.getLogger(__name__)

STREAM_CHUNK = 64 * 1024
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
#: How many lines of ffmpeg output are kept for error reporting.
STDERR_TAIL_LINES = 40
#: Length of one HLS segment in seconds.
HLS_SEGMENT_SECONDS = 1
#: ffmpeg prints the reason for a failure first and then the shutdown noise of
#: its encoders ("Task finished with error code: -22"), so the last lines alone
#: would hand the panel the noise instead of the cause.
_ERROR_CAUSE = re.compile(
    r"\bError\b|Invalid data|Connection (refused|reset|timed out)|Operation timed out|"
    r"No such file|Permission denied|Non-monotonous DTS|Server returned|not found"
)
_ERROR_NOISE = re.compile(
    r"Task finished with error code|Terminating thread with return code|"
    r"Nothing was written into output file|Conversion failed"
)
_ERROR_SCAN_LINES = 12


def _scale_filter(max_height: int) -> str:
    """Return a scale filter that keeps the aspect ratio and caps the height."""
    return f"scale=-2:'min({max_height},ih)'"


def fraction_to_float(value: Any) -> float | None:
    """Convert an ffprobe frame rate fraction (25/1) into a float."""
    text = str(value or "").strip()
    if not text or text in ("0/0", "N/A"):
        return None
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            divisor = float(denominator)
            return round(float(numerator) / divisor, 2) if divisor else None
        except ValueError:
            return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def extract_jpeg_frames(buffer: bytearray) -> Iterator[bytes]:
    """Pop complete JPEG frames off a growing buffer."""
    frames: list[bytes] = []
    while True:
        start = buffer.find(JPEG_START)
        if start < 0:
            buffer.clear()
            break
        end = buffer.find(JPEG_END, start + len(JPEG_START))
        if end < 0:
            if start > 0:
                del buffer[:start]
            break
        frames.append(bytes(buffer[start : end + len(JPEG_END)]))
        del buffer[: end + len(JPEG_END)]
    return iter(frames)


@dataclass(slots=True)
class ProbeResult:
    """Outcome of an ffprobe run against a stream URL."""

    ok: bool
    code: str = "ok"
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON serialisable representation."""
        return {
            "ok": self.ok,
            "code": self.code,
            "error": self.error,
            "details": self.details,
        }


@dataclass(slots=True)
class HlsSession:
    """A running ffmpeg process that writes HLS segments to disk."""

    camera_id: str
    directory: Path
    process: asyncio.subprocess.Process
    touched: float
    #: Last lines ffmpeg wrote to stderr, kept for error reporting.
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    #: Task that fills ``log_lines`` while the process is alive.
    drain: asyncio.Task[None] | None = None

    @property
    def playlist(self) -> Path:
        """Return the playlist path of this session."""
        return self.directory / "index.m3u8"

    @property
    def segments(self) -> list[Path]:
        """Return the segments written so far."""
        return sorted(self.directory.glob("segment_*.ts"))


def binary_command(name: str) -> list[str] | None:
    """Return the command that runs a binary, or None when it is unavailable."""
    path = shutil.which(name)
    return [path] if path else None


class FFmpegUnavailable(RuntimeError):
    """Raised when ffmpeg or ffprobe is missing from the container."""


class StreamFailed(RuntimeError):
    """Raised when a live stream stopped before it produced a single frame."""


class FFmpegService:
    """Run ffprobe and ffmpeg for stream tests, snapshots and previews."""

    def __init__(self, settings: Settings) -> None:
        """Locate the binaries and prepare the preview directory."""
        self.settings = settings
        self.ffmpeg_bin = binary_command("ffmpeg")
        self.ffprobe_bin = binary_command("ffprobe")
        self.sessions: dict[str, HlsSession] = {}

    @property
    def available(self) -> bool:
        """Return True when both ffmpeg and ffprobe are installed."""
        return bool(self.ffmpeg_bin and self.ffprobe_bin)

    # ------------------------------------------------------------- arguments
    def _input_args(self, url: str, transport: str, timeout: int) -> list[str]:
        """Build the input options shared by all ffmpeg and ffprobe calls.

        Only options that every ffmpeg build accepts are used. ``-rw_timeout`` is
        deliberately not passed: some builds reject it (it made previews fail with
        ``Option rw_timeout not found``) and the Python side already enforces
        timeouts for every call.
        """
        args = ["-hide_banner", "-loglevel", "error"]
        if url.lower().startswith(("rtsp://", "rtsps://")):
            micro = max(1, int(timeout)) * 1_000_000
            args += ["-rtsp_transport", transport or "tcp", "-timeout", str(micro)]
        return args

    def _ffmpeg_input(self, url: str, transport: str, timeout: int) -> list[str]:
        """Return the input options followed by the input URL itself."""
        return [*self._input_args(url, transport, timeout), "-i", url]

    def _clean_error(self, raw: bytes | str) -> str:
        """Return a short error message with any URL credentials removed.

        ffmpeg reports the reason first (``Error during demuxing: I/O error``)
        and then the teardown of its encoders (``Task finished with error code:
        -22``). Taking the last lines blindly therefore hides the cause of a
        failed preview behind the noise that followed it.
        """
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        causes = [
            line
            for line in lines[-_ERROR_SCAN_LINES:]
            if _ERROR_CAUSE.search(line) and not _ERROR_NOISE.search(line)
        ]
        message = " | ".join((causes or lines[-3:])[-3:])
        if not message:
            message = "ffmpeg stopped without an error message"
        if self.settings.redact_credentials_in_logs:
            message = redact_credentials(message)
        return message[:500]

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        """Terminate a child process, close its pipes and reap it."""
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()

    async def _drain_stderr(self, session: HlsSession) -> None:
        """Keep the last lines of ffmpeg output of an HLS session.

        ffmpeg often keeps running without writing a single segment (a camera
        that sends no packets, for example), so its output has to be read while
        it runs - otherwise the failure cannot be explained afterwards.
        """
        stream = session.process.stderr
        if stream is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if text:
                    session.log_lines.append(text)

    # ----------------------------------------------------------------- probe
    async def probe(
        self,
        url: str,
        transport: str = "tcp",
        timeout: int | None = None,
    ) -> ProbeResult:
        """Run ffprobe against the stream and report the video properties."""
        if not self.ffprobe_bin:
            return ProbeResult(
                ok=False,
                code="ffprobe_missing",
                error="ffprobe is not installed in this container",
            )

        timeout = int(timeout or self.settings.test_timeout)
        command = [
            *self.ffprobe_bin,
            *self._input_args(url, transport, timeout),
            "-select_streams",
            "v:0",
            "-show_streams",
            "-show_format",
            "-print_format",
            "json",
            url,
        ]

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout + 5
            )
        except TimeoutError:
            if process is not None:
                await self._stop(process)
            return ProbeResult(
                ok=False,
                code="timeout",
                error=f"No answer from the camera within {timeout} seconds",
            )
        except (OSError, ValueError) as err:
            return ProbeResult(ok=False, code="failed", error=str(err))

        if process.returncode != 0:
            return ProbeResult(ok=False, code="failed", error=self._clean_error(stderr))

        try:
            data: dict[str, Any] = json.loads(stdout.decode("utf-8", "replace") or "{}")
        except ValueError:
            data = {}

        streams = data.get("streams") or []
        if not streams:
            return ProbeResult(
                ok=False,
                code="no_video",
                error="Connected to the camera, but no video stream was found",
            )

        video = streams[0]
        width = video.get("width")
        height = video.get("height")
        bit_rate = str(video.get("bit_rate") or "")
        return ProbeResult(
            ok=True,
            details={
                "codec": video.get("codec_name"),
                "profile": video.get("profile"),
                "width": width,
                "height": height,
                "resolution": f"{width}x{height}" if width and height else None,
                "fps": fraction_to_float(
                    video.get("avg_frame_rate") or video.get("r_frame_rate")
                ),
                "pix_fmt": video.get("pix_fmt"),
                "bit_rate_kbps": round(int(bit_rate) / 1000)
                if bit_rate.isdigit()
                else None,
                "transport": transport or "tcp",
                "format": (data.get("format") or {}).get("format_name"),
            },
        )

    # -------------------------------------------------------------- snapshot
    async def snapshot(
        self,
        url: str,
        transport: str = "tcp",
        max_height: int | None = None,
        timeout: int | None = None,
        quality: int = 4,
    ) -> bytes | None:
        """Grab a single JPEG frame from the stream."""
        if not self.ffmpeg_bin:
            return None

        timeout = int(timeout or self.settings.test_timeout)
        height = int(max_height or self.settings.preview_max_height)
        command = [
            *self.ffmpeg_bin,
            *self._ffmpeg_input(url, transport, timeout),
            "-an",
            "-frames:v",
            "1",
            "-vf",
            _scale_filter(height),
            "-c:v",
            "mjpeg",
            "-q:v",
            str(quality),
            "-f",
            "mjpeg",
            "pipe:1",
        ]

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout + 5
            )
        except TimeoutError:
            if process is not None:
                await self._stop(process)
            _LOGGER.warning("Snapshot timed out for %s", redact_url(url))
            return None
        except (OSError, ValueError) as err:
            _LOGGER.error("Snapshot failed for %s: %s", redact_url(url), err)
            return None

        if process.returncode != 0 or not stdout:
            _LOGGER.debug(
                "Snapshot for %s failed: %s", redact_url(url), self._clean_error(stderr)
            )
            return None
        return bytes(stdout)

    # --------------------------------------------------------------- streams
    async def mjpeg_stream(
        self,
        url: str,
        transport: str = "tcp",
        fps: int | None = None,
        max_height: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield JPEG frames of the live stream until the consumer stops."""
        if not self.ffmpeg_bin:
            raise FFmpegUnavailable("ffmpeg is not installed in this container")

        fps = int(fps or self.settings.preview_fps)
        height = int(max_height or self.settings.preview_max_height)
        command = [
            *self.ffmpeg_bin,
            *self._ffmpeg_input(url, transport, self.settings.test_timeout),
            "-an",
            "-r",
            str(fps),
            "-vf",
            _scale_filter(height),
            "-c:v",
            "mjpeg",
            "-q:v",
            "6",
            "-f",
            "mjpeg",
            "pipe:1",
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        buffer = bytearray()
        yielded = False
        try:
            if process.stdout is None:
                raise FFmpegUnavailable("ffmpeg did not provide an output pipe")
            while True:
                chunk = await process.stdout.read(STREAM_CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                for frame in extract_jpeg_frames(buffer):
                    yielded = True
                    yield frame
            if not yielded:
                stderr = b""
                if process.stderr is not None:
                    with contextlib.suppress(Exception):
                        stderr = await process.stderr.read()
                raise StreamFailed(self._clean_error(stderr))
        finally:
            await self._stop(process)

    # ------------------------------------------------------------------- hls
    def _hls_video_args(
        self,
        codec: str | None,
        transcode: bool | None,
        fps: int | None = None,
        max_height: int | None = None,
    ) -> list[str]:
        """Return the video options of an HLS session.

        ``transcode=None`` keeps the historical rule: an H.264 stream is copied,
        anything else is encoded, because a browser can only play H.264 (and HLS
        cannot carry the MJPEG of a camera). ``True``/``False`` force one of the
        two, so a camera whose codec cannot be detected can be tried both ways.
        """
        if transcode is None:
            transcode = str(codec or "").lower() != "h264"
        if not transcode:
            return ["-an", "-c:v", "copy"]
        height = int(max_height or self.settings.preview_max_height)
        rate = max(1, int(fps or self.settings.preview_fps))
        # A segment can only end at a keyframe, and ``-tune zerolatency`` leaves
        # libx264 at its default GOP of 250 frames: at preview_fps the next
        # keyframe after the start would be 50 seconds away, so the muxer wrote a
        # single huge segment and the panel never got a playable playlist
        # (reported as "stream_failed"). One keyframe per segment keeps the
        # preview alive one segment after the start.
        keyframe_interval = max(1, rate * HLS_SEGMENT_SECONDS)
        return [
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(rate),
            "-g",
            str(keyframe_interval),
            "-keyint_min",
            str(keyframe_interval),
            "-sc_threshold",
            "0",
            "-vf",
            _scale_filter(height),
        ]

    async def start_hls(
        self,
        camera_id: str,
        url: str,
        transport: str = "tcp",
        codec: str | None = None,
        fps: int | None = None,
        max_height: int | None = None,
        transcode: bool | None = None,
    ) -> HlsSession:
        """Start an HLS session for a camera and return it.

        ``transcode`` overrides the video handling derived from ``codec``, which
        lets the caller fall back to a plain copy when encoding is impossible
        (a container without ``libx264``, for example).
        """
        if not self.ffmpeg_bin:
            raise FFmpegUnavailable("ffmpeg is not installed in this container")

        await self.stop_hls(camera_id)
        directory = self.settings.preview_dir / camera_id
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        video_args = self._hls_video_args(codec, transcode, fps, max_height)

        command = [
            *self.ffmpeg_bin,
            *self._ffmpeg_input(url, transport, self.settings.test_timeout),
            *video_args,
            "-f",
            "hls",
            "-hls_time",
            str(HLS_SEGMENT_SECONDS),
            "-hls_list_size",
            "5",
            "-hls_flags",
            "delete_segments+append_list",
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            str(directory / "segment_%03d.ts"),
            str(directory / "index.m3u8"),
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        session = HlsSession(
            camera_id=camera_id,
            directory=directory,
            process=process,
            touched=time.monotonic(),
        )
        self.sessions[camera_id] = session
        # Read stderr while ffmpeg runs: a process that stays alive without
        # writing a segment is otherwise completely silent.
        session.drain = asyncio.create_task(self._drain_stderr(session))
        return session

    def get_session(self, camera_id: str, touch: bool = True) -> HlsSession | None:
        """Return the running HLS session of a camera, if any."""
        session = self.sessions.get(camera_id)
        if session is not None and touch:
            session.touched = time.monotonic()
        return session

    async def stop_hls(self, camera_id: str) -> bool:
        """Stop the HLS session of a camera."""
        session = self.sessions.pop(camera_id, None)
        if session is None:
            return False
        if session.drain is not None:
            session.drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session.drain
            session.drain = None
        await self._stop(session.process)
        return True

    async def reap_hls(self, idle_seconds: int = 45) -> list[str]:
        """Stop HLS sessions that have not been requested recently."""
        now = time.monotonic()
        stale = [
            camera_id
            for camera_id, session in self.sessions.items()
            if now - session.touched > idle_seconds
        ]
        for camera_id in stale:
            await self.stop_hls(camera_id)
        return stale

    async def stop_all(self) -> None:
        """Stop every running HLS session."""
        for camera_id in list(self.sessions):
            await self.stop_hls(camera_id)

    async def hls_error(self, session: HlsSession) -> str | None:
        """Return the ffmpeg message of an HLS session, or None when there is none.

        The output captured while ffmpeg was running comes first: a process that
        never wrote a segment is usually still alive (it simply waits for packets
        that never arrive), so insisting on a finished process would turn every
        such failure into the useless ``stream_failed``.
        """
        # Let the reader task finish first when the process is gone: it may
        # already have taken the text out of the pipe, and then only ``log_lines``
        # knows about it. A still running process keeps its reader.
        if session.process.returncode is not None and session.drain is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(session.drain), timeout=1)
        if session.log_lines:
            return self._clean_error("\n".join(session.log_lines))
        if session.process.returncode is None:
            return None
        stderr = b""
        if session.process.stderr is not None:
            with contextlib.suppress(Exception):
                stderr = await session.process.stderr.read()
        return self._clean_error(stderr)

    def session_report(self, session: HlsSession) -> dict[str, Any]:
        """Return a small diagnostic report about an HLS session.

        The JSON API sends it along when a preview cannot be started, so "ffmpeg
        is stuck" and "ffmpeg died" can be told apart without another request.
        """
        return {
            "running": session.process.returncode is None,
            "returncode": session.process.returncode,
            "segments": len(session.segments),
            "log": list(session.log_lines)[-3:],
        }