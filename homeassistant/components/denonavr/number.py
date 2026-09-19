"""Support for Denon AVR number entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR
from denonavr.const import VOLUME_TELNET_HALF_STEP

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import UnitOfSoundPressure
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .const import VOLUME_MIN, ZONE_NUMBERS
from .entity import DenonAvrPendingValueEntity

# Denon receivers do not handle concurrent requests reliably. Only
# covers multi-entity calls - entity.py's shared lock covers the rest.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrNumberEntityDescription(NumberEntityDescription):
    """Describes a Denon AVR number entity."""

    value_fn: Callable[[DenonAVR], float | None]
    set_fn: Callable[[DenonAVR, float], Coroutine[Any, Any, None]]
    # Whether the setting can currently be changed.
    available_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # For an upper bound the receiver reports itself, instead of
    # native_max_value.
    max_value_fn: Callable[[DenonAVR], float] | None = None


NUMBER_TYPES: tuple[DenonAvrNumberEntityDescription, ...] = ()


def _volume_description(zone: str) -> DenonAvrNumberEntityDescription:
    """Describe the volume entity for one of the receiver's zones.

    On the receiver's own scale, unlike media_player's 0..1 one, and capped
    at the configured limit so the top of the range is always accepted.
    """
    zone_number = ZONE_NUMBERS.get(zone)
    # Half steps on the main zone only: the secondary zones move in whole
    # decibels on either transport.
    step = 0.5 if VOLUME_TELNET_HALF_STEP[zone] else 1.0
    return DenonAvrNumberEntityDescription(
        key=f"{zone}-volume",
        translation_key="volume" if zone_number is None else "zone_volume",
        translation_placeholders=None if zone_number is None else {"zone": zone_number},
        entity_registry_enabled_default=False,
        # No device class: SOUND_PRESSURE is a measured level and this is a
        # relative setting, the same reading lyngdorf's trims take.
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        native_min_value=VOLUME_MIN,
        native_step=step,
        value_fn=lambda receiver: receiver.volume,
        set_fn=lambda receiver, value: receiver.async_set_volume(value),
        max_value_fn=lambda receiver: receiver.max_volume,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR number entities from a config entry."""
    async_add_entities(
        DenonAvrNumber(config_entry, description) for description in NUMBER_TYPES
    )
    async_add_entities(
        DenonAvrNumber(config_entry, _volume_description(zone), zone_receiver)
        for zone, zone_receiver in config_entry.runtime_data.receiver.zones.items()
    )


class DenonAvrNumber(DenonAvrPendingValueEntity[float], NumberEntity):
    """Representation of a Denon AVR number entity."""

    entity_description: DenonAvrNumberEntityDescription

    def __init__(
        self,
        config_entry: DenonavrConfigEntry,
        description: DenonAvrNumberEntityDescription,
        receiver: DenonAVR | None = None,
    ) -> None:
        """Initialize the number entity."""
        data = config_entry.runtime_data
        super().__init__(data.coordinator, config_entry, description.key, receiver)
        self.entity_description = description

    @override
    def _read_value(self) -> float | None:
        """Return the receiver's own confirmed value."""
        return self.entity_description.value_fn(self._receiver)

    @override
    def _value_placeholder(self, value: float) -> str:
        """Return the value as the state shows it, in its display unit."""
        state_value = self._convert_to_state_value(value, round, self.device_class)
        if (unit := self.unit_of_measurement) is None:
            return str(state_value)
        return f"{state_value} {unit}"

    @property
    @override
    def available(self) -> bool:
        """Return whether the setting can currently be changed.

        Unlike DenonAvrSelect, a missing value leaves the entity `unknown`
        rather than unavailable, since it is still settable.
        """
        return super().available and self.entity_description.available_fn(
            self._receiver
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current value."""
        return self._current_value

    @property
    @override
    def native_max_value(self) -> float:
        """Return the receiver-reported upper bound, if the description reads one."""
        if (max_value_fn := self.entity_description.max_value_fn) is None:
            return super().native_max_value
        return max_value_fn(self._receiver)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set a new value."""
        # The action enforces only min and max, and the pending value must
        # equal what the receiver reports back, or it holds until expiry.
        if (step := self.native_step) is not None:
            value = round(value / step) * step
        await self._async_apply_change(
            send=lambda: self.entity_description.set_fn(self._receiver, value),
            value=value,
        )
