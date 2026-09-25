"""DVRIP (Xiongmai "Sofia") client for PTZ commands on TCP 34567.

Devices of this family - recognisable by an RTSP URL like
``rtsp://host:554/user=admin&password=&channel=1&stream=0.sdp`` and an open port
34567 - have no HTTP interface at all, so PTZ is controlled with a small binary
protocol: a 20 byte header in front of a JSON payload, a login with a hashed
password and then the PTZ command.

Every command logs in, sends the command and disconnects again, which keeps the
add-on stateless (the devices allow repeated logins). The PTZ payload uses the short
form the add-on publishes (``Command``, ``Step``, ``Preset``, ``Channel``) and is
expanded into the structure the devices expect.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from typing import Any

_LOGGER = logging.getLogger(__name__)

# magic, version, reserved(2), session, sequence, total, current, message id
HEADER_FORMAT = "<BBBBIIHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
MSG_LOGIN_REQUEST = 1000
MSG_LOGIN_RESPONSE = 1001
MSG_PTZ_REQUEST = 1400
MSG_PTZ_RESPONSE = 1401
LOGIN_OK = 100
DEFAULT_TIMEOUT = 6.0

#: The devices expect this structure with the PTZ command inside.
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


def pack(
    message_id: int, payload: dict[str, Any], session: int = 0, sequence: int = 0
) -> bytes:
    """Wrap a JSON payload into a DVRIP message."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack(
        HEADER_FORMAT,
        0xFF,
        0x00,
        0x00,
        0x00,
        session,
        sequence,
        len(body),
        0,
        message_id,
    )
    return header + body


def unpack(data: bytes) -> tuple[int, int, dict[str, Any]]:
    """Return message id, session id and payload of a DVRIP message."""
    if len(data) < HEADER_SIZE:
        return 0, 0, {}
    _, _, _, _, session, _, _total, _current, message_id = struct.unpack(
        HEADER_FORMAT, data[:HEADER_SIZE]
    )
    try:
        payload = json.loads(data[HEADER_SIZE:].decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    return message_id, session, payload


def ptz_payload(short: dict[str, Any], channel: int = 1) -> dict[str, Any]:
    """Expand the short form published by the add-on into a DVRIP PTZ request."""
    parameter = dict(PTZ_TEMPLATE)
    for key in ("Step", "Preset", "Pattern", "Tour", "MenuOpts"):
        if key in short:
            parameter[key] = short[key]
    parameter["Channel"] = int(short.get("Channel") or channel) - 1
    command = str(short.get("Command") or "")
    return {
        "Name": "OPPTZControl",
        "PTZControl": {"Command": command, "Parameter": parameter},
    }



async def async_send(
    host: str,
    port: int,
    username: str,
    password: str,
    short: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str | None]:
    """Log in, send one PTZ command and return success plus an error message."""
    if not host:
        return False, "no_host"
    try:
        return await asyncio.wait_for(
            _async_session(host, port, username, password, short), timeout=timeout
        )
    except TimeoutError:
        return False, "timeout"
    except OSError as err:
        return False, str(err)


async def _async_session(
    host: str,
    port: int,
    username: str,
    password: str,
    short: dict[str, Any],
) -> tuple[bool, str | None]:
    """Run the login and the PTZ command on one connection."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(
            pack(
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

        message_id, session, payload = await _async_read(reader)
        if message_id != MSG_LOGIN_RESPONSE:
            return False, f"unexpected_reply_{message_id}"
        if int(payload.get("Ret", 0)) != LOGIN_OK:
            return False, f"login_failed_{payload.get('Ret')}"
        session = int(payload.get("SessionID") or session)

        ptz = ptz_payload(short)
        ptz["SessionID"] = session
        writer.write(pack(MSG_PTZ_REQUEST, ptz, session=session, sequence=1))
        await writer.drain()

        message_id, _session, payload = await _async_read(reader)
        if message_id != MSG_PTZ_RESPONSE:
            return False, f"unexpected_reply_{message_id}"
        if int(payload.get("Ret", 0)) != LOGIN_OK:
            return False, f"ptz_failed_{payload.get('Ret')}"
        return True, None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):  # pragma: no cover - closing can fail
            pass


async def _async_read(reader: asyncio.StreamReader) -> tuple[int, int, dict[str, Any]]:
    """Read one complete DVRIP message."""
    header = await reader.readexactly(HEADER_SIZE)
    _, _, _, _, session, _, total, _current, message_id = struct.unpack(HEADER_FORMAT, header)
    body = await reader.readexactly(total) if total else b"{}"
    return message_id, session, json.loads(body.decode("utf-8", "replace") or "{}")
