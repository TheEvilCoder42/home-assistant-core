"""Support for Denon AVR number entities."""

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR

from homeassistant.components.number import NumberEntity, NumberEntityDescription
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

    add_numbers(NUMBER_TYPES)


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
