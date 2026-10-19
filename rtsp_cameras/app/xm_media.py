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

import asyncio
from collections.abc import Mapping
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

#: The name of the configuration document and the blocks of the streams inside it: the
#: older layout names a block per stream, the newer one keeps a list under ``Encode`` with
#: one entry for every stream. ``main`` is the high resolution stream the first RTSP
#: channel serves, ``extra`` the sub stream of it, ``third`` the stream some devices add.
CONFIG_NAME = "AVEnc"
STREAMS: tuple[tuple[str, str, str], ...] = (
    ("MainFormat", "main", "Main stream"),
    ("ExtraFormat", "extra", "Sub stream"),
)
ENCODE_KEY = "Encode"
STREAM_NAMES = ("main", "extra", "third")
STREAM_LABELS = ("Main stream", "Sub stream", "Third stream")


def _stream_label(stream: str) -> str:
    """Return the name the panel shows for a stream of the document."""
    if stream in STREAM_NAMES:
        return STREAM_LABELS[STREAM_NAMES.index(stream)]
    return f"Stream {stream}"
DEFAULT_PORT = dvrip.DEFAULT_PORT
#: The backend name the panel shows and the write routes on.
BACKEND = "xm"
#: How much of a document shape an error detail may carry (the panel shows one line).
MAX_DESCRIPTION = 400


# ------------------------------------------------------------------ parsing
def _int(value: Any) -> int | None:
    """Return a whole number out of a configuration value, or None."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


#: The names a firmware may use for the codec word of a stream, most common first.
CODEC_KEYS = ("Compression", "Compress", "Codec", "Encoding")
#: The words the family uses for the two codecs, mapped to what the panel offers. The
#: firmware of the test set writes ``H.265``, others write ``H265``, ``HEVC`` or ``AVC``.
CODEC_ALIASES = {
    "H264": "H264",
    "H.264": "H264",
    "AVC": "H264",
    "H265": "H265",
    "H.265": "H265",
    "HEVC": "H265",
}


def normalize_encoding(value: Any) -> str:
    """Return ``H264``, ``H265`` or an empty string for a codec word of a camera."""
    text = str(value or "").strip().upper().replace(" ", "")
    return CODEC_ALIASES.get(text, "")


def _value(fields: Mapping[str, Any], name: str) -> Any:
    """Return a field of the document, whatever case the firmware writes its keys in."""
    if name in fields:
        return fields[name]
    lowered = name.lower()
    for key, value in fields.items():
        if str(key).lower() == lowered:
            return value
    return None


def _codec_of(fields: Mapping[str, Any]) -> str:
    """Return the codec word of a video object, whatever the firmware calls the field."""
    for name in CODEC_KEYS:
        value = _value(fields, name)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _holds_video(fields: Mapping[str, Any]) -> bool:
    """Return True when an object carries the video fields of a stream."""
    return bool(_codec_of(fields)) or bool(_value(fields, "Resolution"))


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


def describe_document(document: Any) -> str:
    """Return a one line shape of a configuration document (for an error detail).

    The block names of the streams are the one thing this module cannot know - a firmware
    may call them differently - so an answer that carries no stream is shown as the keys
    and blocks it did bring, nested a few levels deep. That is what a report of the panel
    and the next version of this module need, without a packet capture.
    """
    if not isinstance(document, Mapping):
        return ""
    return _shape(document, 2)[:MAX_DESCRIPTION]


def focus_document(document: Mapping[str, Any]) -> str:
    """Return the shape of the part of a document that was expected to carry the streams.

    A document that keeps its streams in a list is shown at the first entry of it and a few
    levels deeper, because that is where the video fields of the camera live.
    """
    entries = document.get(ENCODE_KEY)
    if isinstance(entries, list) and entries:
        return f"{ENCODE_KEY}[{len(entries)}]{_shape(entries[0], 3)}"[:MAX_DESCRIPTION]
    return describe_document(document)


def _shape(value: Any, depth: int) -> str:
    """Return the shape of a document value: its keys, nested a few levels deep."""
    if isinstance(value, Mapping):
        parts = []
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))[:8]:
            nested = _shape(item, depth - 1) if depth > 0 else ""
            parts.append(f"{key}{nested}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        head = _shape(value[0], depth - 1) if depth > 0 else ""
        return f"[{len(value)}]{head}"
    return ""


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


def stream_blocks(
    document: Mapping[str, Any], channel: int = 1
) -> list[tuple[str, tuple[str | int, ...], Mapping[str, Any]]]:
    """Return stream, path and video fields of every stream the document carries.

    Three layouts are known and all are read: a block per stream directly in the document
    (``MainFormat`` / ``ExtraFormat``), a list under ``Encode`` whose entries *are* the
    streams, and a list under ``Encode`` whose entries are the *channels* - the firmware of
    the test set keeps ``MainFormat`` / ``ExtraFormat`` inside every entry of it, so the
    entry of the configured channel is the one that is read. The path points at the object
    that holds ``Compression``, so the write puts the codec word where the device keeps it.
    """
    blocks = _block_streams(document, channel)
    if blocks:
        return blocks

    entries = document.get(ENCODE_KEY)
    if not isinstance(entries, list) or not entries:
        return []

    # A list of channels: every entry carries the blocks of its own streams.
    carrying = [
        position
        for position, entry in enumerate(entries)
        if isinstance(entry, Mapping)
        and any(isinstance(entry.get(key), Mapping) for key, _stream, _label in STREAMS)
    ]
    if carrying:
        wanted = max(0, channel - 1)
        position = wanted if wanted in carrying else carrying[0]
        blocks = _block_streams(entries[position], channel, prefix=(ENCODE_KEY, position))
        if blocks:
            return blocks

    # A list of groups: the blocks of a group are lists over the channels of the device.
    for group, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        blocks = _block_streams(entry, channel, prefix=(ENCODE_KEY, group))
        if blocks:
            return blocks

    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not entry:
            continue
        holder = _video_holder(entry)
        if holder is None:
            continue
        stream = (
            STREAM_NAMES[position] if position < len(STREAM_NAMES) else str(position)
        )
        blocks.append((stream, (ENCODE_KEY, position, *holder[0]), holder[1]))
    return blocks


def _block_streams(
    document: Mapping[str, Any], channel: int = 1, prefix: tuple[str | int, ...] = ()
) -> list[tuple[str, tuple[str | int, ...], Mapping[str, Any]]]:
    """Return the streams of a block layout (``MainFormat`` / ``ExtraFormat``).

    A block is either the object of the stream or a *list* over the channels of the device
    - the firmware of the test set keeps three entries in every block - so the entry of the
    configured channel is taken from it.
    """
    blocks: list[tuple[str, tuple[str | int, ...], Mapping[str, Any]]] = []
    for key, stream, _label in STREAMS:
        block = document.get(key)
        path: tuple[str | int, ...] = (*prefix, key)
        if isinstance(block, list):
            index = _list_index(block, channel)
            if index is None:
                continue
            block = block[index]
            path = (*path, index)
        if not isinstance(block, Mapping):
            continue
        holder = _video_holder(block)
        if holder is None:
            continue
        blocks.append((stream, (*path, *holder[0]), holder[1]))
    return blocks


def _list_index(entries: list[Any], channel: int) -> int | None:
    """Return the entry of a per channel list, or the first one when the numbering differs."""
    if not entries:
        return None
    wanted = max(0, channel - 1)
    return wanted if wanted < len(entries) else 0


def _video_holder(
    entry: Mapping[str, Any], depth: int = 2
) -> tuple[tuple[str, ...], Mapping[str, Any]] | None:
    """Return the path and the object of an encoder entry that holds the video fields.

    The entries of the list layout keep them either directly or inside a block of their
    own (``Video``, or the ``MainFormat`` of a channel), so the entry is searched a few
    levels deep before it is given up.
    """
    if isinstance(entry.get("Video"), Mapping) and _holds_video(entry["Video"]):
        return ("Video",), entry["Video"]
    if _holds_video(entry):
        return (), entry
    if depth <= 0:
        return None
    for key, value in entry.items():
        if isinstance(value, Mapping):
            inner = _video_holder(value, depth - 1)
            if inner is not None:
                return (str(key), *inner[0]), inner[1]
    return None


def _set_codec(fields: dict[str, Any], encoding: str) -> None:
    """Write the codec word, keeping the spelling of the key the firmware uses."""
    for name in CODEC_KEYS:
        for key in fields:
            if str(key).lower() == name.lower():
                fields[key] = encoding
                return
    fields["Compression"] = encoding


def codec_spelling(current: str, wanted: str) -> str:
    """Return the codec word in the spelling the camera itself used (``H.265`` / ``H265``)."""
    raw = str(current or "").strip().upper()
    if "." in raw:
        return "H.264" if wanted == "H264" else "H.265"
    return wanted


#: The request types a write of the family may hide behind: the reference client uses one
#: number for a read and for a write, other clients write on a number of their own.
WRITE_MESSAGE_IDS = (dvrip.MSG_CONFIG_REQUEST, 1040, 1044)
#: How long the camera of a change is given to restart the encoder before the read back.
WRITE_SETTLE_SECONDS = 0.4


def write_variants(raw_encoding: str, wanted: str) -> list[tuple[int, str]]:
    """Return the (request type, codec word) pairs a camera of the family may accept.

    The family is not consistent about either one: a firmware answers the same request
    type for a read and for a write while another writes on a type of its own, and the
    codec word is spelled ``H.265`` by one device and ``H265`` by the next. Every pair is
    tried - and the device is read back after each - so the panel names the pair that
    really changed the camera instead of guessing it.
    """
    words = [codec_spelling(raw_encoding, wanted)]
    if wanted not in words:
        words.append(wanted)
    return [
        (message_id, word) for message_id in WRITE_MESSAGE_IDS for word in words
    ]


def _follow(document: Any, path: tuple[str | int, ...]) -> dict[str, Any] | None:
    """Return the object a path points at, or None when the document has no such object."""
    node: Any = document
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or step >= len(node):
                return None
            node = node[step]
        else:
            if not isinstance(node, Mapping):
                return None
            node = node.get(step)
    return node if isinstance(node, dict) else None


def parse_encoders(
    document: dict[str, Any], channel: int, preferred: str = ""
) -> list[dict[str, Any]]:
    """Read the streams of an ``AVEnc`` document.

    The stream the camera of the panel serves first (``preferred``, taken from the
    ``stream=`` of its RTSP URL) is listed first, so the encoder the user has to change is
    the one on top. The ``element`` of every entry is the object of the document that holds
    the video fields: the write sends the document back with that one field replaced.
    """
    configurations: list[dict[str, Any]] = []
    for stream, path, video in stream_blocks(document, channel):
        width, height = _resolution(_value(video, "Resolution"))
        raw_encoding = _codec_of(video)
        configurations.append(
            {
                "token": token_of(channel, stream),
                "name": _stream_label(stream),
                "use_count": None,
                "encoding": normalize_encoding(raw_encoding) or raw_encoding,
                "raw_encoding": raw_encoding,
                "width": width,
                "height": height,
                "quality": _number(_value(video, "Quality")),
                "fps": _number(_value(video, "FPS")),
                "encoding_interval": None,
                "bitrate": _int(_value(video, "BitRate")),
                "gop": _int(_value(video, "GOP")),
                "profile": str(_value(video, "Profile") or "").strip(),
                "element": video,
                "stream": stream,
                "path": path,
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
    shape: str = "",
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
        shape=shape,
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
        return EncoderReport(
            error=ERROR_NO_MEDIA,
            detail=f"no_streams: {focus_document(document or {})}".rstrip(": "),
            backend=BACKEND,
        )
    if not any(item["encoding"] for item in configurations):
        # The device answered streams, but none of them names a codec this module knows:
        # the shape of the document is what the panel reports and what a next version needs.
        return _report(
            configurations, host, port, shape=focus_document(document or {})
        )
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

    # The document the camera handed out goes back with one word replaced - and the pair
    # of request type and codec word a camera accepts is found by trying them, because a
    # device that answers "done" and keeps its codec is what the family does too.
    attempts: list[str] = []
    for message_id, word in write_variants(current.get("raw_encoding", ""), wanted):
        written = {key: _copy(value) for key, value in (document or {}).items()}
        video = _follow(written, current["path"])
        if video is None:
            return EncoderReport(
                error=ERROR_FAULT, detail="no_video_block", backend=BACKEND
            )
        _set_codec(video, word)

        _answer, error = await dvrip.async_config(
            host,
            port,
            username,
            password,
            CONFIG_NAME,
            document=written,
            timeout=timeout,
            extra={"Channel": max(0, channel - 1)},
            message_id=message_id,
        )
        if error is not None:
            attempts.append(f"{message_id}:{word}={error}")
            continue

        after_document, after_failure = await _read_after_write(
            host, port, username, password, channel, timeout
        )
        if after_failure is not None:
            # The camera accepted the write but would not answer the read: report what the
            # panel asked for, and say that it could not be confirmed.
            return _report(
                configurations, host, port, changed=True, detail="reread_failed"
            )
        after = parse_encoders(after_document or {}, channel, preferred=stream)
        confirmed = next((item for item in after if item["token"] == wanted_token), None)
        if confirmed is not None and confirmed["encoding"] == wanted:
            return _report(
                after, host, port, changed=True, detail=f"write={message_id}:{word}"
            )
        kept = confirmed["encoding"] if confirmed is not None else "unknown"
        attempts.append(f"{message_id}:{word}=kept_{kept or 'unknown'}")

    return EncoderReport(
        error=ERROR_FAULT,
        detail=("codec_kept_" + ";".join(attempts))[:MAX_DESCRIPTION],
        backend=BACKEND,
    )


async def _read_after_write(
    host: str,
    port: int,
    username: str,
    password: str,
    channel: int,
    timeout: float,
) -> tuple[dict[str, Any] | None, EncoderReport | None]:
    """Read the encoders again, giving the camera a moment to restart that encoder."""
    failure: EncoderReport | None = None
    for _attempt in range(2):
        await asyncio.sleep(WRITE_SETTLE_SECONDS)
        document, failure = await _read_document(
            host, port, username, password, channel, timeout
        )
        if failure is None:
            return document, None
    return None, failure

