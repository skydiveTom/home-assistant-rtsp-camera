"""Camera platform for the RTSP Camera Manager integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import aiohttp
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.stream.const import CONF_RTSP_TRANSPORT, RTSP_TRANSPORTS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ADD_CAMERA_URL,
    ATTR_CAMERA_ID,
    ATTR_CODEC,
    ATTR_PTZ,
    ATTR_PTZ_PRESETS,
    ATTR_RTSP_TRANSPORT,
    ATTR_SOURCE_FILE,
    ATTR_STREAM_URL,
    DOMAIN,
    KEYFRAME_WAIT_SECONDS,
    MANUFACTURER,
    PTZ_DIRECTIONS,
    SNAPSHOT_TIMEOUT_SECONDS,
)
from .coordinator import RtspCamerasCoordinator
from .models import PtzConfig, RtspCameraDefinition, redact_url
from .ptz import async_execute as ptz_async_execute
from .ptz import async_move as ptz_async_move
from .services import default_duration, resolve_action, resolve_duration, resolve_speed

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the camera entities for a config entry."""
    coordinator: RtspCamerasCoordinator = entry.runtime_data
    manager = RtspCameraEntityManager(hass, coordinator, async_add_entities)
    manager.start()
    entry.async_on_unload(manager.async_stop)


class RtspCameraEntityManager:
    """Keep the camera entities in sync with the file published by the add-on."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: RtspCamerasCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Initialise the manager."""
        self.hass = hass
        self.coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._entities: dict[str, RtspCamera] = {}
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        """Register for coordinator updates and create the initial entities."""
        self._unsub = self.coordinator.async_add_listener(self._async_sync)
        self._async_sync()

    @callback
    def async_stop(self) -> None:
        """Stop listening for updates."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _async_sync(self) -> None:
        """Add new, update changed and remove deleted cameras."""
        wanted: dict[str, RtspCameraDefinition] = self.coordinator.data or {}

        for camera_id in list(self._entities):
            if camera_id in wanted:
                continue
            entity = self._entities.pop(camera_id)
            _LOGGER.info(
                "Camera %s disappeared from the add-on, removing its entity", camera_id
            )
            self.hass.async_create_task(entity.async_remove(force_remove=True))

        new_entities: list[RtspCamera] = []
        for camera_id, definition in wanted.items():
            entity = self._entities.get(camera_id)
            if entity is None:
                entity = RtspCamera(self.coordinator, definition)
                self._entities[camera_id] = entity
                new_entities.append(entity)
            else:
                entity.async_update_definition(definition)

        if new_entities:
            _LOGGER.info(
                "Registered %d camera entity/entities: %s",
                len(new_entities),
                ", ".join(sorted(entity.definition.name for entity in new_entities)),
            )
            for entity in new_entities:
                if not entity.definition.plays_in_browsers:
                    _LOGGER.warning(
                        "Camera %s streams %s. Home Assistant camera cards can only "
                        "play H.265 in a few browsers - set the camera to H.264 or "
                        "publish an H.264 sub stream for Home Assistant",
                        entity.definition.name,
                        entity.definition.codec,
                    )
            self._async_add_entities(new_entities, update_before_add=True)


class RtspCamera(CoordinatorEntity[RtspCamerasCoordinator], Camera):
    """A camera entity backed by a plain RTSP/RTMP/HTTP stream URL."""

    _attr_has_entity_name = False
    _attr_supported_features = CameraEntityFeature.STREAM
    # NOTE: Home Assistant 2026.9 has no ``_attr_use_stream_for_stills`` attribute -
    # ``Camera.use_stream_for_stills`` is a plain property that returns False - so
    # stills are produced by ``async_camera_image`` below.

    def __init__(
        self,
        coordinator: RtspCamerasCoordinator,
        definition: RtspCameraDefinition,
    ) -> None:
        """Initialise the entity.

        ``CoordinatorEntity.__init__`` does not take part in the cooperative
        initialisation chain, so both base classes have to be initialised
        explicitly - otherwise the camera never gets its access token, cache and
        stream bookkeeping and Home Assistant rejects the entity.
        """
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._definition = definition
        self._attr_name = definition.name
        self._attr_unique_id = f"{DOMAIN}_{definition.id}"
        self.async_update_stream_options()

    @callback
    def async_update_stream_options(self) -> None:
        """Hand the RTSP transport over to the Home Assistant stream component.

        Home Assistant's ``Stream`` honours ``rtsp_transport`` in
        ``Camera.stream_options``, so the camera card streams exactly like the
        add-on. That matters for H.265 (or any high bitrate) stream: RTSP over UDP
        loses packets, ffprobe/``Test stream`` never notices it and the video stays
        black.
        """
        transport = str(self._definition.rtsp_transport or "").strip().lower()
        if transport and transport in RTSP_TRANSPORTS:
            self.stream_options = {CONF_RTSP_TRANSPORT: transport}
        else:  # pragma: no cover - the add-on only publishes known transports
            self.stream_options = {}

    @callback
    def async_update_definition(self, definition: RtspCameraDefinition) -> None:
        """Apply a changed camera definition."""
        if definition == self._definition:
            return
        self._definition = definition
        self._attr_name = definition.name
        self.async_update_stream_options()
        self.async_write_ha_state()

    @property
    def definition(self) -> RtspCameraDefinition:
        """Return the camera definition currently in use."""
        return self._definition

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the camera.

        The device page links to the documentation that explains how cameras are
        added in the add-on panel.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, self._definition.id)},
            name=self._definition.name,
            manufacturer=MANUFACTURER,
            model=self._definition.model,
            configuration_url=ADD_CAMERA_URL,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose diagnostic attributes."""
        attributes: dict[str, Any] = {
            ATTR_CAMERA_ID: self._definition.id,
            ATTR_RTSP_TRANSPORT: self._definition.rtsp_transport,
            ATTR_SOURCE_FILE: str(self.coordinator.cameras_file),
        }
        if self._definition.codec:
            attributes[ATTR_CODEC] = self._definition.codec
        if self._definition.stream_url:
            attributes[ATTR_STREAM_URL] = redact_url(self._definition.stream_url)
        ptz = self._definition.ptz
        if ptz is not None:
            attributes[ATTR_PTZ] = True
            attributes[ATTR_PTZ_PRESETS] = [name for _, name in ptz.presets]
        return attributes

    @property
    def entity_picture(self) -> str | None:
        """Return an optional snapshot URL for the entity picture."""
        return self._definition.snapshot_url

    async def stream_source(self) -> str | None:
        """Return the stream URL handed over to the Home Assistant stream component.

        Home Assistant 2026.9 calls this as an awaited method (it used to be a
        property), so it has to stay a coroutine - otherwise adding the entity
        fails with ``TypeError: 'str' object is not callable`` as soon as a WebRTC
        provider inspects the camera.
        """
        return self._definition.stream_source

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image for the camera card and ``camera.snapshot``.

        Home Assistant grabs stills from the live stream with
        ``Stream.async_get_image`` - but its ``use_stream_for_stills`` flag is a
        plain property (``_attr_use_stream_for_stills`` does not exist in 2026.9),
        and the first call right after a stream start often has no keyframe yet and
        returns ``None`` (Home Assistant then reports "Unable to get image").
        Therefore the still is taken here: a keyframe is awaited with a bound
        timeout, then whatever frame is available is used. A snapshot URL known to
        the add-on has priority.
        """
        if self._definition.snapshot_url:
            image = await self._async_fetch_snapshot(self._definition.snapshot_url)
            if image is not None:
                return image

        stream = await self.async_create_stream()
        if stream is None:
            return None

        try:
            async with asyncio.timeout(KEYFRAME_WAIT_SECONDS):
                image = await stream.async_get_image(
                    width=width, height=height, wait_for_next_keyframe=True
                )
        except TimeoutError:
            _LOGGER.debug("No keyframe for %s within %s s", self.entity_id, KEYFRAME_WAIT_SECONDS)
            image = None

        if image is None:
            image = await stream.async_get_image(width=width, height=height)
        return image

    async def _async_fetch_snapshot(self, url: str) -> bytes | None:
        """Download an image from the optional snapshot URL of the camera."""
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(SNAPSHOT_TIMEOUT_SECONDS):
                async with session.get(url) as response:
                    if response.status != HTTPStatus.OK:
                        _LOGGER.warning(
                            "Snapshot of %s returned HTTP %s", self.entity_id, response.status
                        )
                        return None
                    return await response.read()
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Snapshot of %s failed: %s", self.entity_id, err)
            return None

    # ------------------------------------------------------------------- PTZ
    @property
    def ptz(self) -> PtzConfig | None:
        """Return the PTZ configuration of this camera."""
        return self._definition.ptz

    async def async_ptz(self, **kwargs: Any) -> None:
        """Handle the ``rtsp_cameras.ptz`` service (ONVIF compatible fields).

        Home Assistant sends ``pan``/``tilt``/``zoom``/``speed`` (0..1),
        ``continuous_duration`` (seconds), ``preset`` and ``move_mode``; the plain
        ``action`` of the add-on is accepted as a shortcut.
        """
        config = self._definition.ptz
        action = resolve_action(kwargs)
        speed = resolve_speed(kwargs)
        duration = resolve_duration(kwargs)

        if action == "preset":
            preset = str(kwargs.get("preset") or "").strip()
            if not preset:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="ptz_preset_missing"
                )
            await ptz_async_execute(self.hass, config, "preset", speed=speed, preset=preset)
            return

        if action in PTZ_DIRECTIONS:
            await ptz_async_move(
                self.hass,
                config,  # type: ignore[arg-type]
                action,
                speed=speed,
                duration=duration if duration is not None else default_duration(),
            )
            return

        await ptz_async_execute(self.hass, config, action, speed=speed)

    async def async_ptz_home(self, **kwargs: Any) -> None:
        """Handle the ``rtsp_cameras.ptz_home`` service."""
        await ptz_async_execute(
            self.hass, self._definition.ptz, "home", speed=resolve_speed(kwargs)
        )
