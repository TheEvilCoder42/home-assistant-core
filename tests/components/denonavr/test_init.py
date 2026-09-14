"""Test the denonavr integration setup and teardown."""

from unittest.mock import MagicMock

from denonavr.exceptions import AvrNetworkError
import pytest

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.components.denonavr.const import CONF_ZONE2, CONF_ZONE3
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_MODEL, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import (
    TEST_HOST,
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_UNIQUE_ID,
)

from tests.common import MockConfigEntry


def _create_entry(options: dict | None = None) -> MockConfigEntry:
    """Build a not-yet-added config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_UNIQUE_ID,
        data={
            CONF_HOST: TEST_HOST,
            CONF_MODEL: TEST_MODEL,
            CONF_TYPE: TEST_RECEIVER_TYPE,
            CONF_MANUFACTURER: TEST_MANUFACTURER,
            CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
        },
        options=options or {},
    )


async def test_setup_entry_not_ready_on_connection_error(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A connection failure during setup must be retried, not fail permanently."""
    client.async_setup.side_effect = AvrNetworkError("Connection refused", "GET")
    entry = _create_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_proceeds_despite_missing_receiver_info(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Document current behavior: incomplete receiver info doesn't block setup.

    receiver.py's async_connect_receiver() logs an error and returns
    False when manufacturer/name/model_name/receiver_type are missing,
    but __init__.py never checks that return value, so setup proceeds
    anyway - a pre-existing gap, not something this test asserts is
    correct, just the current, actual behavior.
    """
    client.manufacturer = None
    entry = _create_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_telnet_disconnects_on_home_assistant_stop(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Telnet must be disconnected when Home Assistant stops, if it was used."""
    entry = _create_entry(options={"use_telnet": True})
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    client.async_telnet_disconnect.assert_awaited_once()


async def test_no_telnet_disconnect_on_stop_without_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Without Telnet enabled, stopping Home Assistant must not try to disconnect it."""
    entry = _create_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    client.async_telnet_disconnect.assert_not_awaited()


async def test_unload_disconnects_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Unloading the entry must disconnect Telnet, if it was used."""
    entry = _create_entry(options={"use_telnet": True})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    client.async_telnet_disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    ("zone_option", "zone_unique_id_suffix"),
    [
        pytest.param(CONF_ZONE2, "Zone2", id="zone2"),
        pytest.param(CONF_ZONE3, "Zone3", id="zone3"),
    ],
)
async def test_unload_removes_disabled_zone_entity(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    zone_option: str,
    zone_unique_id_suffix: str,
) -> None:
    """A zone entity must be removed from the registry once its option is turned off.

    Simulates a stray leftover entity from when the zone option was
    previously enabled - the real zone-creation path isn't exercised
    here since the client mock always reports a single "Main" zone.
    """
    entry = _create_entry(options={zone_option: False})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stray_entity_id = entity_registry.async_get_or_create(
        "media_player",
        DOMAIN,
        f"{TEST_UNIQUE_ID}-{zone_unique_id_suffix}",
        config_entry=entry,
    ).entity_id

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(stray_entity_id) is None
