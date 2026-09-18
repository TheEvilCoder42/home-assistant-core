"""Shared entity base and availability helpers for Denon AVR entities.

A command's value is shown optimistically, since the receiver can briefly
still report the old one, and reconciled once it catches up. A timeout keeps
a command that never applied from masking reality forever.
"""

from collections.abc import Callable, Coroutine
from typing import Any, override

from denonavr import DenonAVR
from denonavr.exceptions import DenonAvrError

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DenonavrConfigEntry
from .const import CONF_SERIAL_NUMBER, DOMAIN, PENDING_VALUE_TIMEOUT
from .coordinator import (
    COMMAND_UNAVAILABLE_ON,
    DenonAvrDataUpdateCoordinator,
    mark_unavailable,
)

_DIRECT_SOUND_MODES = ("DIRECT", "PURE DIRECT")


def receiver_unique_id(config_entry: DenonavrConfigEntry, key: str) -> str:
    """Return the unique_id of the receiver-level entity with this key."""
    if (
        config_entry.data.get(CONF_SERIAL_NUMBER) is not None
        and config_entry.unique_id is not None
    ):
        return f"{config_entry.unique_id}-{key}"
    return f"{config_entry.entry_id}-{key}"


def error_message(err: DenonAvrError) -> str:
    """Return the error's own message, without the rest of its args.

    AvrCommandError and AvrProcessingError hand every positional argument
    to Exception, so str() on them renders the whole args tuple.
    """
    return str(err.args[0]) if err.args else str(err)


def audyssey_available(receiver: DenonAVR) -> bool:
    """Return whether the receiver takes Audyssey commands in its sound mode.

    Direct bypasses Audyssey: the receiver drops every Audyssey command there,
    while Telnet keeps reporting the stored values, so knowing them says nothing.
    """
    return receiver.sound_mode not in _DIRECT_SOUND_MODES


def tone_control_available(receiver: DenonAVR) -> bool:
    """Return whether bass, treble and the tone control toggle can be set.

    Dynamic EQ freezes all three: the receiver answers the command and drops
    it. `is not True` keeps them available while Dynamic EQ is still unknown.
    Direct drops them too, over HTTP and Telnet alike, as it does Audyssey.
    """
    return (
        bool(receiver.support_tone_control)
        and receiver.dynamic_eq is not True
        and receiver.sound_mode not in _DIRECT_SOUND_MODES
    )


class DenonAvrPendingValueEntity[_T](CoordinatorEntity[DenonAvrDataUpdateCoordinator]):
    """Base for entities that show an optimistic value until confirmed."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DenonAvrDataUpdateCoordinator,
        config_entry: DenonavrConfigEntry,
        key: str,
        follows_other_coordinator: bool = False,
    ) -> None:
        """Initialize the entity on the receiver's device."""
        super().__init__(coordinator)
        self._data = config_entry.runtime_data
        self._follows_other_coordinator = follows_other_coordinator
        self._receiver = coordinator.receiver
        self._attr_unique_id = receiver_unique_id(config_entry, key)
        # Identifiers alone: the media_player entities describe the device.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.unique_id or config_entry.entry_id)},
        )
        # The lock the coordinator refreshes under; PARALLEL_UPDATES only
        # serializes a call targeting several entities at once.
        self._action_lock = coordinator.lock
        self._pending_value: _T | None = None
        self._pending_value_expiry_unsub: Callable[[], None] | None = None

    def _read_value(self) -> _T | None:
        """Return the receiver's own confirmed value."""
        raise NotImplementedError

    def _value_placeholder(self, value: _T) -> str:
        """Return the value as a failed command's error message shows it."""
        return str(value)

    def _set_pending_value(self, value: _T) -> None:
        """Show a value optimistically and schedule its expiry."""
        self._pending_value = value
        if self._pending_value_expiry_unsub is not None:
            self._pending_value_expiry_unsub()
        self._pending_value_expiry_unsub = async_call_later(
            self.hass, PENDING_VALUE_TIMEOUT, self._async_handle_pending_expiry
        )

    def _clear_pending_value(self) -> None:
        """Clear the pending override and cancel its scheduled expiry."""
        self._pending_value = None
        if self._pending_value_expiry_unsub is not None:
            self._pending_value_expiry_unsub()
            self._pending_value_expiry_unsub = None

    @callback
    def _async_handle_pending_expiry(self, _now: Any) -> None:
        """Give up on an unconfirmed pending value and read the receiver.

        No poll is guaranteed to follow: the settings one is off by default
        and polling can be disabled, so without this the state could keep
        showing the pending value with nothing left to correct it.
        Forced, because expiry means no Telnet push confirmed the value and
        the Telnet-healthy skip would drop the read meant to replace it.
        """
        self._pending_value_expiry_unsub = None
        self._pending_value = None
        self.async_write_ha_state()
        self.coordinator.config_entry.async_create_task(
            self.hass,
            self.coordinator.async_refresh_forced(),
            "denonavr pending value expiry refresh",
        )

    @property
    def _current_value(self) -> _T | None:
        """Return the pending value if set, else the receiver's own value."""
        if self._pending_value is not None:
            return self._pending_value
        return self._read_value()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Clear an optimistic value once confirmed by the receiver.

        Not right after requesting a refresh: that request is debounced and
        does not complete synchronously with the call.
        """
        if (
            self._pending_value is not None
            and self._read_value() == self._pending_value
        ):
            self._clear_pending_value()
        super()._handle_coordinator_update()

    @override
    async def async_added_to_hass(self) -> None:
        """Also follow the other coordinator, if availability reads it."""
        await super().async_added_to_hass()
        if self._follows_other_coordinator:
            other = (
                self._data.coordinator
                if self.coordinator is self._data.settings_coordinator
                else self._data.settings_coordinator
            )
            self.async_on_remove(
                other.async_add_listener(self._handle_coordinator_update)
            )

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel any scheduled pending-value expiry."""
        if self._pending_value_expiry_unsub is not None:
            self._pending_value_expiry_unsub()
            self._pending_value_expiry_unsub = None

    async def _async_apply_change(
        self,
        *,
        send: Callable[[], Coroutine[Any, Any, None]],
        value: _T,
    ) -> None:
        """Send a command, update optimistically, then request confirmation."""
        async with self._action_lock:
            try:
                await send()
            except DenonAvrError as err:
                if isinstance(err, COMMAND_UNAVAILABLE_ON):
                    # An unreachable receiver rather than a rejected command,
                    # so no coordinator's data is current, whichever one asked.
                    mark_unavailable(self._data.coordinator, err)
                    mark_unavailable(self._data.settings_coordinator, err)
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="set_failed",
                    translation_placeholders={
                        "entity_id": self.entity_id,
                        "value": self._value_placeholder(value),
                        "host": self._receiver.host,
                        "error": error_message(err),
                    },
                ) from err

        self._set_pending_value(value)
        self.async_write_ha_state()

        # Confirming through the coordinator rather than reading here also
        # notifies every entity listening to it, including one on the other
        # coordinator whose availability follows this setting.
        await self.coordinator.async_request_refresh()
