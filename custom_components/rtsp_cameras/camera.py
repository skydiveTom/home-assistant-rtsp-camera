"""Camera platform for the RTSP Camera Manager integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CAMERA_ID,
    ATTR_RTSP_TRANSPORT,
    ATTR_SOURCE_FILE,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import RtspCamerasCoordinator
from .models import RtspCameraDefinition

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
            _LOGGER.debug("Camera %s disappeared from the add-on, removing entity", camera_id)
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
            _LOGGER.debug("Registering %d camera entities", len(new_entities))
            self._async_add_entities(new_entities, update_before_add=True)


class RtspCamera(CoordinatorEntity[RtspCamerasCoordinator], Camera):
    """A camera entity backed by a plain RTSP/RTMP/HTTP stream URL."""

    _attr_has_entity_name = False
    _attr_use_stream_for_stills = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: RtspCamerasCoordinator,
        definition: RtspCameraDefinition,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._definition = definition
        self._attr_name = definition.name
        self._attr_unique_id = f"{DOMAIN}_{definition.id}"

    @callback
    def async_update_definition(self, definition: RtspCameraDefinition) -> None:
        """Apply a changed camera definition."""
        if definition == self._definition:
            return
        self._definition = definition
        self._attr_name = definition.name
        self.async_write_ha_state()

    @property
    def definition(self) -> RtspCameraDefinition:
        """Return the camera definition currently in use."""
        return self._definition

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information for the camera."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._definition.id)},
            name=self._definition.name,
            manufacturer=MANUFACTURER,
            model=self._definition.model,
            configuration_url=(
                "https://github.com/skydiveTom/home-assistant-rtsp-camera"
            ),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose diagnostic attributes."""
        return {
            ATTR_CAMERA_ID: self._definition.id,
            ATTR_RTSP_TRANSPORT: self._definition.rtsp_transport,
            ATTR_SOURCE_FILE: str(self.coordinator.cameras_file),
        }

    @property
    def entity_picture(self) -> str | None:
        """Return an optional snapshot URL for the entity picture."""
        return self._definition.snapshot_url

    @property
    def stream_source(self) -> str | None:
        """Return the stream URL handed over to the Home Assistant stream component."""
        return self._definition.url

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Let Home Assistant generate still images from the live stream."""
        return None