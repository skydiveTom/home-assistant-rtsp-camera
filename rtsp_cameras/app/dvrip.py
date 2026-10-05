"""DVRIP (Xiongmai "Sofia") client for PTZ commands on TCP 34567.

Devices of this family - recognisable by an RTSP URL like
``rtsp://host:554/user=admin&password=&channel=1&stream=0.sdp`` and an open port
34567 - have no HTTP interface at all, so PTZ is controlled with a small binary
protocol: a 20 byte header in front of a JSON payload, a login with a hashed
password and then the PTZ command.

The framing and the login follow the vendor SDK (``libFunSDK.so`` of the Android
FunSDK, read out of ``DVRIP_MSG_HEAD_T``, ``MNetSDK::CProtocolNetIP::InitMsg`` and
``XMMD5Encrypt``):

* the header is 20 bytes long, starts with ``0xFF`` and the version ``0x01``,
  carries the session and the sequence, the message type as a 16 bit value at
  offset 14 and the length of the JSON payload as a 32 bit value at offset 16,
* the payload is plain JSON, without padding,
* ``PassWord`` is neither a plain nor a double MD5: the MD5 digest is read in
  pairs, the two bytes of a pair are added, the sum is taken modulo 62 and written
  as a character of ``0-9A-Za-z`` - eight characters in total, the user name is not
  part of it.

Every command logs in, sends the command and disconnects again, which keeps the
add-on stateless (the devices allow repeated logins). The PTZ payload uses the short
form the add-on publishes (``Command``, ``Step``, ``Preset``, ``Channel``) and is
expanded into the structure the devices expect - the message name in front and the
same name again as the key of the nested object, which is how the devices look the
structure up. A hand written payload that already carries ``Name`` is sent as it is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: magic, version, reserved(2), session, sequence, packet flags(2), message type,
#: payload length - the header the protocol uses, 20 bytes long.
HEADER_FORMAT = "<BB2xIIBBHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
HEADER_MAGIC = 0xFF
HEADER_VERSION = 0x01
MSG_LOGIN_REQUEST = 1000
MSG_LOGIN_RESPONSE = 1001
MSG_PTZ_REQUEST = 1400
MSG_PTZ_RESPONSE = 1401
LOGIN_OK = 100
DEFAULT_PORT = 34567
DEFAULT_TIMEOUT = 6.0
NO_RESULT = -1
MAX_PAYLOAD = 32768

#: The login types of the family. The first one is what the web client of the vendor
#: sends, the others are what the mobile clients use.
LOGIN_TYPES = ("DVRIP-Web", "DVRIP-Mobile", "DVRIP-Xm030")
#: A device that answers one of these codes does not reject the password but the login
#: type (``102`` unsupported version, ``103`` request not permitted), so the next one
#: is worth a try.
LOGIN_RETRY_CODES = (102, 103)
#: ``XMMD5Encrypt`` writes the password hash with this alphabet: digits, upper case
#: letters, lower case letters.
HASH_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: The devices expect this structure with the PTZ command inside.
PTZ_TEMPLATE: dict[str, Any] = {
    "AUX": {"Number": 0, "Status": "On"},
    "MenuOpts": "Enter",
    "Pattern": "Start",
    "Preset": -1,
    "Step": 1,
    "Tour": 0,
}

#: Name of the PTZ request. A device looks the structure up **by this name**, so the
#: JSON carries it twice: as the value of ``Name`` and as the key of the nested object
#: - ``{"Name": "OPPTZControl", "OPPTZControl": {...}}``, the same way the ``OPMonitor``
#: request the SDK ships is built. ``MNetSDK::CProtocolNetIP::NewPTZControlPTL``
#: (0xF1918C of ``libFunSDK.so``) loads the string once for each place (the
#: ``adrp``/``add`` pair at 0xF197E8 and the one at 0xF1981C both point at 0x58D3A9),
#: and the Java layer of that SDK keeps both as one constant,
#: ``OPPTZControlBean.OPPTZCONTROL_JSONNAME = "OPPTZControl"``. A payload that nests
#: the command under any other key - ``PTZControl``, which this file used to send - is
#: answered with ``Ret: 100`` and then ignored: the device has no member of that name.
PTZ_MESSAGE = "OPPTZControl"


def hash_password(password: str) -> str:
    """Return the eight character hash the DVRIP login expects as ``PassWord``.

    ``XMMD5Encrypt`` of the FunSDK reads the MD5 digest in pairs, adds the two bytes
    of a pair, takes the sum modulo 62 and writes it as ``0-9A-Za-z``. The user name
    is not part of the hash.
    """
    digest = hashlib.md5(password.encode("utf-8")).digest()
    pairs = zip(digest[::2], digest[1::2], strict=True)
    alphabet = HASH_ALPHABET
    return "".join(alphabet[(first + second) % len(alphabet)] for first, second in pairs)


def login_payload(username: str, password: str, login_type: str) -> dict[str, Any]:
    """Return the login request of the protocol."""
    return {
        "EncryptType": "MD5",
        "LoginType": login_type,
        "PassWord": hash_password(password),
        "UserName": username,
    }


def ptz_payload(short: dict[str, Any], channel: int = 1) -> dict[str, Any]:
    """Expand the short form published by the add-on into a DVRIP PTZ request.

    A payload that carries ``Name`` is a hand written one - the shape of a request
    captured from the vendor app - and is passed through unchanged, so a device whose
    commands the built in profile does not know can still be driven.
    """
    if "Name" in short:
        return {key: value for key, value in short.items() if key != "SessionID"}
    parameter = dict(PTZ_TEMPLATE)
    for key in ("Step", "Preset", "Pattern", "Tour", "MenuOpts"):
        if key in short:
            parameter[key] = short[key]
    parameter["Channel"] = max(0, int(short.get("Channel") or channel) - 1)
    command = str(short.get("Command") or "")
    if "Tour" in command:
        parameter["Tour"] = 1
    return {
        "Name": PTZ_MESSAGE,
        PTZ_MESSAGE: {"Command": command, "Parameter": parameter},
    }


def pack(message_id: int, payload: dict[str, Any], session: int = 0, sequence: int = 0) -> bytes:
    """Wrap a JSON payload into a DVRIP message."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack(
        HEADER_FORMAT,
        HEADER_MAGIC,
        HEADER_VERSION,
        session,
        sequence,
        0,
        0,
        message_id,
        len(body),
    )
    return header + body


def unpack(data: bytes) -> tuple[int, int, dict[str, Any]]:
    """Return message id, session id and payload of a DVRIP message."""
    if len(data) < HEADER_SIZE:
        return 0, 0, {}
    header = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    message_id, session, length = header[6], header[2], header[7]
    return message_id, session, parse_payload(data[HEADER_SIZE : HEADER_SIZE + length])


def parse_payload(body: bytes) -> dict[str, Any]:
    """Parse the JSON body of a message; devices pad it with NUL bytes and line ends."""
    text = body.decode("utf-8", "replace").strip("\x00\r\n\t ")
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


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
    except (EOFError, OSError, ValueError) as err:
        return False, str(err) or err.__class__.__name__


async def _async_session(
    host: str, port: int, username: str, password: str, short: dict[str, Any]
) -> tuple[bool, str | None]:
    """Try the login types of the family until one of them is accepted."""
    result: tuple[bool, str | None] = (False, "login_failed")
    for login_type in LOGIN_TYPES:
        ok, error, retry = await _async_try(host, port, username, password, short, login_type)
        if not retry:
            return ok, error
        result = (ok, error)
    return result


async def _async_try(
    host: str,
    port: int,
    username: str,
    password: str,
    short: dict[str, Any],
    login_type: str,
) -> tuple[bool, str | None, bool]:
    """Log in with one login type and send the PTZ command on the same connection.

    The third value tells whether another login type is worth a try: a device that
    only dislikes the login type answers ``102`` or ``103`` and stays logged out.
    Every attempt gets a fresh connection, because a device that did log in refuses
    a second login on the same one.
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(pack(MSG_LOGIN_REQUEST, login_payload(username, password, login_type)))
        await writer.drain()

        message_id, session, payload = await _async_read(reader)
        if message_id != MSG_LOGIN_RESPONSE:
            return False, f"unexpected_reply_{message_id}", False
        ret = result_code(payload)
        if ret != LOGIN_OK:
            return False, f"login_failed_{ret}", ret in LOGIN_RETRY_CODES
        session = session_id(payload) or session

        ptz = ptz_payload(short)
        ptz["SessionID"] = f"0x{session:08X}"
        writer.write(pack(MSG_PTZ_REQUEST, ptz, session=session, sequence=1))
        await writer.drain()

        message_id, _session, payload = await _async_read(reader)
        if message_id != MSG_PTZ_RESPONSE:
            return False, f"unexpected_reply_{message_id}", False
        ret = result_code(payload)
        if ret != LOGIN_OK:
            return False, f"ptz_failed_{ret}", False
        return True, None, False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, RuntimeError):  # pragma: no cover - closing can fail
            pass


def result_code(payload: dict[str, Any]) -> int:
    """Return the ``Ret`` code of an answer, ``-1`` when it carries none."""
    try:
        return int(payload.get("Ret", NO_RESULT))
    except (TypeError, ValueError):
        return NO_RESULT


def session_id(payload: dict[str, Any]) -> int:
    """Return the session of a login answer (the devices send it as ``0x2C``)."""
    raw = payload.get("SessionID")
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 16)
    except (TypeError, ValueError):
        return 0


async def _async_read(reader: asyncio.StreamReader) -> tuple[int, int, dict[str, Any]]:
    """Read one complete DVRIP message."""
    header = await reader.readexactly(HEADER_SIZE)
    parsed = struct.unpack(HEADER_FORMAT, header)
    session, message_id, length = parsed[2], parsed[6], parsed[7]
    if length > MAX_PAYLOAD:
        raise ValueError(f"invalid_length_{length}")
    body = await reader.readexactly(length) if length else b""
    return message_id, session, parse_payload(body)
