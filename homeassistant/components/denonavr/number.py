"""Support for Denon AVR number entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import EntityCategory, UnitOfSoundPressure
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .entity import DenonAvrPendingValueEntity, tone_control_available

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
    # See the matching field on DenonAvrSwitchEntityDescription.
    follows_other_coordinator: bool = False
    # Whether the receiver has this setting at all; checked once at setup.
    supported_fn: Callable[[DenonAVR], bool] = lambda receiver: True


# denonavr reports bass and treble on the receiver's raw 0..12 scale,
# which sits 6 above the dB value the receiver itself displays.
TONE_CONTROL_DB_OFFSET = 6


def _tone_control_db(value: int | None) -> float | None:
    """Convert a raw tone control value to the dB the receiver shows."""
    if value is None:
        return None
    return value - TONE_CONTROL_DB_OFFSET


NUMBER_TYPES: tuple[DenonAvrNumberEntityDescription, ...] = (
    DenonAvrNumberEntityDescription(
        key="bass",
        translation_key="bass",
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        native_min_value=-6,
        native_max_value=6,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        # None until denonavr reads a value: the receiver can answer
        # GetToneControl blank for long stretches, from setup on.
        value_fn=lambda receiver: _tone_control_db(receiver.bass),
        set_fn=lambda receiver, value: receiver.async_set_bass(
            int(value) + TONE_CONTROL_DB_OFFSET
        ),
        available_fn=tone_control_available,
        follows_other_coordinator=True,
        supported_fn=lambda receiver: bool(receiver.support_tone_control),
    ),
    DenonAvrNumberEntityDescription(
        key="treble",
        translation_key="treble",
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        native_min_value=-6,
        native_max_value=6,
        native_step=1,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda receiver: _tone_control_db(receiver.treble),
        set_fn=lambda receiver, value: receiver.async_set_treble(
            int(value) + TONE_CONTROL_DB_OFFSET
        ),
        available_fn=tone_control_available,
        follows_other_coordinator=True,
        supported_fn=lambda receiver: bool(receiver.support_tone_control),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR number entities from a config entry."""
    receiver = config_entry.runtime_data.receiver
    async_add_entities(
        DenonAvrNumber(config_entry, description)
        for description in NUMBER_TYPES
        if description.supported_fn(receiver)
    )


class DenonAvrNumber(DenonAvrPendingValueEntity[float], NumberEntity):
    """Representation of a Denon AVR number entity."""

    entity_description: DenonAvrNumberEntityDescription

    def __init__(
        self,
        config_entry: DenonavrConfigEntry,
        description: DenonAvrNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        data = config_entry.runtime_data
        super().__init__(
            data.coordinator,
            config_entry,
            description.key,
            follows_other_coordinator=description.follows_other_coordinator,
        )
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
