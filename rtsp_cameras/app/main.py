"""Starlette application: web interface and REST API of the add-on."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .actions import (
    ACTION_INSTALL_ADDON_UPDATE,
    ACTION_REFRESH_ADDON_UPDATE,
    request_action,
)
from .config import (
    PREVIEW_MODE_AUTO,
    PREVIEW_MODE_CHOICES,
    SUPPORTED_LANGUAGES,
    SUPPORTED_PREVIEW_MODES,
    SUPPORTED_TRANSPORTS,
    Settings,
)
from .ffmpeg import FFmpegService, FFmpegUnavailable, HlsSession, StreamFailed
from .ha import HomeAssistantClient
from .i18n import Translations, detect_language
from .integration import IntegrationInstaller
from .models import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    Camera,
    utcnow,
    validate_optional_stream_url,
    validate_stream_url,
)
from .storage import CameraStore

_LOGGER = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

MJPEG_BOUNDARY = "rtspcamframe"
HLS_IDLE_SECONDS = 45
HEALTH_MIN_INTERVAL = 15
# How long the automatic preview detection waits for the first frame / playlist.
PREVIEW_DETECT_TIMEOUT = 10

API_ERRORS = (
    "generic",
    "network",
    "name_required",
    "url_required",
    "url_invalid",
    "url_scheme",
    "not_found",
    "ffmpeg_missing",
    "ffprobe_missing",
    "timeout",
    "no_video",
    "failed",
    "storage_error",
    "source_missing",
    "install_failed",
    "restart_failed",
    "stream_failed",
    "supervisor_missing",
    "update_check_failed",
    "update_failed",
)


@dataclass(slots=True)
class AppContext:
    """Everything the request handlers share."""

    settings: Settings
    store: CameraStore
    ffmpeg: FFmpegService
    ha: HomeAssistantClient
    installer: IntegrationInstaller
    translations: Translations
    templates: Jinja2Templates
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)


def _ctx(request: Request) -> AppContext:
    """Return the application context of a request."""
    return request.app.state.ctx


def _ok(**payload: Any) -> JSONResponse:
    """Return a successful API response."""
    return JSONResponse({"ok": True, **payload})


async def _error(
    request: Request,
    code: str,
    status: int = 400,
    **values: Any,
) -> JSONResponse:
    """Return a translated API error response."""
    context = _ctx(request)
    language = await _language(request)
    return JSONResponse(
        {
            "ok": False,
            "error": code,
            "message": context.translations.error(code, language, **values),
        },
        status_code=status,
    )


async def _language(request: Request) -> str:
    """Resolve the language for this request."""
    context = _ctx(request)
    return await detect_language(
        context.settings,
        context.ha,
        accept_language=request.headers.get("accept-language"),
        requested=request.query_params.get("lang"),
    )


def _base_path(request: Request) -> str:
    """Return the ingress base path used by the browser, without a trailing slash."""
    header = request.headers.get("x-ingress-path") or ""
    if not header:
        return ""
    return "/" + header.strip().strip("/")


async def _json_body(request: Request) -> dict[str, Any]:
    """Read a JSON body, returning an empty mapping when unusable."""
    try:
        payload = await request.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _apply_probe(camera: Camera, result: Any) -> None:
    """Copy the outcome of a stream test onto the camera."""
    camera.last_checked = utcnow()
    camera.status = STATUS_ONLINE if result.ok else STATUS_OFFLINE
    camera.last_error = None if result.ok else result.error
    camera.last_probe = result.to_dict()


def _settings_payload(context: AppContext) -> dict[str, Any]:
    """Describe the effective add-on settings for the interface."""
    settings = context.settings
    return {
        "version": settings.addon_version,
        "language": settings.language,
        "default_rtsp_transport": settings.default_rtsp_transport,
        "preview_mode": settings.preview_mode,
        "health_check_interval": settings.health_check_interval,
        "test_timeout": settings.test_timeout,
        "preview_fps": settings.preview_fps,
        "preview_max_height": settings.preview_max_height,
        "install_integration": settings.install_integration,
        "ha_restart_after_install": settings.ha_restart_after_install,
        "redact_credentials_in_logs": settings.redact_credentials_in_logs,
        "cameras_file": str(settings.cameras_file),
        "published_file": str(settings.published_file),
        "integration_target": str(settings.integration_target),
        "ffmpeg_available": context.ffmpeg.available,
        "publish_error": context.store.publish_error,
        "transports": list(SUPPORTED_TRANSPORTS),
        "preview_modes": list(SUPPORTED_PREVIEW_MODES),
        "preview_mode_choices": list(PREVIEW_MODE_CHOICES),
        "preview_mode_auto": PREVIEW_MODE_AUTO,
        "languages": list(SUPPORTED_LANGUAGES),
        "supervisor_api": context.ha.enabled,
    }


def _cameras_payload(context: AppContext) -> list[dict[str, Any]]:
    """Return every camera in API form."""
    return [camera.to_api_dict() for camera in context.store.list()]


async def index(request: Request) -> HTMLResponse:
    """Render the single page interface."""
    context = _ctx(request)
    language = await _language(request)
    base_path = _base_path(request)
    bootstrap = {
        "base_path": base_path,
        "language": language,
        "settings": _settings_payload(context),
        "cameras": _cameras_payload(context),
        "integration": context.installer.status().to_dict(),
        "i18n": context.translations.bundles,
        "languages": list(SUPPORTED_LANGUAGES),
    }
    encoded = json.dumps(bootstrap, ensure_ascii=False).replace("</", "<\\/")
    return context.templates.TemplateResponse(
        request,
        "index.html",
        {
            "base_path": base_path,
            "language": language,
            "bootstrap": encoded,
            "addon_version": context.settings.addon_version,
            "title": context.translations.translate("app.title", language),
        },
    )


async def health(request: Request) -> JSONResponse:
    """Answer the Supervisor watchdog."""
    context = _ctx(request)
    return _ok(
        status="ok",
        version=context.settings.addon_version,
        uptime_seconds=round(time.monotonic() - context.started_at),
        cameras=context.store.count(),
        ffmpeg=context.ffmpeg.available,
    )


async def meta(request: Request) -> JSONResponse:
    """Return settings, cameras and integration state for the interface."""
    context = _ctx(request)
    return _ok(
        language=await _language(request),
        settings=_settings_payload(context),
        cameras=_cameras_payload(context),
        integration=context.installer.status().to_dict(),
    )


# ------------------------------------------------------------------ cameras
async def cameras(request: Request) -> JSONResponse:
    """List all cameras or create a new one."""
    context = _ctx(request)
    if request.method == "GET":
        return _ok(cameras=_cameras_payload(context))

    body = await _json_body(request)
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    transport = (
        str(body.get("rtsp_transport") or context.settings.default_rtsp_transport)
        .strip()
        .lower()
    )
    enabled = bool(body.get("enabled", True))
    ha_stream_url = str(body.get("ha_stream_url") or "").strip()

    if not name:
        return await _error(request, "name_required", 400)
    url_error = validate_stream_url(url)
    if url_error:
        return await _error(request, url_error, 400)
    ha_url_error = validate_optional_stream_url(ha_stream_url)
    if ha_url_error:
        return await _error(request, ha_url_error, 400)
    if transport not in SUPPORTED_TRANSPORTS:
        transport = context.settings.default_rtsp_transport

    camera = context.store.add(
        name=name,
        url=url,
        rtsp_transport=transport,
        enabled=enabled,
        ha_stream_url=ha_stream_url,
    )
    if context.store.publish_error:
        _LOGGER.error("Camera file could not be published")
    return _ok(
        camera=camera.to_api_dict(),
        publish_error=context.store.publish_error,
        cameras=_cameras_payload(context),
    )


async def camera_item(request: Request) -> JSONResponse:
    """Read, update or delete one camera."""
    context = _ctx(request)
    camera_id = request.path_params["camera_id"]
    camera = context.store.get(camera_id)
    if camera is None:
        return await _error(request, "not_found", 404)

    if request.method == "GET":
        return _ok(camera=camera.to_api_dict())

    if request.method == "DELETE":
        await context.ffmpeg.stop_hls(camera_id)
        context.store.delete(camera_id)
        return _ok(deleted=camera_id, cameras=_cameras_payload(context))

    body = await _json_body(request)
    changes: dict[str, Any] = {}
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            return await _error(request, "name_required", 400)
        changes["name"] = name
    if "url" in body:
        url = str(body.get("url") or "").strip()
        url_error = validate_stream_url(url)
        if url_error:
            return await _error(request, url_error, 400)
        changes["url"] = url
    if "rtsp_transport" in body:
        transport = str(body.get("rtsp_transport") or "").strip().lower()
        if transport in SUPPORTED_TRANSPORTS:
            changes["rtsp_transport"] = transport
        else:
            changes["rtsp_transport"] = context.settings.default_rtsp_transport
    if "enabled" in body:
        changes["enabled"] = bool(body.get("enabled"))
    if "ha_stream_url" in body:
        ha_stream_url = str(body.get("ha_stream_url") or "").strip()
        ha_url_error = validate_optional_stream_url(ha_stream_url)
        if ha_url_error:
            return await _error(request, ha_url_error, 400)
        changes["ha_stream_url"] = ha_stream_url

    if "url" in changes or "rtsp_transport" in changes:
        # The learned preview mode belongs to the previous stream.
        changes["preview_mode"] = ""

    updated = context.store.update(camera_id, **changes)
    if updated is None:
        return await _error(request, "not_found", 404)
    return _ok(camera=updated.to_api_dict(), cameras=_cameras_payload(context))


async def camera_test(request: Request) -> JSONResponse:
    """Run ffprobe against a camera and store the result.

    Works both for a saved camera (/api/cameras/<id>/test) and for a URL that
    has not been saved yet (/api/probe).
    """
    context = _ctx(request)
    camera: Camera | None = None
    camera_id = request.path_params.get("camera_id")

    if camera_id:
        camera = context.store.get(camera_id)
        if camera is None:
            return await _error(request, "not_found", 404)
        url = camera.url
        transport = camera.rtsp_transport
    else:
        body = await _json_body(request)
        url = str(body.get("url") or "").strip()
        transport = (
            str(body.get("rtsp_transport") or context.settings.default_rtsp_transport)
            .strip()
            .lower()
        )
        url_error = validate_stream_url(url)
        if url_error:
            return await _error(request, url_error, 400)

    if not context.ffmpeg.available:
        return await _error(request, "ffmpeg_missing", 503)

    result = await context.ffmpeg.probe(url, transport)
    if camera is not None:
        before = (camera.status, camera.last_probe)
        _apply_probe(camera, result)
        if (camera.status, camera.last_probe) != before:
            # The published file also carries the codec, which is what tells
            # Home Assistant whether it can play the stream.
            context.store.save()
    return _ok(
        probe=result.to_dict(),
        camera=camera.to_api_dict() if camera is not None else None,
    )


# ------------------------------------------------------------------ streams
HLS_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _optional_int(value: Any) -> int | None:
    """Parse an optional integer query parameter."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _mjpeg_part(frame: bytes) -> bytes:
    """Frame one JPEG image as a multipart body part."""
    header = (
        f"--{MJPEG_BOUNDARY}\r\n"
        "Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    )
    return header.encode("ascii") + frame + b"\r\n"


async def camera_snapshot(request: Request) -> Response:
    """Return one JPEG frame of a camera."""
    context = _ctx(request)
    camera = context.store.get(request.path_params["camera_id"])
    if camera is None:
        return await _error(request, "not_found", 404)
    if not context.ffmpeg.available:
        return await _error(request, "ffmpeg_missing", 503)

    image = await context.ffmpeg.snapshot(
        camera.url,
        camera.rtsp_transport,
        max_height=_optional_int(request.query_params.get("height")),
    )
    if image is None:
        return await _error(request, "stream_failed", 502)
    return Response(
        image,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def camera_mjpeg(request: Request) -> Response:
    """Stream the camera as MJPEG multipart frames."""
    context = _ctx(request)
    camera = context.store.get(request.path_params["camera_id"])
    if camera is None:
        return await _error(request, "not_found", 404)
    if not context.ffmpeg.available:
        return await _error(request, "ffmpeg_missing", 503)

    frames = context.ffmpeg.mjpeg_stream(
        camera.url,
        camera.rtsp_transport,
        fps=_optional_int(request.query_params.get("fps")),
        max_height=_optional_int(request.query_params.get("height")),
    )
    try:
        first = await asyncio.wait_for(
            anext(frames), timeout=context.settings.test_timeout + 5
        )
    except (StreamFailed, FFmpegUnavailable) as err:
        with contextlib.suppress(Exception):
            await frames.aclose()
        _LOGGER.info("MJPEG preview of %s failed: %s", camera.id, err)
        return JSONResponse(
            {"ok": False, "error": "stream_failed", "detail": str(err)},
            status_code=502,
        )
    except TimeoutError:
        with contextlib.suppress(Exception):
            await frames.aclose()
        return await _error(request, "timeout", 504)
    except StopAsyncIteration:
        return await _error(request, "stream_failed", 502)

    async def body() -> AsyncIterator[bytes]:
        """Yield the multipart stream until the client disconnects."""
        try:
            yield _mjpeg_part(first)
            async for frame in frames:
                yield _mjpeg_part(frame)
        except (StreamFailed, FFmpegUnavailable) as err:
            _LOGGER.info("MJPEG preview of %s stopped: %s", camera.id, err)
        finally:
            with contextlib.suppress(Exception):
                await frames.aclose()

    return StreamingResponse(
        body(),
        media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


async def _start_hls_session(
    context: AppContext, camera: Camera
) -> tuple[HlsSession | None, str | None]:
    """Start an HLS session for a camera and wait until its playlist exists.

    Returns the running session, or None together with an error message. The
    special value ``ffmpeg_missing`` means the container has no ffmpeg at all.
    """
    probe_details = (camera.last_probe or {}).get("details") or {}
    try:
        session = await context.ffmpeg.start_hls(
            camera.id,
            camera.url,
            camera.rtsp_transport,
            codec=probe_details.get("codec"),
        )
    except FFmpegUnavailable:
        return None, "ffmpeg_missing"

    deadline = time.monotonic() + context.settings.test_timeout
    while time.monotonic() < deadline:
        if session.playlist.is_file():
            return session, None
        if session.process.returncode is not None:
            break
        await asyncio.sleep(0.25)

    error = await context.ffmpeg.hls_error(session)
    await context.ffmpeg.stop_hls(camera.id)
    return None, error or "stream_failed"


async def camera_hls_start(request: Request) -> JSONResponse:
    """Start an HLS session and wait until the playlist exists."""
    context = _ctx(request)
    camera = context.store.get(request.path_params["camera_id"])
    if camera is None:
        return await _error(request, "not_found", 404)
    if not context.ffmpeg.available:
        return await _error(request, "ffmpeg_missing", 503)

    session, error = await _start_hls_session(context, camera)
    if session is None:
        if error == "ffmpeg_missing":
            return await _error(request, "ffmpeg_missing", 503)
        _LOGGER.info("HLS preview of %s failed: %s", camera.id, error)
        return JSONResponse(
            {"ok": False, "error": "stream_failed", "detail": error},
            status_code=502,
        )
    return _ok(
        mode="hls",
        playlist=f"api/cameras/{camera.id}/hls/index.m3u8",
    )


async def _probe_mjpeg(context: AppContext, camera: Camera, timeout: int) -> str | None:
    """Return None when MJPEG produced a frame, otherwise the reason."""
    frames = context.ffmpeg.mjpeg_stream(camera.url, camera.rtsp_transport)
    try:
        await asyncio.wait_for(anext(frames), timeout=timeout)
    except (StreamFailed, FFmpegUnavailable) as err:
        return str(err)
    except TimeoutError:
        return f"No frame within {timeout} seconds"
    except StopAsyncIteration:
        return "ffmpeg stopped without producing a frame"
    finally:
        with contextlib.suppress(Exception):
            await frames.aclose()
    return None


async def camera_preview_detect(request: Request) -> JSONResponse:
    """Find out which preview implementation works and remember it.

    With the add-on option set to a concrete mode nothing is tested: the
    configured mode is used. In automatic mode MJPEG is tried first (lowest
    latency) and HLS second, and the winner is stored on the camera.
    """
    context = _ctx(request)
    camera = context.store.get(request.path_params["camera_id"])
    if camera is None:
        return await _error(request, "not_found", 404)
    if not context.ffmpeg.available:
        return await _error(request, "ffmpeg_missing", 503)

    configured = context.settings.preview_mode
    if configured in SUPPORTED_PREVIEW_MODES:
        context.store.set_preview_mode(camera.id, configured)
        return _ok(
            mode=configured,
            auto=False,
            cached=False,
            camera=camera.to_api_dict(),
        )

    forced = str(request.query_params.get("force") or "").lower() in ("1", "true", "yes")
    if camera.preview_mode and not forced:
        return _ok(
            mode=camera.preview_mode,
            auto=True,
            cached=True,
            camera=camera.to_api_dict(),
        )

    timeout = min(int(context.settings.test_timeout), PREVIEW_DETECT_TIMEOUT)
    attempts: dict[str, str] = {}

    mjpeg_error = await _probe_mjpeg(context, camera, timeout)
    if mjpeg_error is None:
        context.store.set_preview_mode(camera.id, "mjpeg")
        _LOGGER.info("Preview of %s uses MJPEG", camera.id)
        return _ok(
            mode="mjpeg",
            auto=True,
            cached=False,
            camera=camera.to_api_dict(),
        )

    attempts["mjpeg"] = mjpeg_error
    _LOGGER.info("MJPEG preview of %s is not usable: %s", camera.id, mjpeg_error)

    session, hls_error = await _start_hls_session(context, camera)
    if session is not None:
        context.store.set_preview_mode(camera.id, "hls")
        _LOGGER.info("Preview of %s uses HLS", camera.id)
        return _ok(
            mode="hls",
            auto=True,
            cached=False,
            playlist=f"api/cameras/{camera.id}/hls/index.m3u8",
            attempts=attempts,
            camera=camera.to_api_dict(),
        )

    attempts["hls"] = hls_error or "stream_failed"
    _LOGGER.info("No working preview mode for %s: %s", camera.id, attempts)
    return JSONResponse(
        {
            "ok": False,
            "error": "stream_failed",
            "detail": attempts.get("hls") or attempts.get("mjpeg"),
            "attempts": attempts,
        },
        status_code=502,
    )


async def camera_hls_file(request: Request) -> Response:
    """Serve a playlist or segment of a running HLS session."""
    context = _ctx(request)
    camera_id = request.path_params["camera_id"]
    filename = request.path_params["filename"]
    if not HLS_FILENAME.match(filename):
        return await _error(request, "not_found", 404)

    session = context.ffmpeg.get_session(camera_id)
    if session is None:
        return await _error(request, "stream_failed", 409)

    path = session.directory / filename
    if not path.is_file():
        return await _error(request, "not_found", 404)

    media_type = (
        "application/vnd.apple.mpegurl"
        if filename.endswith(".m3u8")
        else "video/mp2t"
    )
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# -------------------------------------------------------------- integration
async def integration_status(request: Request) -> JSONResponse:
    """Report whether the bundled integration is installed."""
    context = _ctx(request)
    return _ok(integration=context.installer.status().to_dict())


async def integration_install(request: Request) -> JSONResponse:
    """Install or update the bundled integration."""
    context = _ctx(request)
    body = await _json_body(request)
    status = context.installer.install()
    if status.error:
        code = status.error if status.error == "source_missing" else "install_failed"
        return await _error(request, code, 500)

    restart_requested = False
    if context.settings.ha_restart_after_install:
        restart_requested = await context.ha.async_restart_core()
        if restart_requested:
            context.installer.clear_restart_flag()
    _LOGGER.info("Integration install requested (force=%s)", bool(body.get("force")))
    return _ok(integration=status.to_dict(), restart_requested=restart_requested)


async def ha_restart(request: Request) -> JSONResponse:
    """Ask Home Assistant to restart so the integration gets loaded."""
    context = _ctx(request)
    if not await context.ha.async_restart_core():
        return await _error(request, "restart_failed", 502)
    context.installer.clear_restart_flag()
    return _ok(restarted=True)


# --------------------------------------------------------------- add-on update
def _addon_update_payload(context: AppContext, info: dict[str, Any]) -> dict[str, Any]:
    """Add the information the interface needs to offer the right button."""
    payload = dict(info)
    # With the integration installed, Home Assistant can install the update even
    # when this container has no Supervisor token.
    payload["via_home_assistant"] = context.installer.status().installed
    return payload


async def addon_update_status(request: Request) -> JSONResponse:
    """Report whether a newer version of this add-on is available."""
    context = _ctx(request)
    info = await context.ha.async_addon_update_info()
    return _ok(addon=_addon_update_payload(context, info))


async def addon_update_check(request: Request) -> JSONResponse:
    """Force Supervisor to look for a new add-on version.

    A missing or refused Supervisor is reported inside the payload instead of as
    an error, so the interface can show the version it knows plus the reason.
    """
    context = _ctx(request)
    if context.ha.enabled:
        reloaded = await context.ha.async_store_reload()
    else:
        # No token: Home Assistant does the check for us (see the integration).
        reloaded = request_action(context.settings, ACTION_REFRESH_ADDON_UPDATE)

    info = await context.ha.async_addon_update_info()
    if not info.get("error"):
        info["error"] = context.ha.last_error
    _LOGGER.info(
        "Add-on update check: installed %s, latest %s, reloaded=%s, source=%s",
        info.get("version"),
        info.get("version_latest"),
        reloaded,
        info.get("source"),
    )
    return _ok(reloaded=reloaded, addon=_addon_update_payload(context, info))


async def addon_update_install(request: Request) -> JSONResponse:
    """Ask Supervisor - or Home Assistant - to update this add-on."""
    context = _ctx(request)

    if context.ha.enabled:
        if not await context.ha.async_update_addon():
            return await _error(request, "update_failed", 502)
        _LOGGER.info("Add-on update started by Supervisor, the container will restart")
        context.installer.mark_restart_needed()
        return _ok(
            update_started=True,
            via="supervisor",
            addon=_addon_update_payload(context, await context.ha.async_addon_update_info()),
        )

    # Without a Supervisor token the integration does it through Home Assistant.
    if not request_action(
        context.settings,
        ACTION_INSTALL_ADDON_UPDATE,
        version=context.settings.addon_version,
    ):
        return await _error(request, "update_failed", 502)

    context.installer.mark_restart_needed()
    return _ok(
        update_started=True,
        via="home_assistant",
        addon=_addon_update_payload(context, await context.ha.async_addon_update_info()),
    )


# ----------------------------------------------------------- background jobs
async def _check_all(context: AppContext) -> None:
    """Probe every enabled camera once."""
    if not context.ffmpeg.available:
        return
    timeout = min(context.settings.test_timeout, 10)
    changed = False
    for camera in context.store.list():
        if not camera.enabled:
            continue
        result = await context.ffmpeg.probe(camera.url, camera.rtsp_transport, timeout)
        before = (camera.status, camera.last_probe)
        _apply_probe(camera, result)
        if (camera.status, camera.last_probe) != before:
            changed = True
    if changed:
        # Keep the published codec (used by the integration) up to date.
        context.store.save()


async def _health_loop(context: AppContext) -> None:
    """Check the configured cameras on a fixed interval."""
    interval = max(HEALTH_MIN_INTERVAL, context.settings.health_check_interval)
    while True:
        try:
            await _check_all(context)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the task alive
            _LOGGER.exception("Stream health check failed")
        await asyncio.sleep(interval)


async def _reaper_loop(context: AppContext) -> None:
    """Stop idle HLS preview sessions."""
    while True:
        await asyncio.sleep(15)
        try:
            await context.ffmpeg.reap_hls(HLS_IDLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the task alive
            _LOGGER.exception("HLS cleanup failed")


@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Prepare the add-on on start and clean up on stop."""
    context: AppContext = app.state.ctx
    _LOGGER.info(
        "RTSP Camera Manager %s starting (language=%s, ingress port=%s)",
        context.settings.addon_version,
        context.settings.language,
        context.settings.ingress_port,
    )
    if not context.ffmpeg.available:
        _LOGGER.warning(
            "ffmpeg or ffprobe is missing, stream tests and previews are disabled"
        )

    report = context.ha.environment_report()
    if report["token"]:
        _LOGGER.info("Supervisor API reachable via %s", report["api_url"])
    else:
        _LOGGER.warning(
            "No Supervisor token in this container (variables: %s, socket: %s). "
            "Supervisor injects SUPERVISOR_TOKEN when hassio_api is enabled - reload "
            "the add-on store and update the add-on to recreate the container. Until "
            "then the update check and the update itself go through Home Assistant.",
            report["variables"] or "none",
            report["socket"],
        )

    context.store.load()

    if context.settings.install_integration:
        status = context.installer.status()
        if not status.installed or status.update_available:
            _LOGGER.info("Installing the Home Assistant integration")
            context.installer.install()

    if context.settings.health_check_interval > 0:
        context.tasks.append(asyncio.create_task(_health_loop(context)))
    context.tasks.append(asyncio.create_task(_reaper_loop(context)))

    try:
        yield
    finally:
        for task in context.tasks:
            task.cancel()
        for task in context.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await context.ffmpeg.stop_all()
        _LOGGER.info("RTSP Camera Manager stopped")


def create_app(settings: Settings | None = None) -> Starlette:
    """Build the Starlette application."""
    resolved = settings or Settings.load()
    context = AppContext(
        settings=resolved,
        store=CameraStore(resolved),
        ffmpeg=FFmpegService(resolved),
        ha=HomeAssistantClient(resolved),
        installer=IntegrationInstaller(resolved),
        translations=Translations(),
        templates=Jinja2Templates(directory=str(TEMPLATES_DIR)),
    )

    routes = [
        Route("/", index),
        Route("/health", health),
        Route("/api", meta),
        Route("/api/meta", meta),
        Route("/api/cameras", cameras, methods=["GET", "POST"]),
        Route("/api/probe", camera_test, methods=["POST"]),
        Route(
            "/api/cameras/{camera_id}",
            camera_item,
            methods=["GET", "PUT", "DELETE"],
        ),
        Route("/api/cameras/{camera_id}/test", camera_test, methods=["POST"]),
        Route("/api/cameras/{camera_id}/snapshot.jpg", camera_snapshot),
        Route("/api/cameras/{camera_id}/mjpeg", camera_mjpeg),
        Route("/api/cameras/{camera_id}/preview/detect", camera_preview_detect, methods=["POST"]),
        Route("/api/cameras/{camera_id}/hls/start", camera_hls_start, methods=["POST"]),
        Route("/api/cameras/{camera_id}/hls/{filename}", camera_hls_file),
        Route("/api/integration", integration_status),
        Route("/api/integration/install", integration_install, methods=["POST"]),
        Route("/api/ha/restart", ha_restart, methods=["POST"]),
        Route("/api/addon/update", addon_update_status),
        Route("/api/addon/update/check", addon_update_check, methods=["POST"]),
        Route("/api/addon/update/install", addon_update_install, methods=["POST"]),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]

    application = Starlette(routes=routes, lifespan=lifespan)
    application.state.ctx = context
    return application


def main() -> None:
    """Run the add-on web server."""
    import uvicorn

    settings = Settings.load()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.ingress_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
