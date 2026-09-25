"""DVRIP (Xiongmai "Sofia") client of the RTSP Camera Manager integration.

The add-on publishes PTZ commands for DVRs that have no HTTP interface at all; they
speak this small binary protocol on TCP 34567. Every command logs in (the login of
the RTSP URL is reused), sends the PTZ payload and disconnects again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: magic, version, reserved(2), session, sequence, total, current, message id
HEADER_FORMAT = "<BBBBIIHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MSG_LOGIN_REQUEST = 1000
MSG_LOGIN_RESPONSE = 1001
MSG_PTZ_REQUEST = 1400
MSG_PTZ_RESPONSE = 1401
RESULT_OK = 100
DEFAULT_PORT = 34567

PTZ_TEMPLATE: dict[str, Any] = {
    "AUX": {"Number": 0, "Status": "On"},
    "MenuOpts": "Enter",
    "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0},
    "Pattern": "Start",
    "Preset": -1,
    "Step": 1,
    "Tour": 0,
}


def hash_password(username: str, password: str) -> str:
    """Return the double MD5 hash the DVRIP login expects."""
    first = hashlib.md5(password.encode("utf-8")).hexdigest().upper()
    return hashlib.md5(f"{username}{first}".encode()).hexdigest().upper()


def ptz_payload(short: dict[str, Any]) -> dict[str, Any]:
    """Expand the short payload published by the add-on."""
    parameter = dict(PTZ_TEMPLATE)
    for key in ("Step", "Preset", "Pattern", "Tour", "MenuOpts"):
        if key in short:
            parameter[key] = short[key]
    parameter["Channel"] = max(0, int(short.get("Channel") or 1) - 1)
    return {
        "Name": "OPPTZControl",
        "PTZControl": {"Command": str(short.get("Command") or ""), "Parameter": parameter},
    }


def _pack(message_id: int, payload: dict[str, Any], session: int = 0, sequence: int = 0) -> bytes:
    """Wrap a JSON payload into a DVRIP message."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack(
        HEADER_FORMAT, 0xFF, 0x00, 0x00, 0x00, session, sequence, len(body), 0, message_id
    )
    return header + body


async def _read(reader: asyncio.StreamReader) -> tuple[int, dict[str, Any]]:
    """Read one complete DVRIP message."""
    header = await reader.readexactly(HEADER_SIZE)
    _, _, _, _, _session, _, total, _current, message_id = struct.unpack(HEADER_FORMAT, header)
    body = await reader.readexactly(total) if total else b"{}"
    try:
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    return message_id, payload


async def async_send(
    host: str,
    port: int,
    username: str,
    password: str,
    short: dict[str, Any],
) -> tuple[bool, str | None]:
    """Log in and send one PTZ command. Returns success and an error message."""
    if not host:
        return False, "no_host"
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as err:
        return False, str(err)

    try:
        writer.write(
            _pack(
                MSG_LOGIN_REQUEST,
                {
                    "EncryptType": "MD5",
                    "LoginType": "DVRIP-Web",
                    "PassWord": hash_password(username, password),
                    "UserName": username,
                },
            )
        )
        await writer.drain()

        message_id, payload = await _read(reader)
        if message_id != MSG_LOGIN_RESPONSE:
            return False, f"unexpected_reply_{message_id}"
        if int(payload.get("Ret", 0)) != RESULT_OK:
            return False, f"login_failed_{payload.get('Ret')}"
        session = int(payload.get("SessionID") or 0)

        ptz = ptz_payload(short)
        ptz["SessionID"] = session
        writer.write(_pack(MSG_PTZ_REQUEST, ptz, session=session, sequence=1))
        await writer.drain()

        message_id, payload = await _read(reader)
        if message_id != MSG_PTZ_RESPONSE:
            return False, f"unexpected_reply_{message_id}"
        if int(payload.get("Ret", 0)) != RESULT_OK:
            return False, f"ptz_failed_{payload.get('Ret')}"
        return True, None
    except (OSError, asyncio.IncompleteReadError) as err:
        return False, str(err)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):  # pragma: no cover - closing can fail
            pass
