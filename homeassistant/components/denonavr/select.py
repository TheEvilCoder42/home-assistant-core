"""Support for Denon AVR select entities."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, override

from denonavr import DenonAVR
from denonavr.const import MAX_VOLUME_MAX, MAX_VOLUME_MIN, MAX_VOLUME_STEP

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DenonavrConfigEntry
from .const import ZONE_NAMES
from .entity import DenonAvrPendingValueEntity, audyssey_available

# Denon receivers do not handle concurrent requests reliably. Only
# covers multi-entity calls - entity.py's shared lock covers the rest.
PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class DenonAvrSelectEntityDescription(SelectEntityDescription):
    """Describes a Denon AVR select entity."""

    # Option key -> the value denonavr reads and sets for it.
    values: dict[str, str]
    current_value_fn: Callable[[DenonAVR], str | None]
    select_value_fn: Callable[[DenonAVR, str], Coroutine[Any, Any, None]]
    # The values the receiver accepts, where that depends on the connection.
    settable_values_fn: Callable[[DenonAVR], list[str]] | None = None
    # Whether the setting can currently be changed (e.g. Reference Level
    # Offset requires Dynamic EQ to be on).
    available_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # Whether the receiver has reported the setting yet. Until it has, the
    # entity reads unknown rather than unavailable, since it is settable.
    reported_fn: Callable[[DenonAVR], bool] = lambda receiver: True
    # AppCommand0300 values need the coordinator whose poll is conditional
    # on "Update audio settings periodically"; everything else reads with
    # the status one.
    uses_settings_coordinator: bool = False
    # For a setting whose available_fn reads a value the other coordinator
    # holds: without Telnet, only that one's refresh sees it change.
    follows_other_coordinator: bool = False


SELECT_TYPES: tuple[DenonAvrSelectEntityDescription, ...] = (
    DenonAvrSelectEntityDescription(
        key="reference_level_offset",
        translation_key="reference_level_offset",
        entity_category=EntityCategory.CONFIG,
        values={"0db": "0dB", "5db": "+5dB", "10db": "+10dB", "15db": "+15dB"},
        current_value_fn=lambda receiver: receiver.reference_level_offset,
        select_value_fn=lambda receiver, value: receiver.async_set_reflevoffset(value),
        available_fn=lambda receiver: (
            audyssey_available(receiver) and bool(receiver.dynamic_eq)
        ),
        uses_settings_coordinator=True,
        follows_other_coordinator=True,
    ),
    DenonAvrSelectEntityDescription(
        key="dynamic_volume",
        translation_key="dynamic_volume",
        entity_category=EntityCategory.CONFIG,
        values={"off": "Off", "light": "Light", "medium": "Medium", "heavy": "Heavy"},
        current_value_fn=lambda receiver: receiver.dynamic_volume,
        select_value_fn=lambda receiver, value: receiver.async_set_dynamicvol(value),
        # MultEQ Off forces Dynamic Volume off, and a change there silently
        # turns MultEQ back on.
        available_fn=lambda receiver: (
            audyssey_available(receiver) and receiver.multi_eq != "Off"
        ),
        uses_settings_coordinator=True,
        follows_other_coordinator=True,
    ),
    DenonAvrSelectEntityDescription(
        key="multi_eq",
        translation_key="multi_eq",
        entity_category=EntityCategory.CONFIG,
        values={
            "off": "Off",
            "flat": "Flat",
            "l_r_bypass": "L/R Bypass",
            "reference": "Reference",
            "manual": "Manual",
        },
        current_value_fn=lambda receiver: receiver.multi_eq,
        select_value_fn=lambda receiver, value: receiver.async_set_multieq(value),
        # Manual is settable over Telnet only.
        settable_values_fn=lambda receiver: receiver.multi_eq_setting_list,
        available_fn=audyssey_available,
        uses_settings_coordinator=True,
        follows_other_coordinator=True,
    ),
    DenonAvrSelectEntityDescription(
        key="eco_mode",
        translation_key="eco_mode",
        entity_category=EntityCategory.CONFIG,
        values={"on": "On", "auto": "Auto", "off": "Off"},
        current_value_fn=lambda receiver: receiver.eco_mode,
        select_value_fn=lambda receiver, value: receiver.async_eco_mode(value),
    ),
    DenonAvrSelectEntityDescription(
        key="dimmer",
        translation_key="dimmer",
        entity_category=EntityCategory.CONFIG,
        values={"off": "Off", "dark": "Dark", "dim": "Dim", "bright": "Bright"},
        current_value_fn=lambda receiver: receiver.dimmer,
        select_value_fn=lambda receiver, value: receiver.async_dimmer(value),
    ),
    DenonAvrSelectEntityDescription(
        key="auto_standby",
        translation_key="auto_standby",
        entity_category=EntityCategory.CONFIG,
        values={
            "off": "OFF",
            "15m": "15M",
            "30m": "30M",
            "60m": "60M",
            "2h": "2H",
            "4h": "4H",
            "8h": "8H",
        },
        current_value_fn=lambda receiver: receiver.auto_standby,
        select_value_fn=lambda receiver, value: receiver.async_auto_standby(value),
    ),
)

# The limit can be off, and 0.0 dB is itself a valid limit, so this is a
# select rather than a number.
VOLUME_LIMIT_OFF = "OFF"


def _volume_limit_values(zone: str) -> dict[str, str]:
    """Map an option key to each limit a zone accepts, lowest first.

    The main zone takes every whole decibel, the secondary zones only
    multiples of ten. A translation key cannot start with "-".
    """
    step = MAX_VOLUME_STEP[zone]
    limits = (
        MAX_VOLUME_MIN + index * step
        for index in range(round((MAX_VOLUME_MAX - MAX_VOLUME_MIN) / step) + 1)
    )
    return {"off": VOLUME_LIMIT_OFF} | {
        f"minus_{-limit:g}db" if limit < 0 else f"{limit:g}db": f"{limit:g}"
        for limit in limits
    }


def _current_volume_limit(receiver: DenonAVR) -> str | None:
    """Return the configured limit, OFF when there is none."""
    if not receiver.max_volume_known:
        return None
    # The library reads no limit as 18.0, the hardware maximum.
    if (max_volume := receiver.max_volume) >= 18.0:
        return VOLUME_LIMIT_OFF
    return f"{max_volume:g}"


def _volume_limit_description(zone: str) -> DenonAvrSelectEntityDescription:
    """Describe the volume limit entity for one of the receiver's zones."""
    zone_name = ZONE_NAMES.get(zone)
    return DenonAvrSelectEntityDescription(
        key=f"{zone}-volume_limit",
        translation_key="volume_limit" if zone_name is None else "zone_volume_limit",
        translation_placeholders=None if zone_name is None else {"zone": zone_name},
        entity_category=EntityCategory.CONFIG,
        values=_volume_limit_values(zone),
        current_value_fn=_current_volume_limit,
        # Off the AppCommand API, only a Telnet push on a change reports one.
        reported_fn=lambda receiver: receiver.max_volume_known,
        select_value_fn=lambda receiver, value: receiver.async_set_max_volume(
            None if value == VOLUME_LIMIT_OFF else float(value)
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: DenonavrConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the DenonAVR select entities from a config entry."""
    async_add_entities(
        DenonAvrSelect(config_entry, description) for description in SELECT_TYPES
    )
    async_add_entities(
        DenonAvrSelect(config_entry, _volume_limit_description(zone), zone_receiver)
        for zone, zone_receiver in config_entry.runtime_data.receiver.zones.items()
    )


class DenonAvrSelect(DenonAvrPendingValueEntity[str], SelectEntity):
    """Representation of a Denon AVR select entity."""

    entity_description: DenonAvrSelectEntityDescription

    def __init__(
        self,
        config_entry: DenonavrConfigEntry,
        description: DenonAvrSelectEntityDescription,
        receiver: DenonAVR | None = None,
    ) -> None:
        """Initialize the select entity."""
        data = config_entry.runtime_data
        super().__init__(
            data.settings_coordinator
            if description.uses_settings_coordinator
            else data.coordinator,
            config_entry,
            description.key,
            receiver,
            follows_other_coordinator=description.follows_other_coordinator,
        )
        self.entity_description = description
        self._options_by_value = {
            value: option for option, value in description.values.items()
        }

    @override
    def _read_value(self) -> str | None:
        """Return the option the receiver reports, None for an unknown value."""
        value = self.entity_description.current_value_fn(self._receiver)
        if value is None:
            return None
        return self._options_by_value.get(value)

    @property
    @override
    def available(self) -> bool:
        """Return whether the receiver reports a known, currently settable value.

        Also False if the coordinator's last refresh failed, so an
        unresponsive receiver doesn't keep showing stale data as current.
        """
        if not super().available:
            return False
        if self._current_value is None and self.entity_description.reported_fn(
            self._receiver
        ):
            return False
        return self.entity_description.available_fn(self._receiver)

    @property
    @override
    def current_option(self) -> str | None:
        """Return the current selected option."""
        return self._current_value

    @property
    @override
    def options(self) -> list[str]:
        """Return the options the receiver accepts."""
        values = self.entity_description.values
        if (settable_values_fn := self.entity_description.settable_values_fn) is None:
            return list(values)
        settable = settable_values_fn(self._receiver)
        return [option for option, value in values.items() if value in settable]

    @override
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        value = self.entity_description.values[option]
        await self._async_apply_change(
            send=lambda: self.entity_description.select_value_fn(self._receiver, value),
            value=option,
        )
