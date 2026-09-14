"""Tests for the Denon AVR Network Receivers integration."""

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
)
from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from tests.common import MockConfigEntry, async_fire_time_changed

TEST_HOST = "1.2.3.4"
TEST_NAME = "Test_Receiver"
TEST_MODEL = "model5"
TEST_SERIALNUMBER = "123456789"
TEST_MANUFACTURER = "Denon"
TEST_RECEIVER_TYPE = "avr-x"
TEST_ZONE = "Main"
TEST_UNIQUE_ID = f"{TEST_MODEL}-{TEST_SERIALNUMBER}"


def get_entity_id(entity_registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Return the entity_id of the receiver-level entity with this key."""
    entity_id = entity_registry.async_get_entity_id(
        domain, DOMAIN, f"{TEST_UNIQUE_ID}-{key}"
    )
    assert entity_id is not None
    return entity_id


async def setup_denonavr(
    hass: HomeAssistant,
    options: dict[str, bool] | None = None,
    pref_disable_polling: bool = False,
) -> MockConfigEntry:
    """Initialize the denonavr integration for tests."""
    entry_data = {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        # What the config flow names it; the device takes it as its name
        # when a test loads no media_player.
        title=TEST_NAME,
        unique_id=TEST_UNIQUE_ID,
        data=entry_data,
        options=options or {},
        pref_disable_polling=pref_disable_polling,
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry


async def advance_time(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    """Move the clock on and let whatever falls due run."""
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def wait_for_debounced_refresh(hass: HomeAssistant) -> None:
    """Let a coordinator's debounced confirmation refresh actually fire.

    async_block_till_done() alone returns before the debouncer's own task
    has run, so the sleep hands it the loop first.
    """
    await asyncio.sleep(0)
    await hass.async_block_till_done()
