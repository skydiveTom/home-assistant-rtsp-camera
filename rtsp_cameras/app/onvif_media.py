"""ONVIF Media service: which encoders a camera runs and which codec they send.

Home Assistant does not transcode an RTSP stream - it copies the packets of the camera
into HLS, and a browser plays them only when the codec inside is one it knows. A sub
stream that sends H.265 (``hvc1``) leaves Chrome, Edge and Firefox with a spinner while
Safari and iOS play it. The lever is the camera itself: this module reads the encoder
configurations of an ONVIF device and switches the codec of one of them.

The service is spoken the way ONVIF describes it: ``GetServices`` on the device service
names the XAddr of the media service, ``GetVideoEncoderConfigurations`` lists the
encoders and ``SetVideoEncoderConfiguration`` writes one back. The configuration is *not*
rebuilt from the fields the panel shows - the document the camera itself handed out is
sent back with the encoding word changed, so nothing the device knows (multicast, session
timeout, a rate control this module never heard of) is dropped on the way. A firmware
that is strict about the schema keeps its picture only that way.

Authentication: the SOAP request carries a WS-Security ``UsernameToken`` (what the
specification asks for) and the HTTP layer follows a Basic or Digest challenge on top of
it. Devices accept either one, so both are offered before a request is reported as
refused.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
SCHEMA_NS = "http://www.onvif.org/ver10/schema"
DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_ENCODING = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)

#: One SOAP request is answered in a moment; the value is a deadline per request, not
#: per read - a device that is not there costs one timeout, because a dead host is not
#: asked four more paths (see ``_read_blocking``).
DEFAULT_TIMEOUT = 8.0
MAX_TIMEOUT = 30.0
MAX_REPLY_BYTES = 262144

DEVICE_PATH = "/onvif/device_service"
#: Where firmwares offer the media service when ``GetServices`` says nothing. The
#: vendor answer of the test set is the first entry.
MEDIA_PATHS = ("/onvif/media_service", "/onvif/Media", "/onvif/media")
#: The codecs the panel offers. H.264 is what every browser plays, H.265 the one that
#: sends the smaller picture - switching *to* H.264 is the point of the whole module.
ENCODINGS = ("H264", "H265")

#: Error codes of the encoder endpoints (translated by app/locales/*.json).
ERROR_BAD_URL = "encoder_bad_url"
ERROR_UNREACHABLE = "encoder_unreachable"
ERROR_TIMEOUT = "encoder_timeout"
ERROR_AUTH = "encoder_auth"
ERROR_NO_MEDIA = "encoder_no_media"
ERROR_FAULT = "encoder_fault"
ERROR_BAD_ENCODING = "encoder_bad_encoding"
ERROR_TOKEN_REQUIRED = "encoder_token_required"
ERROR_TOKEN_UNKNOWN = "encoder_token_unknown"
#: Errors the *caller* can fix: the panel answers them without asking the camera again.
REQUEST_ERRORS = (
    ERROR_BAD_URL,
    ERROR_BAD_ENCODING,
    ERROR_TOKEN_REQUIRED,
    ERROR_TOKEN_UNKNOWN,
)

ET.register_namespace("s", SOAP_NS)
ET.register_namespace("trt", MEDIA_NS)
ET.register_namespace("tt", SCHEMA_NS)
ET.register_namespace("tds", DEVICE_NS)


@dataclass(slots=True)
class EncoderReport:
    """The encoders of a camera, as one read of the media service found them."""

    ok: bool = False
    media_url: str = ""
    configurations: list[dict[str, Any]] = field(default_factory=list)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    detail: str = ""
    #: True when a write was accepted - the panel tells "already H.264" apart with it.
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the representation the panel (and the tests) see."""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "media_url": self.media_url,
            "configurations": [public_configuration(item) for item in self.configurations],
            "profiles": list(self.profiles),
            "changed": self.changed,
        }
        if self.error:
            payload["error"] = self.error
            payload["detail"] = self.detail
        return payload


def public_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Return one encoder configuration without the XML element behind it."""
    return {key: value for key, value in configuration.items() if key != "element"}


# ------------------------------------------------------------ SOAP documents
def _security_header(username: str, password: str) -> str:
    """Return a WS-Security ``UsernameToken`` with a password digest.

    The digest is the one the specification defines: SHA1 over the nonce, the creation
    time and the password, base64 encoded. The plain password never leaves the add-on.
    """
    nonce = os.urandom(16)
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    nonce_text = base64.b64encode(nonce).decode("ascii")
    return (
        '<s:Header><wsse:Security s:mustUnderstand="1"><wsse:UsernameToken>'
        f"<wsse:Username>{escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{PASSWORD_DIGEST}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{BASE64_ENCODING}">{nonce_text}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security></s:Header>"
    )


def envelope(inner: str, *, security: str = "") -> str:
    """Wrap a body into a SOAP envelope that declares every prefix it uses."""
    return (
        f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:trt="{MEDIA_NS}" xmlns:tt="{SCHEMA_NS}"'
        f' xmlns:tds="{DEVICE_NS}" xmlns:wsse="{WSSE_NS}" xmlns:wsu="{WSU_NS}">'
        f"{security}<s:Body>{inner}</s:Body></s:Envelope>"
    )


def get_services_body() -> str:
    """Return the ``GetServices`` document: the device names its own media service."""
    return (
        "<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability>"
        "</tds:GetServices>"
    )


def get_configurations_body() -> str:
    """Return the ``GetVideoEncoderConfigurations`` document."""
    return "<trt:GetVideoEncoderConfigurations/>"


def get_profiles_body() -> str:
    """Return the ``GetProfiles`` document (the names of the streams)."""
    return "<trt:GetProfiles/>"


def set_configuration_body(configuration: Mapping[str, Any], encoding: str) -> str:
    """Return a ``SetVideoEncoderConfiguration`` with the codec replaced.

    The document of the camera is deep copied and sent back: everything this module does
    not model survives the write. Only the encoding word changes; the block of the new
    codec is filled in when the old configuration had none, and the block of the codec
    that is left behind is removed - two codec blocks in one configuration is what a
    strict firmware answers with a fault.
    """
    element = configuration.get("element")
    if not isinstance(element, ET.Element):
        raise ValueError("missing_configuration")

    wanted = str(encoding or "").strip().upper()
    # The parameter of the operation is called ``Configuration`` and lives in the
    # namespace of the media service, while the entries inside it belong to the schema -
    # the element of the reply (``Configurations``) is renamed, not rebuilt.
    document = ET.Element(f"{{{MEDIA_NS}}}Configuration", dict(element.attrib))
    for child in element:
        document.append(copy.deepcopy(child))
    _set_text(document, "Encoding", wanted)
    for other in ENCODINGS:
        if other == wanted:
            continue
        block = _first(document, other)
        if block is not None:
            document.remove(block)
    if _first(document, wanted) is None:
        block = ET.SubElement(document, f"{{{SCHEMA_NS}}}{wanted}")
        gov = configuration.get("gop")
        if gov:
            ET.SubElement(block, f"{{{SCHEMA_NS}}}GovLength").text = str(gov)
        if wanted == "H264":
            # Main is what a camera sends by default and what every browser decodes.
            ET.SubElement(block, f"{{{SCHEMA_NS}}}H264Profile").text = "Main"

    inner = ET.tostring(document, encoding="unicode")
    return (
        f"<trt:SetVideoEncoderConfiguration>{inner}"
        "<trt:ForcePersistence>true</trt:ForcePersistence>"
        "</trt:SetVideoEncoderConfiguration>"
    )


# ------------------------------------------------------------------ parsing
def _local(tag: Any) -> str:
    """Return the local name of an element tag (the namespace prefix removed)."""
    return str(tag).rsplit("}", 1)[-1]


def _first(node: ET.Element | None, name: str) -> ET.Element | None:
    """Return the first child of ``node`` with that local name."""
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node: ET.Element | None, name: str) -> str:
    """Return the text of a child element, stripped and empty when it is missing."""
    child = _first(node, name)
    return (child.text or "").strip() if child is not None else ""


def _set_text(node: ET.Element, name: str, value: str) -> None:
    """Write the text of a child element, creating it when the device left it out."""
    child = _first(node, name)
    if child is None:
        child = ET.SubElement(node, f"{{{SCHEMA_NS}}}{name}")
    child.text = value


def _int(value: Any) -> int | None:
    """Return a whole number out of an XML text, or None."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    """Return a number out of an XML text, or None."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse(text: str) -> ET.Element | None:
    """Parse an XML document, returning None for a reply that is not XML at all."""
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError):  # pragma: no cover - a broken device
        return None


def _descendants(node: ET.Element, name: str) -> list[ET.Element]:
    """Return every element below ``node`` (itself included) with that local name."""
    return [element for element in node.iter() if _local(element.tag) == name]


def parse_media_url(text: str) -> str | None:
    """Return the XAddr of the media service out of a ``GetServices`` answer.

    The ver20 namespace is left alone on purpose: its operations differ, and a device
    that offers both answers ver10 as well.
    """
    root = _parse(text)
    if root is None:
        return None
    for service in _descendants(root, "Service"):
        namespace = _text(service, "Namespace").rstrip("/")
        address = _text(service, "XAddr")
        if address and namespace == MEDIA_NS:
            return address
    return None


def parse_profiles(text: str) -> list[dict[str, str]]:
    """Return the profiles of a camera with the encoder token each one uses."""
    root = _parse(text)
    if root is None:
        return []
    profiles: list[dict[str, str]] = []
    for node in _descendants(root, "Profiles"):
        token = str(node.get("token") or "").strip()
        if not token:
            continue
        encoder = _first(node, "VideoEncoderConfiguration")
        profiles.append(
            {
                "token": token,
                "name": _text(node, "Name") or token,
                "encoder": str((encoder.get("token") if encoder is not None else "") or ""),
            }
        )
    return profiles


def parse_encoder_configurations(text: str) -> list[dict[str, Any]]:
    """Read the encoder configurations out of a SOAP answer.

    Each configuration keeps the element it was parsed from under ``element``: the write
    sends that very document back (see ``set_configuration_body``). The rest is what the
    panel shows - codec, picture, frame rate, bit rate and the keyframe interval.
    """
    root = _parse(text)
    if root is None:
        return []
    configurations: list[dict[str, Any]] = []
    for node in root.iter():
        if _local(node.tag) not in ("Configurations", "VideoEncoderConfiguration"):
            continue
        token = str(node.get("token") or "").strip()
        if not token:
            continue
        resolution = _first(node, "Resolution")
        rate_control = _first(node, "RateControl")
        codec = _first(node, "H264")
        if codec is None:
            codec = _first(node, "H265")
        configurations.append(
            {
                "token": token,
                "name": _text(node, "Name") or token,
                "use_count": _int(_text(node, "UseCount")),
                "encoding": _text(node, "Encoding").upper(),
                "width": _int(_text(resolution, "Width")),
                "height": _int(_text(resolution, "Height")),
                "quality": _number(_text(node, "Quality")),
                "fps": _number(_text(rate_control, "FrameRateLimit")),
                "encoding_interval": _int(_text(rate_control, "EncodingInterval")),
                "bitrate": _int(_text(rate_control, "BitrateLimit")),
                "gop": _int(_text(codec, "GovLength")),
                "profile": _text(codec, "H264Profile") or _text(codec, "H265Profile"),
                "element": node,
            }
        )
    return configurations


# --------------------------------------------------------------- transport
_FAULT_ELEMENT = re.compile(r"<(?:[\w.\-]+:)?Fault[\s>/]", re.IGNORECASE)
_FAULT_TEXT = re.compile(r"<(?:[\w.\-]+:)?(?:Text|faultstring)[^>]*>\s*([^<]{1,200})<")
_FAULT_SUBCODE = re.compile(r"<(?:[\w.\-]+:)?Value[^>]*>\s*([^<]{1,120})<")
#: Words of a fault that ask for credentials instead of naming an unsupported operation.
_AUTH_WORDS = (
    "notauthorized",
    "failedauthentication",
    "sender not authorized",
    "unauthorized",
    "invalidsecurity",
)


def fault_detail(text: str) -> str:
    """Return the words of a SOAP fault, or an empty string when there is none.

    The subcode is kept next to the reason: ``ter:ActionNotSupported`` says what a device
    cannot do, the reason often says only "Optional Action Not Implemented".
    """
    if not text or not _FAULT_ELEMENT.search(text):
        return ""
    parts: list[str] = []
    subcodes = _FAULT_SUBCODE.findall(text)
    if subcodes:
        # The last value is the specific one: ``s:Sender`` is the class of the fault,
        # ``ter:ActionNotSupported`` the answer that matters.
        parts.append(subcodes[-1].strip())
    reason = _FAULT_TEXT.search(text)
    if reason:
        parts.append(reason.group(1).strip())
    return ": ".join(part for part in parts if part) or "fault"


def is_auth_fault(detail: str) -> bool:
    """Return True when a fault is about the credentials, not about the operation."""
    text = str(detail or "").lower().replace(" ", "")
    return any(word.replace(" ", "") in text for word in _AUTH_WORDS)


def transport_error(detail: str) -> str:
    """Name the API error of a transport failure."""
    text = str(detail or "").lower()
    if "timed out" in text or "timeout" in text:
        return ERROR_TIMEOUT
    return ERROR_UNREACHABLE


def transport_report(status: int | None, error: str) -> EncoderReport:
    """Return the report of a request that never got an answer worth reading.

    A refused login is not an unreachable camera: the device is there and only wants other
    credentials, which is what the panel tells the user.
    """
    if status in (401, 403):
        return EncoderReport(error=ERROR_AUTH, detail=f"HTTP {status}")
    return EncoderReport(error=transport_error(error), detail=error)


def _opener(url: str, username: str, password: str) -> Any:
    """Return an opener that follows a Basic or a Digest challenge of the camera."""
    manager = HTTPPasswordMgrWithDefaultRealm()
    if username or password:
        parts = urlsplit(url)
        manager.add_password(None, f"{parts.scheme}://{parts.netloc}/", username, password)
    return build_opener(HTTPBasicAuthHandler(manager), HTTPDigestAuthHandler(manager))


def post_soap(
    url: str,
    body: str,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int | None, str | None, str]:
    """Send one SOAP document and return status, a transport error and the reply."""
    request = Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with _opener(url, username, password).open(request, timeout=timeout) as response:
            return (
                int(response.status),
                None,
                response.read(MAX_REPLY_BYTES).decode("utf-8", "replace"),
            )
    except HTTPError as err:
        return (
            int(err.code),
            f"HTTP {err.code}",
            err.read(MAX_REPLY_BYTES).decode("utf-8", "replace"),
        )
    except (HTTPException, URLError, OSError, ValueError) as err:
        return None, str(err) or err.__class__.__name__, ""


def call(
    url: str,
    inner: str,
    *,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int | None, str | None, str]:
    """Send a SOAP body, offering WS-Security first and the HTTP challenge second.

    A device that refuses the header (or wants its own scheme) still answers the plain
    request, so the second attempt is only made when the first one was refused.
    """
    bodies: list[str] = []
    if username:
        bodies.append(envelope(inner, security=_security_header(username, password)))
    bodies.append(envelope(inner))

    answer: tuple[int | None, str | None, str] = (None, ERROR_UNREACHABLE, "")
    for body in bodies:
        answer = post_soap(url, body, username, password, timeout)
        status, _error, text = answer
        fault = fault_detail(text)
        if status in (401, 403) or (fault and is_auth_fault(fault)):
            continue
        return answer
    return answer


def normalize_base_url(value: Any) -> str:
    """Return ``scheme://host[:port]`` of an address, or "" when it is unusable.

    An IP is enough - the panel offers the host of the RTSP URL as well - and a path
    that was typed along (``http://host/onvif/device_service``) is dropped.
    """
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def clamp_timeout(value: Any) -> float:
    """Return a per request timeout clamped to sane bounds."""
    seconds = _number(value)
    if seconds is None:
        return DEFAULT_TIMEOUT
    return max(1.0, min(MAX_TIMEOUT, seconds))


def candidate_urls(
    base_url: str,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[str], EncoderReport | None]:
    """Return the URLs worth asking for the media service, most likely first.

    The second value is the failure of a host that does not answer at all - no other path
    of it would, and a dead camera must not cost one timeout per path.
    """
    urls: list[str] = []
    status, error, text = call(
        f"{base_url}{DEVICE_PATH}",
        get_services_body(),
        username=username,
        password=password,
        timeout=timeout,
    )
    if error is not None and status is None:
        return urls, transport_report(status, error)
    if error is None and status is not None and status < 400:
        media = parse_media_url(text)
        if media:
            urls.append(media)
    for path in MEDIA_PATHS:
        url = f"{base_url}{path}"
        if url not in urls:
            urls.append(url)
    # Some firmwares answer the media operations on the device service as well.
    device = f"{base_url}{DEVICE_PATH}"
    if device not in urls:
        urls.append(device)
    return urls, None


# --------------------------------------------------------------- reading
def _read_blocking(
    base_url: str, username: str, password: str, timeout: float
) -> EncoderReport:
    """Ask every candidate URL for the encoder configurations of the camera."""
    failure = EncoderReport(error=ERROR_NO_MEDIA, detail="no_media_service")
    urls, dead = candidate_urls(base_url, username, password, timeout)
    if dead is not None:
        return dead
    for url in urls:
        status, error, text = call(
            url,
            get_configurations_body(),
            username=username,
            password=password,
            timeout=timeout,
        )
        fault = fault_detail(text or "")
        if fault:
            # A refusal arrives as an HTTP error *and* a fault that names it.
            failure = EncoderReport(
                error=ERROR_AUTH if is_auth_fault(fault) else ERROR_FAULT, detail=fault
            )
            continue
        if error is not None:
            failure = transport_report(status, error)
            if status is None:
                # Nothing answers on this host: the other paths are the same silence,
                # and a dead camera must not cost four timeouts.
                break
            continue
        configurations = parse_encoder_configurations(text or "")
        if not configurations:
            failure = EncoderReport(
                error=ERROR_NO_MEDIA, detail=f"no_configurations (HTTP {status})"
            )
            continue
        _status, _error, profiles_text = call(
            url,
            get_profiles_body(),
            username=username,
            password=password,
            timeout=timeout,
        )
        return EncoderReport(
            ok=True,
            media_url=url,
            configurations=configurations,
            profiles=parse_profiles(profiles_text or ""),
        )
    return failure


async def async_read(
    base_url: Any,
    *,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> EncoderReport:
    """Read the encoder configurations of a camera (a read only request)."""
    base = normalize_base_url(base_url)
    if not base:
        return EncoderReport(error=ERROR_BAD_URL, detail=str(base_url or ""))
    return await asyncio.to_thread(
        _read_blocking, base, username, password, clamp_timeout(timeout)
    )


# --------------------------------------------------------------- writing
def _set_blocking(
    base_url: str,
    token: str,
    encoding: str,
    username: str,
    password: str,
    timeout: float,
) -> EncoderReport:
    """Switch one encoder and report what the camera says afterwards."""
    reported = _read_blocking(base_url, username, password, timeout)
    if not reported.ok:
        return reported

    current = next(
        (item for item in reported.configurations if item["token"] == token), None
    )
    if current is None:
        return EncoderReport(error=ERROR_TOKEN_UNKNOWN, detail=token)
    if str(current.get("encoding") or "").upper() == encoding:
        # Nothing to do: the still to re-encode is a still the camera already sends.
        return EncoderReport(
            ok=True,
            media_url=reported.media_url,
            configurations=reported.configurations,
            profiles=reported.profiles,
            changed=False,
        )

    status, error, text = call(
        reported.media_url,
        set_configuration_body(current, encoding),
        username=username,
        password=password,
        timeout=timeout,
    )
    fault = fault_detail(text or "")
    if fault:
        return EncoderReport(
            error=ERROR_AUTH if is_auth_fault(fault) else ERROR_FAULT, detail=fault
        )
    if error is not None:
        return transport_report(status, error)
    if status is not None and status >= 400:
        return EncoderReport(error=ERROR_FAULT, detail=f"HTTP {status}")

    # The write is only real when the camera reports it: read the device again.
    after = _read_blocking(base_url, username, password, timeout)
    if not after.ok:
        return EncoderReport(
            ok=True,
            media_url=reported.media_url,
            configurations=reported.configurations,
            profiles=reported.profiles,
            changed=True,
            detail="reread_failed",
        )
    after.changed = True
    return after


async def async_set_encoding(
    base_url: Any,
    *,
    token: Any,
    encoding: Any,
    username: str = "",
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> EncoderReport:
    """Change the codec of one encoder and return the encoders the camera reports."""
    base = normalize_base_url(base_url)
    if not base:
        return EncoderReport(error=ERROR_BAD_URL, detail=str(base_url or ""))
    wanted = str(encoding or "").strip().upper()
    if wanted not in ENCODINGS:
        return EncoderReport(error=ERROR_BAD_ENCODING, detail=str(encoding or ""))
    name = str(token or "").strip()
    if not name:
        return EncoderReport(error=ERROR_TOKEN_REQUIRED)
    return await asyncio.to_thread(
        _set_blocking, base, name, wanted, username, password, clamp_timeout(timeout)
    )

