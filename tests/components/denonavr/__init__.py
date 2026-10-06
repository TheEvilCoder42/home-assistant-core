"""Tests for the Denon AVR Network Receivers integration."""

from collections.abc import Mapping
from typing import Any

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry

TEST_HOST = "1.2.3.4"
TEST_NAME = "Test_Receiver"
TEST_MODEL = "model5"
TEST_SERIALNUMBER = "123456789"
TEST_MANUFACTURER = "Denon"
TEST_RECEIVER_TYPE = "avr-x"
TEST_ZONE = "Main"
TEST_UNIQUE_ID = f"{TEST_MODEL}-{TEST_SERIALNUMBER}"


async def setup_denonavr(
    hass: HomeAssistant,
    serial_number: str | None = TEST_SERIALNUMBER,
    options: Mapping[str, Any] | None = None,
    pref_disable_polling: bool = False,
) -> MockConfigEntry:
    """Set up the denonavr integration for tests."""
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        # What the config flow names it; the device takes it as its name
        # when a test loads no media_player.
        title=TEST_NAME,
        unique_id=TEST_UNIQUE_ID if serial_number else None,
        data={
            CONF_HOST: TEST_HOST,
            CONF_MODEL: TEST_MODEL,
            CONF_TYPE: TEST_RECEIVER_TYPE,
            CONF_MANUFACTURER: TEST_MANUFACTURER,
            CONF_SERIAL_NUMBER: serial_number,
        },
        options=options or {},
        pref_disable_polling=pref_disable_polling,
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry
