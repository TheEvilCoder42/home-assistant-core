"""Support for Denon AVR number entities."""

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.const import EntityCategory, UnitOfSoundPressure
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .const import CONF_SERIAL_NUMBER, DOMAIN
from .coordinator import DenonAvrDataUpdateCoordinator
from .entity import DenonAvrPendingValueEntity

# See the matching constant in select.py.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrNumberEntityDescription(NumberEntityDescription):
    """Describes a Denon AVR number entity."""

    value_fn: Callable[[DenonAVR], float | None]
    set_fn: Callable[[DenonAVR, float], Coroutine[Any, Any, None]]
    # Whether the setting can currently be changed - see the matching
    # field on DenonAvrSelectEntityDescription.
    available_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # For settings whose upper bound the receiver reports itself, so it
    # can't be a static native_max_value. Returning None falls back to
    # the description's own bound, for receivers that don't report one.
    max_value_fn: Callable[[DenonAVR], float | None] | None = None
    # See the matching field on DenonAvrSelectEntityDescription.
    uses_settings_coordinator: bool = False


NUMBER_TYPES: tuple[DenonAvrNumberEntityDescription, ...] = ()

# denonavr names the subwoofers "Subwoofer", "Subwoofer 2" and so on.
# The entity key and the name want the plain number. Which of them a
# receiver actually has is read from its response, never from this map.
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
        # subwoofer_level(), never subwoofer_levels[...]: the dict is
        # None while the receiver's own level-adjust menu is off, and a
        # subwoofer it stops reporting drops out of the keys entirely,
        # neither of which the availability gate below covers.
        value_fn=lambda receiver: receiver.subwoofer_level(subwoofer),
        set_fn=lambda receiver, value: receiver.async_set_subwoofer_level(
            subwoofer, _nearest_half_decibel(value)
        ),
        # is not False, not truthiness: None means the receiver has
        # never reported the flag, which is what a model without the
        # command looks like and no reason to disable the entity.
        available_fn=lambda receiver: receiver.subwoofer_level_status is not False,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR number entities from a config entry."""
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
        description: DenonAvrNumberEntityDescription,
    ) -> DenonAvrDataUpdateCoordinator:
        return (
            data.settings_coordinator
            if description.uses_settings_coordinator
            else data.coordinator
        )

    @callback
    def add_numbers(
        descriptions: Iterable[DenonAvrNumberEntityDescription],
    ) -> None:
        """Add one entity per description.

        Also the entry point for descriptions that can only be built
        once the receiver has reported what it exposes, which a
        coordinator listener discovers after setup.
        """
        async_add_entities(
            DenonAvrNumber(
                _coordinator_for(description), description, unique_id_base, device_info
            )
            for description in descriptions
        )

    known_subwoofers: set[str] = set()

    @callback
    def add_reported_subwoofer_levels() -> None:
        """Add a level entity for each subwoofer the receiver newly reports.

        The readable set is gated on a signal being present and on
        subwoofer output being on, so it can't be built at setup and it
        moves between refreshes. Entities are never removed: a
        subwoofer that drops out of the response goes unavailable,
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
        add_numbers(
            _subwoofer_level_description(subwoofer) for subwoofer in new_subwoofers
        )

    add_numbers(NUMBER_TYPES)
    add_reported_subwoofer_levels()
    config_entry.async_on_unload(
        data.coordinator.async_add_listener(add_reported_subwoofer_levels)
    )


class DenonAvrNumber(DenonAvrPendingValueEntity[float], NumberEntity):
    """Representation of a Denon AVR number entity."""

    entity_description: DenonAvrNumberEntityDescription

    def __init__(
        self,
        coordinator: DenonAvrDataUpdateCoordinator,
        description: DenonAvrNumberEntityDescription,
        unique_id_base: str,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(
            coordinator, f"{unique_id_base}-{description.key}", device_info
        )
        self.entity_description = description

    @override
    def _read_value(self) -> float | None:
        """Return the receiver's own confirmed value."""
        return self.entity_description.value_fn(self._receiver)

    @property
    @override
    def available(self) -> bool:
        """Return whether the setting can currently be changed.

        Unlike DenonAvrSelect, a missing value doesn't make the entity
        unavailable: a settable number the receiver hasn't reported yet
        is `unknown`, not gone.
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
            error_label=self.entity_description.key,
        )
