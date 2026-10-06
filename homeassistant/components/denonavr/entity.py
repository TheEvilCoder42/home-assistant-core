"""Shared helpers for Denon AVR entities."""

from . import DenonavrConfigEntry
from .const import CONF_SERIAL_NUMBER


def receiver_unique_id(config_entry: DenonavrConfigEntry, key: str) -> str:
    """Return the unique_id of the receiver's entity with this key."""
    if (
        config_entry.data.get(CONF_SERIAL_NUMBER) is not None
        and config_entry.unique_id is not None
    ):
        return f"{config_entry.unique_id}-{key}"
    return f"{config_entry.entry_id}-{key}"
