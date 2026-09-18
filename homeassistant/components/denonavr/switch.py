"""Support for Denon AVR switch entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .entity import DenonAvrPendingValueEntity, audyssey_available

# Denon receivers do not handle concurrent requests reliably. Only
# covers multi-entity calls - entity.py's shared lock covers the rest.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrSwitchEntityDescription(SwitchEntityDescription):
    """Describes a Denon AVR switch entity."""

    is_on_fn: Callable[[DenonAVR], bool | None]
    set_fn: Callable[[DenonAVR, bool], Coroutine[Any, Any, None]]
    # Whether the setting can currently be changed.
    available_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # AppCommand0300 values need the coordinator whose poll is conditional
    # on "Update audio settings periodically"; everything else reads with
    # the status one.
    uses_settings_coordinator: bool = False
    # For a setting whose available_fn reads a value the other coordinator
    # holds: without Telnet, only that one's refresh sees it change.
    follows_other_coordinator: bool = False


SWITCH_TYPES: tuple[DenonAvrSwitchEntityDescription, ...] = (
    DenonAvrSwitchEntityDescription(
        key="dynamic_eq",
        translation_key="dynamic_eq",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda receiver: receiver.dynamic_eq,
        set_fn=lambda receiver, on: (
            receiver.async_dynamic_eq_on() if on else receiver.async_dynamic_eq_off()
        ),
        # MultEQ Off turns Dynamic EQ off and drops a command to turn it back
        # on. `!=` keeps it available while MultEQ is still unknown.
        available_fn=lambda receiver: (
            audyssey_available(receiver) and receiver.multi_eq != "Off"
        ),
        uses_settings_coordinator=True,
        follows_other_coordinator=True,
    ),
    DenonAvrSwitchEntityDescription(
        key="auto_lip_sync",
        translation_key="auto_lip_sync",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=lambda receiver: receiver.auto_lip_sync,
        # Not async_auto_lip_sync_toggle(): it inverts the last value read, so
        # it raises while that is unknown and repeats a change not yet read back.
        set_fn=lambda receiver, on: (
            receiver.async_auto_lip_sync_on()
            if on
            else receiver.async_auto_lip_sync_off()
        ),
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
    async_add_entities(
        DenonAvrSwitch(config_entry, description) for description in SWITCH_TYPES
    )


class DenonAvrSwitch(DenonAvrPendingValueEntity[bool], SwitchEntity):
    """Representation of a Denon AVR switch entity."""

    entity_description: DenonAvrSwitchEntityDescription

    def __init__(
        self,
        config_entry: DenonavrConfigEntry,
        description: DenonAvrSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        data = config_entry.runtime_data
        super().__init__(
            data.settings_coordinator
            if description.uses_settings_coordinator
            else data.coordinator,
            config_entry,
            description.key,
            follows_other_coordinator=description.follows_other_coordinator,
        )
        self.entity_description = description

    @override
    def _read_value(self) -> bool | None:
        """Return the receiver's own confirmed state."""
        return self.entity_description.is_on_fn(self._receiver)

    @override
    def _value_placeholder(self, value: bool) -> str:
        """Return the state the switch was being turned to."""
        return STATE_ON if value else STATE_OFF

    @property
    @override
    def available(self) -> bool:
        """Return whether the receiver reports a settable state.

        Also False if the coordinator's last refresh failed, so an
        unresponsive receiver doesn't keep showing stale data as current.
        """
        return (
            super().available
            and self._current_value is not None
            and self.entity_description.available_fn(self._receiver)
        )

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
        )
