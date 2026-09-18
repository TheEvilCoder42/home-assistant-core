"""Support for Denon AVR switch entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .const import CONF_SERIAL_NUMBER, DOMAIN
from .coordinator import DenonAvrDataUpdateCoordinator
from .entity import DenonAvrPendingValueEntity

# See the matching constant in select.py.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrSwitchEntityDescription(SwitchEntityDescription):
    """Describes a Denon AVR switch entity."""

    is_on_fn: Callable[[DenonAVR], bool | None]
    set_fn: Callable[[DenonAVR, bool], Coroutine[Any, Any, None]]
    # What a failed command calls this setting to the user. Unlike
    # DenonAvrSelect's fallback there's no description.name to fall back
    # to, since new descriptions carry a translation_key alone.
    error_label: str
    # See the matching field on DenonAvrSelectEntityDescription.
    uses_settings_coordinator: bool = False


SWITCH_TYPES: tuple[DenonAvrSwitchEntityDescription, ...] = (
    DenonAvrSwitchEntityDescription(
        key="dynamic_eq",
        translation_key="dynamic_eq",
        # Provide a fallback name if translations are unavailable.
        name="Dynamic EQ",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda receiver: receiver.dynamic_eq,
        # Reference Level Offset can only be set while Dynamic EQ is on,
        # so flipping this switch changes that entity's availability too
        # once the shared coordinator refresh completes.
        set_fn=lambda receiver, on: (
            receiver.async_dynamic_eq_on() if on else receiver.async_dynamic_eq_off()
        ),
        error_label="Dynamic EQ",
        uses_settings_coordinator=True,
    ),
    DenonAvrSwitchEntityDescription(
        key="auto_lip_sync",
        translation_key="auto_lip_sync",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda receiver: receiver.auto_lip_sync,
        # Not async_auto_lip_sync_toggle(): that decides on a state only
        # the Telnet callback ever writes, so on an HTTP-only receiver
        # it always turns the setting on.
        set_fn=lambda receiver, on: (
            receiver.async_auto_lip_sync_on()
            if on
            else receiver.async_auto_lip_sync_off()
        ),
        error_label="Auto lip sync",
        # Without Telnet the value comes from GetAudioDelay, which only
        # this coordinator's request fetches.
        uses_settings_coordinator=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR switch entities from a config entry."""
    data = config_entry.runtime_data

    if (
        config_entry.data.get(CONF_SERIAL_NUMBER) is not None
        and config_entry.unique_id is not None
    ):
        unique_id_base = config_entry.unique_id
    else:
        unique_id_base = config_entry.entry_id

    device_info = DeviceInfo(
        identifiers={(DOMAIN, config_entry.unique_id or config_entry.entry_id)},
    )

    def _coordinator_for(
        description: DenonAvrSwitchEntityDescription,
    ) -> DenonAvrDataUpdateCoordinator:
        return (
            data.settings_coordinator
            if description.uses_settings_coordinator
            else data.coordinator
        )

    async_add_entities(
        DenonAvrSwitch(
            _coordinator_for(description), description, unique_id_base, device_info
        )
        for description in SWITCH_TYPES
    )


class DenonAvrSwitch(DenonAvrPendingValueEntity[bool], SwitchEntity):
    """Representation of a Denon AVR switch entity."""

    entity_description: DenonAvrSwitchEntityDescription

    def __init__(
        self,
        coordinator: DenonAvrDataUpdateCoordinator,
        description: DenonAvrSwitchEntityDescription,
        unique_id_base: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the switch."""
        super().__init__(
            coordinator, f"{unique_id_base}-{description.key}", device_info
        )
        self.entity_description = description

    @override
    def _read_value(self) -> bool | None:
        """Return the receiver's own confirmed state."""
        return self.entity_description.is_on_fn(self._receiver)

    @property
    @override
    def available(self) -> bool:
        """Return whether the receiver reports a state for this setting.

        Also False if the coordinator's last refresh failed - see
        DenonAvrSelect.available.
        """
        return super().available and self._current_value is not None

    @property
    @override
    def is_on(self) -> bool | None:
        """Return True if the setting is on."""
        return self._current_value

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        """Turn the setting on or off."""
        await self._async_apply_change(
            send=lambda: self.entity_description.set_fn(self._receiver, on),
            value=on,
            error_label=self.entity_description.error_label,
        )
