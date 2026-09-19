"""Support for Denon AVR number entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import EntityCategory, UnitOfSoundPressure
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .entity import DenonAvrPendingValueEntity

# Denon receivers do not handle concurrent requests reliably. Only
# covers multi-entity calls - entity.py's shared lock covers the rest.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrNumberEntityDescription(NumberEntityDescription):
    """Describes a Denon AVR number entity."""

    value_fn: Callable[[DenonAVR], float | None]
    set_fn: Callable[[DenonAVR, float], Coroutine[Any, Any, None]]
    # Whether the setting can currently be changed - see the matching
    # field on DenonAvrSelectEntityDescription.
    available_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # For an upper bound the receiver reports itself. None falls back to
    # native_max_value, for a receiver that reports none.
    max_value_fn: Callable[[DenonAVR], float | None] | None = None
    # AppCommand0300 values need the coordinator whose poll is conditional
    # on "Update audio settings periodically"; everything else reads with
    # the status one.
    uses_settings_coordinator: bool = False


NUMBER_TYPES: tuple[DenonAvrNumberEntityDescription, ...] = ()

# denonavr names the subwoofers "Subwoofer", "Subwoofer 2" and so on, while
# the entity wants the plain number. Which ones exist is read from the
# receiver, never from this map.
SUBWOOFER_NUMBERS: dict[str, int] = {
    "Subwoofer": 1,
    "Subwoofer 2": 2,
    "Subwoofer 3": 3,
    "Subwoofer 4": 4,
}


def _nearest_half_decibel(value: float) -> float:
    """Round to the half decibel the receiver's level scale is built on."""
    return round(value * 2) / 2


def _subwoofer_level_description(subwoofer: str) -> DenonAvrNumberEntityDescription:
    """Describe the level entity for one of the receiver's subwoofers."""
    number = SUBWOOFER_NUMBERS[subwoofer]
    return DenonAvrNumberEntityDescription(
        key=f"subwoofer_level_{number}",
        translation_key="subwoofer_level",
        translation_placeholders={"subwoofer": str(number)},
        native_unit_of_measurement=UnitOfSoundPressure.DECIBEL,
        native_min_value=-12,
        native_max_value=12,
        native_step=0.5,
        entity_category=EntityCategory.CONFIG,
        # subwoofer_level(), never subwoofer_levels[...]: the dict is None
        # while the level-adjust menu is off and loses the key of a subwoofer
        # that stops being reported, neither of which the gate below covers.
        value_fn=lambda receiver: receiver.subwoofer_level(subwoofer),
        set_fn=lambda receiver, value: receiver.async_set_subwoofer_level(
            subwoofer, _nearest_half_decibel(value)
        ),
        # is not False, not truthiness: None is a model that never reports
        # the flag, which is no reason to disable the entity.
        available_fn=lambda receiver: receiver.subwoofer_level_status is not False,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR number entities from a config entry."""
    data = config_entry.runtime_data
    known_subwoofers: set[str] = set()

    @callback
    def add_reported_subwoofer_levels() -> None:
        """Add a level entity for each subwoofer the receiver newly reports.

        The readable set is gated on a signal being present and on
        subwoofer output being on, so it can't be built at setup and it
        moves between refreshes. Entities are never removed: a
        subwoofer that drops out of the response reads unknown,
        which is easier to make sense of than one that disappears.
        """
        reported = data.receiver.subwoofer_levels or {}
        new_subwoofers = [
            subwoofer
            for subwoofer in reported
            if subwoofer in SUBWOOFER_NUMBERS and subwoofer not in known_subwoofers
        ]
        if not new_subwoofers:
            return
        known_subwoofers.update(new_subwoofers)
        async_add_entities(
            DenonAvrNumber(config_entry, _subwoofer_level_description(subwoofer))
            for subwoofer in new_subwoofers
        )

    async_add_entities(
        DenonAvrNumber(config_entry, description) for description in NUMBER_TYPES
    )
    add_reported_subwoofer_levels()
    config_entry.async_on_unload(
        data.coordinator.async_add_listener(add_reported_subwoofer_levels)
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
            data.settings_coordinator
            if description.uses_settings_coordinator
            else data.coordinator,
            config_entry,
            description.key,
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
        if not super().available:
            return False
        return self.entity_description.available_fn(self._receiver)

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current value."""
        return self._current_value

    @property
    @override
    def native_max_value(self) -> float:
        """Return the receiver-reported upper bound, if it reports one."""
        if (max_value_fn := self.entity_description.max_value_fn) is None:
            return super().native_max_value
        if (max_value := max_value_fn(self._receiver)) is None:
            return super().native_max_value
        return max_value

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Set a new value."""
        await self._async_apply_change(
            send=lambda: self.entity_description.set_fn(self._receiver, value),
            value=value,
        )
