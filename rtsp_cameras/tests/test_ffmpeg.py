"""Tests for the ffmpeg based stream helpers."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.ffmpeg import (
    FFmpegService,
    FFmpegUnavailable,
    StreamFailed,
    extract_jpeg_frames,
    fraction_to_float,
)
from tests.fake_ffmpeg import JPEG

STREAM_URL = "rtsp://user:secret@192.168.1.10:554/stream1"


def run(coro):
    """Run a coroutine in a fresh event loop."""
    return asyncio.run(coro)


def wait_for_file(path: Path, timeout: float = 10.0) -> bool:
    """Wait until a file exists (the fake ffmpeg writes it asynchronously)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.05)
    return path.is_file()


async def async_wait_for_file(path: Path, timeout: float = 10.0) -> bool:
    """Async flavour of wait_for_file that keeps the event loop responsive."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        await asyncio.sleep(0.05)
    return path.is_file()


@pytest.fixture()
def service(settings: Settings, fake_tools: None) -> FFmpegService:
    """A service wired to the fake binaries."""
    return FFmpegService(settings)


def read_arguments(path: Path) -> list[str]:
    """Return the command line a fake binary recorded."""
    return json.loads(path.read_text(encoding="utf-8"))


def assert_input_url(arguments: list[str], url: str) -> None:
    """The input URL must reach ffmpeg, otherwise it has nothing to read."""
    assert "-i" in arguments, "ffmpeg was called without an input URL"
    assert arguments[arguments.index("-i") + 1] == url
    assert "-nostdin" not in arguments


def test_extract_jpeg_frames_handles_partial_data() -> None:
    buffer = bytearray(JPEG + JPEG[:5])
    assert list(extract_jpeg_frames(buffer)) == [JPEG]
    assert bytes(buffer) == JPEG[:5]

    buffer = bytearray(b"\x00\x00" + JPEG + JPEG)
    assert list(extract_jpeg_frames(buffer)) == [JPEG, JPEG]
    assert len(buffer) == 0

    buffer = bytearray(b"garbage")
    assert list(extract_jpeg_frames(buffer)) == []
    assert len(buffer) == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("25/1", 25.0),
        ("30000/1001", 29.97),
        ("0/0", None),
        ("", None),
        ("12.5", 12.5),
        ("N/A", None),
    ],
)
def test_fraction_to_float(value: str, expected: float | None) -> None:
    assert fraction_to_float(value) == expected


def test_service_reports_binaries(service: FFmpegService) -> None:
    assert service.available is True


def test_probe_returns_stream_details(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "ok")
    args_file = tmp_path / "probe-args.json"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))

    result = run(service.probe(STREAM_URL))

    assert result.ok is True
    assert result.details["resolution"] == "1920x1080"
    assert result.details["codec"] == "h264"
    assert result.details["fps"] == 25.0
    assert result.details["bit_rate_kbps"] == 2048
    assert result.details["transport"] == "tcp"

    recorded = args_file.read_text(encoding="utf-8")
    assert "-rtsp_transport" in recorded
    assert "-rw_timeout" in recorded

    arguments = json.loads(recorded)
    assert STREAM_URL in arguments
    # ffmpeg 8 rejects -nostdin in front of other options; stdin is closed instead.
    assert "-nostdin" not in arguments


def test_processes_run_with_a_closed_stdin(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    original = asyncio.create_subprocess_exec

    async def spy(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr("app.ffmpeg.asyncio.create_subprocess_exec", spy)
    monkeypatch.setenv("FAKE_PROBE_MODE", "ok")

    assert run(service.probe(STREAM_URL)).ok is True
    assert captured["stdin"] == asyncio.subprocess.DEVNULL


def test_probe_masks_credentials(service: FFmpegService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "fail")

    result = run(service.probe(STREAM_URL))

    assert result.ok is False
    assert result.code == "failed"
    assert "401" in result.error
    assert "secret" not in result.error
    assert "***:***@camera.local" in result.error


def test_probe_handles_missing_video(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "no_video")

    result = run(service.probe(STREAM_URL))

    assert result.ok is False
    assert result.code == "no_video"


def test_probe_handles_broken_output(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "invalid")

    result = run(service.probe(STREAM_URL))

    assert result.ok is False
    assert result.code == "no_video"


def test_probe_times_out(service: FFmpegService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_PROBE_MODE", "sleep")
    monkeypatch.setenv("FAKE_SLEEP", "20")

    started = time.monotonic()
    result = run(service.probe(STREAM_URL, timeout=1))

    assert result.ok is False
    assert result.code == "timeout"
    assert time.monotonic() - started < 15


def test_probe_without_ffprobe(settings: Settings, no_tools: None) -> None:
    service = FFmpegService(settings)

    result = run(service.probe(STREAM_URL))

    assert service.available is False
    assert result.ok is False
    assert result.code == "ffprobe_missing"


def test_snapshot_returns_a_jpeg(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "snapshot")
    args_file = tmp_path / "snapshot-args.json"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))

    assert run(service.snapshot(STREAM_URL)) == JPEG

    arguments = read_arguments(args_file)
    assert_input_url(arguments, STREAM_URL)
    assert arguments[-1] == "pipe:1"
    assert "-frames:v" in arguments


def test_snapshot_returns_none_on_failure(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")

    assert run(service.snapshot(STREAM_URL)) is None


def test_snapshot_without_ffmpeg(settings: Settings, no_tools: None) -> None:
    service = FFmpegService(settings)

    assert run(service.snapshot(STREAM_URL)) is None


def test_mjpeg_stream_yields_frames(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "mjpeg")
    monkeypatch.setenv("FAKE_FRAMES", "3")
    args_file = tmp_path / "mjpeg-args.json"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))

    async def collect() -> list[bytes]:
        frames: list[bytes] = []
        async for frame in service.mjpeg_stream(STREAM_URL):
            frames.append(frame)
        return frames

    assert run(collect()) == [JPEG, JPEG, JPEG]
    assert_input_url(read_arguments(args_file), STREAM_URL)


def test_mjpeg_stream_raises_when_no_frame_arrives(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "fail")

    async def collect() -> None:
        async for _frame in service.mjpeg_stream(STREAM_URL):
            pass

    with pytest.raises(StreamFailed) as error:
        run(collect())
    assert "Connection refused" in str(error.value)


def test_mjpeg_stream_without_ffmpeg(settings: Settings, no_tools: None) -> None:
    service = FFmpegService(settings)

    async def collect() -> None:
        async for _frame in service.mjpeg_stream(STREAM_URL):
            pass

    with pytest.raises(FFmpegUnavailable):
        run(collect())


def test_hls_session_lifecycle(service: FFmpegService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hls")

    async def scenario() -> None:
        session = await service.start_hls("front-door", STREAM_URL, codec="h264")
        assert await async_wait_for_file(session.playlist)
        assert service.get_session("front-door") is session

        session.touched = time.monotonic() - 120
        assert await service.reap_hls(idle_seconds=45) == ["front-door"]
        assert service.get_session("front-door") is None

    run(scenario())


def test_hls_transcodes_for_non_h264(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hls")
    args_file = tmp_path / "ffmpeg-args.json"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))

    async def scenario() -> None:
        await service.start_hls("gate", STREAM_URL, codec="hevc")
        assert await async_wait_for_file(args_file)
        await service.stop_all()

    run(scenario())
    arguments = read_arguments(args_file)
    assert_input_url(arguments, STREAM_URL)
    assert "libx264" in arguments
    assert "scale=-2:'min(1080,ih)'" in arguments


def test_hls_copies_h264_streams(
    service: FFmpegService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_FFMPEG_MODE", "hls")
    args_file = tmp_path / "ffmpeg-args.json"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(args_file))

    async def scenario() -> None:
        await service.start_hls("gate", STREAM_URL, codec="h264")
        assert await async_wait_for_file(args_file)
        await service.stop_all()

    run(scenario())
    arguments = read_arguments(args_file)
    assert_input_url(arguments, STREAM_URL)
    assert "copy" in arguments
    assert "libx264" not in arguments


def test_hls_without_ffmpeg(settings: Settings, no_tools: None) -> None:
    service = FFmpegService(settings)

    with pytest.raises(FFmpegUnavailable):
        run(service.start_hls("gate", STREAM_URL))

