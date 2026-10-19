"""Xiongmai encoders (``AVEnc`` over DVRIP): which codec every stream of the camera sends.

The Xiongmai family of cameras - the ones with an RTSP URL like
``rtsp://host:554/user=admin&password=&channel=1&stream=0.sdp`` - often has no ONVIF media
service at all, so the encoder configurations of :mod:`app.onvif_media` cannot be read
there. The firmware still keeps them in a configuration document that its own protocol
hides behind the name ``AVEnc``: one block per stream, ``MainFormat`` for the high
resolution stream and ``ExtraFormat`` for the sub stream, each with a ``Video`` object
that names ``Compression`` next to ``Resolution``, ``FPS``, ``BitRate`` and ``GOP``.

That document is read and written over the same DVRIP connection the PTZ commands of the
family use (:mod:`app.dvrip`), so the codec of a stream is switched without a vendor app
and without the ONVIF service the firmware never shipped. The write sends the document
the camera itself handed out back with the codec word replaced - nothing the device knows
(and this module does not model) is dropped on the way - and reads the device again before
it reports success, because a device that acknowledges a write and keeps its codec is
exactly what a panel that trusts the acknowledgement shows as done.
"""

from __future__ import annotations

from typing import Any

from . import dvrip
from .onvif_media import (
    ENCODINGS,
    ERROR_AUTH,
    ERROR_BAD_ENCODING,
    ERROR_BAD_URL,
    ERROR_FAULT,
    ERROR_NO_MEDIA,
    ERROR_TIMEOUT,
    ERROR_TOKEN_REQUIRED,
    ERROR_TOKEN_UNKNOWN,
    ERROR_UNREACHABLE,
    EncoderReport,
)

#: The name of the configuration document and the blocks of the streams inside it, in the
#: order the panel lists them. ``main`` is the high resolution stream the first RTSP
#: channel serves, ``extra`` the sub stream of it.
CONFIG_NAME = "AVEnc"
STREAMS: tuple[tuple[str, str, str], ...] = (
    ("MainFormat", "main", "Main stream"),
    ("ExtraFormat", "extra", "Sub stream"),
)
DEFAULT_PORT = dvrip.DEFAULT_PORT
#: The backend name the panel shows and the write routes on.
BACKEND = "xm"


# ------------------------------------------------------------------ parsing
def _int(value: Any) -> int | None:
    """Return a whole number out of a configuration value, or None."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    """Return a number out of a configuration value, or None."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _resolution(value: Any) -> tuple[int | None, int | None]:
    """Split the ``1280*720`` of the document into width and height."""
    text = str(value or "").strip().lower()
    for separator in ("*", "x"):
        if separator in text:
            width, _, height = text.partition(separator)
            return _int(width), _int(height)
    return None, None


def document_of(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the ``AVEnc`` document of a config answer, or None when it carries none."""
    if not isinstance(answer, dict):
        return None
    document = answer.get(CONFIG_NAME)
    if isinstance(document, dict) and document:
        return document
    return None


def token_of(channel: int, stream: str) -> str:
    """Return the token the panel sees for one stream of a camera."""
    return f"xm:{channel}:{stream}"


def parse_token(token: Any) -> tuple[int, str] | None:
    """Return channel and stream of a token, or None when it is not one of ours."""
    parts = str(token or "").strip().split(":")
    if len(parts) != 3 or parts[0] != BACKEND:
        return None
    channel = _int(parts[1])
    stream = parts[2].strip().lower()
    if channel is None or stream not in {item[1] for item in STREAMS}:
        return None
    return channel, stream


def parse_encoders(
    document: dict[str, Any], channel: int, preferred: str = ""
) -> list[dict[str, Any]]:
    """Read the streams of an ``AVEnc`` document.

    The stream the camera of the panel serves first (``preferred``, taken from the
    ``stream=`` of its RTSP URL) is listed first, so the encoder the user has to change is
    the one on top. The ``element`` of every entry is the ``Video`` object of the document:
    the write sends the document back with that one field replaced.
    """
    configurations: list[dict[str, Any]] = []
    for key, stream, label in STREAMS:
        block = document.get(key)
        if not isinstance(block, dict):
            continue
        video = block.get("Video")
        if not isinstance(video, dict) or not video:
            continue
        width, height = _resolution(video.get("Resolution"))
        configurations.append(
            {
                "token": token_of(channel, stream),
                "name": label,
                "use_count": None,
                "encoding": str(video.get("Compression") or "").strip().upper(),
                "width": width,
                "height": height,
                "quality": _number(video.get("Quality")),
                "fps": _number(video.get("FPS")),
                "encoding_interval": None,
                "bitrate": _int(video.get("BitRate")),
                "gop": _int(video.get("GOP")),
                "profile": str(video.get("Profile") or "").strip(),
                "element": video,
                "stream": stream,
                "key": key,
            }
        )
    if preferred:
        configurations.sort(key=lambda item: item["stream"] != preferred)
    return configurations


# ------------------------------------------------------------------- errors
def _failure(detail: str) -> EncoderReport:
    """Translate a DVRIP failure into the error code of the encoder endpoints."""
    text = str(detail or "")
    lowered = text.lower()
    if lowered in ("timeout", "timed out"):
        return EncoderReport(error=ERROR_TIMEOUT, detail=text, backend=BACKEND)
    if lowered == "no_host":
        return EncoderReport(error=ERROR_BAD_URL, detail=text, backend=BACKEND)
    if lowered.startswith("login_failed"):
        return EncoderReport(error=ERROR_AUTH, detail=text, backend=BACKEND)
    if lowered.startswith("config_failed"):
        return EncoderReport(error=ERROR_FAULT, detail=text, backend=BACKEND)
    if "refused" in lowered or "unreachable" in lowered or "name or service" in lowered:
        return EncoderReport(error=ERROR_UNREACHABLE, detail=text, backend=BACKEND)
    return EncoderReport(error=ERROR_FAULT, detail=text, backend=BACKEND)


def _report(
    configurations: list[dict[str, Any]],
    host: str,
    port: int,
    *,
    changed: bool = False,
    detail: str = "",
) -> EncoderReport:
    """Return a successful report of the encoders of a camera."""
    return EncoderReport(
        ok=True,
        media_url=f"dvrip://{host}:{port}",
        configurations=configurations,
        profiles=[],
        changed=changed,
        detail=detail,
        backend=BACKEND,
    )


def _copy(value: Any) -> Any:
    """Return a deep copy of a configuration value (the document is nested)."""
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


# ------------------------------------------------------------------ reading
async def _read_document(
    host: str,
    port: int,
    username: str,
    password: str,
    channel: int,
    timeout: float,
) -> tuple[dict[str, Any] | None, EncoderReport | None]:
    """Read the ``AVEnc`` document of one channel, or the failure of the read."""
    answer, error = await dvrip.async_config(
        host,
        port,
        username,
        password,
        CONFIG_NAME,
        timeout=timeout,
        extra={"Channel": max(0, channel - 1)},
    )
    if error is not None:
        return None, _failure(error)
    document = document_of(answer)
    if document is None:
        keys = ", ".join(sorted(answer)) if isinstance(answer, dict) else ""
        return None, EncoderReport(
            error=ERROR_NO_MEDIA,
            detail=f"no_avenc ({keys})" if keys else "no_avenc",
            backend=BACKEND,
        )
    return document, None


async def async_read(
    host: str,
    *,
    port: int = DEFAULT_PORT,
    username: str = "",
    password: str = "",
    channel: int = 1,
    preferred: str = "",
    timeout: float = dvrip.DEFAULT_TIMEOUT,
) -> EncoderReport:
    """Read the encoders of a camera: the codec every stream of it sends."""
    if not host:
        return EncoderReport(error=ERROR_BAD_URL, detail="", backend=BACKEND)
    document, failure = await _read_document(
        host, port, username, password, channel, timeout
    )
    if failure is not None:
        return failure
    configurations = parse_encoders(document or {}, channel, preferred)
    if not configurations:
        return EncoderReport(error=ERROR_NO_MEDIA, detail="no_streams", backend=BACKEND)
    return _report(configurations, host, port)


# ------------------------------------------------------------------ writing
async def async_write(
    host: str,
    *,
    token: Any,
    encoding: Any,
    port: int = DEFAULT_PORT,
    username: str = "",
    password: str = "",
    channel: int = 1,
    timeout: float = dvrip.DEFAULT_TIMEOUT,
) -> EncoderReport:
    """Switch the codec of one stream and report what the camera sends afterwards."""
    if not host:
        return EncoderReport(error=ERROR_BAD_URL, detail="", backend=BACKEND)
    wanted = str(encoding or "").strip().upper()
    if wanted not in ENCODINGS:
        return EncoderReport(
            error=ERROR_BAD_ENCODING, detail=str(encoding or ""), backend=BACKEND
        )
    name = str(token or "").strip()
    if not name:
        return EncoderReport(error=ERROR_TOKEN_REQUIRED, backend=BACKEND)
    parsed = parse_token(name)
    if parsed is None:
        return EncoderReport(error=ERROR_TOKEN_UNKNOWN, detail=name, backend=BACKEND)
    token_channel, stream = parsed

    document, failure = await _read_document(
        host, port, username, password, channel, timeout
    )
    if failure is not None:
        return failure
    configurations = parse_encoders(document or {}, channel)
    wanted_token = token_of(token_channel, stream)
    current = next(
        (item for item in configurations if item["token"] == wanted_token), None
    )
    if current is None:
        return EncoderReport(error=ERROR_TOKEN_UNKNOWN, detail=name, backend=BACKEND)
    if current["encoding"] == wanted:
        # Nothing to do: the camera already sends the codec the panel asked for.
        return _report(configurations, host, port)

    # The document the camera handed out goes back with one word replaced.
    written = {key: _copy(value) for key, value in (document or {}).items()}
    block = written.get(current["key"])
    if not isinstance(block, dict):
        return EncoderReport(error=ERROR_FAULT, detail="no_stream_block", backend=BACKEND)
    video = block.get("Video")
    if not isinstance(video, dict):
        return EncoderReport(error=ERROR_FAULT, detail="no_video_block", backend=BACKEND)
    video["Compression"] = wanted

    _answer, error = await dvrip.async_config(
        host,
        port,
        username,
        password,
        CONFIG_NAME,
        document=written,
        timeout=timeout,
        extra={"Channel": max(0, channel - 1)},
    )
    if error is not None:
        return _failure(error)

    # The write is only real when the device reports it: read the camera again.
    after_document, after_failure = await _read_document(
        host, port, username, password, channel, timeout
    )
    if after_failure is not None:
        # The camera accepted the write but would not answer the read: report what the
        # panel asked for, and say that it could not be confirmed.
        return _report(configurations, host, port, changed=True, detail="reread_failed")
    after = parse_encoders(after_document or {}, channel, preferred=stream)
    confirmed = next(
        (item for item in after if item["token"] == wanted_token), None
    )
    if confirmed is None:
        return _report(configurations, host, port, changed=True, detail="reread_failed")
    if confirmed["encoding"] != wanted:
        return EncoderReport(
            error=ERROR_FAULT,
            detail=f"codec_kept_{confirmed['encoding'] or 'unknown'}",
            backend=BACKEND,
        )
    return _report(after, host, port, changed=True)
