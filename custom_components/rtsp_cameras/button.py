"""Buttons for the RTSP Camera Manager integration.

Every PTZ preset published by the add-on becomes a button (handy on dashboards and
for automations) plus one button that stops the camera.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ADD_CAMERA_URL, DOMAIN, MANUFACTURER
from .coordinator import RtspCamerasCoordinator
from .models import RtspCameraDefinition
from .ptz import async_execute

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PTZ buttons for a config entry."""
    coordinator: RtspCamerasCoordinator = entry.runtime_data
    manager = PtzButtonManager(hass, coordinator, async_add_entities)
    manager.start()
    entry.async_on_unload(manager.async_stop)


class PtzButtonManager:
    """Keep the PTZ buttons in sync with the file published by the add-on."""

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
        self._entities: dict[str, PtzButton] = {}
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        """Register for coordinator updates and create the initial buttons."""
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
        """Add new and remove vanished buttons."""
        wanted = _wanted_buttons(self.coordinator.data or {})

        for key in list(self._entities):
            if key in wanted:
                continue
            entity = self._entities.pop(key)
            self.hass.async_create_task(entity.async_remove(force_remove=True))

        new_entities: list[PtzButton] = []
        for key, (definition, preset_id, preset_name) in wanted.items():
            entity = self._entities.get(key)
            if entity is None:
                entity = PtzButton(self.coordinator, definition, preset_id, preset_name)
                self._entities[key] = entity
                new_entities.append(entity)
            else:
                entity.async_update_definition(definition)

        if new_entities:
            _LOGGER.info(
                "Registered %d PTZ button(s) for %s",
                len(new_entities),
                ", ".join(sorted({entity.definition.name for entity in new_entities})),
            )
            self._async_add_entities(new_entities, update_before_add=True)


def _wanted_buttons(
    cameras: dict[str, RtspCameraDefinition],
) -> dict[str, tuple[RtspCameraDefinition, str | None, str]]:
    """Return the buttons that should exist, keyed by their unique id."""
    wanted: dict[str, tuple[RtspCameraDefinition, str | None, str]] = {}
    for definition in cameras.values():
        ptz = definition.ptz
        if ptz is None or not ptz.enabled:
            continue
        if ptz.uses("stop"):
            wanted[f"{definition.id}_stop"] = (definition, None, "Stop")
        for preset_id, preset_name in ptz.presets:
            wanted[f"{definition.id}_preset_{preset_id}"] = (definition, preset_id, preset_name)
    return wanted


class PtzButton(CoordinatorEntity[RtspCamerasCoordinator], ButtonEntity):
    """A PTZ preset button or the stop button of a camera."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RtspCamerasCoordinator,
        definition: RtspCameraDefinition,
        preset_id: str | None,
        preset_name: str,
    ) -> None:
        """Initialise the button.

        ``CoordinatorEntity.__init__`` has to be called explicitly (it does not take
        part in the cooperative initialisation chain), so both base classes are set
        up here.
        """
        CoordinatorEntity.__init__(self, coordinator)
        ButtonEntity.__init__(self)
        self._definition = definition
        self._preset_id = preset_id
        self._attr_name = f"PTZ {preset_name}" if preset_id else "PTZ stop"

    @callback
    def async_update_definition(self, definition: RtspCameraDefinition) -> None:
        """Apply a changed camera definition."""
        if definition == self._definition:
            return
        self._definition = definition
        self.async_write_ha_state()

    @property
    def definition(self) -> RtspCameraDefinition:
        """Return the camera definition currently in use."""
        return self._definition

    @property
    def unique_id(self) -> str:
        """Return the unique id of this button."""
        suffix = f"ptz_{self._preset_id}" if self._preset_id else "ptz_stop"
        return f"{DOMAIN}_{self._definition.id}_{suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the button to the camera device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._definition.id)},
            name=self._definition.name,
            manufacturer=MANUFACTURER,
            model=self._definition.model,
            configuration_url=ADD_CAMERA_URL,
        )

    async def async_press(self) -> None:
        """Move the camera to its preset, or stop it."""
        config = self._definition.ptz
        if self._preset_id is not None:
            await async_execute(self.hass, config, "preset", preset=self._preset_id)
            return
        await async_execute(self.hass, config, "stop")
