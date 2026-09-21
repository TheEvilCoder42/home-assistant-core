"""Test the denonavr integration setup and teardown."""

from collections.abc import Callable
import logging
from unittest.mock import MagicMock

from denonavr.exceptions import AvrCommandError, AvrNetworkError, AvrTimoutError
import pytest

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.components.denonavr.const import CONF_ZONE2, CONF_ZONE3
from homeassistant.components.denonavr.coordinator import mark_unavailable
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_MODEL, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import (
    TEST_HOST,
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_UNIQUE_ID,
    setup_denonavr,
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


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(AvrNetworkError("Connection refused", "GET"), id="network_error"),
        pytest.param(AvrCommandError("Rejected", "GET"), id="command_error"),
    ],
)
async def test_setup_entry_not_ready_on_receiver_error(
    hass: HomeAssistant, client: MagicMock, exception: Exception
) -> None:
    """Any receiver failure during setup must be retried, not fail permanently.

    A receiver that answers badly rather than not at all must not land in
    SETUP_ERROR, which never retries.
    """
    client.async_setup.side_effect = exception
    entry = _create_entry()
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize(
    ("exception", "state"),
    [
        pytest.param(
            AvrCommandError("Rejected", "GET"),
            ConfigEntryState.LOADED,
            id="rejected_command",
        ),
        pytest.param(
            AvrTimoutError("Timed out", "GET"),
            ConfigEntryState.SETUP_RETRY,
            id="connectivity_error",
        ),
    ],
)
async def test_telnet_warm_up_read_failures(
    hass: HomeAssistant,
    client: MagicMock,
    exception: Exception,
    state: ConfigEntryState,
) -> None:
    """The Telnet warm-up read classifies failures the way a poll does.

    A rejected command is logged and setup carries on, since the same
    failure during a poll doesn't take the entry down either; only a
    connectivity failure holds setup back for a retry.
    """
    client.async_update.side_effect = exception
    entry = _create_entry(options={"use_telnet": True})
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is state


async def test_setup_entry_not_ready_on_missing_receiver_info(
    hass: HomeAssistant, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A receiver that does not identify itself is retried, not loaded.

    The Telnet connection is already up at that point, so it must be closed
    or every retry leaks one. The reason goes into the retry, which Home
    Assistant logs itself, not into an error of its own on every attempt.
    """
    client.manufacturer = None
    entry = _create_entry(options={"use_telnet": True})
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason == (
        f"Receiver at {TEST_HOST} did not identify itself: manufacturer 'None',"
        f" name '{TEST_NAME}', model '{TEST_MODEL}', type '{TEST_RECEIVER_TYPE}'"
    )
    assert all(record.levelno < logging.WARNING for record in caplog.records)
    client.async_telnet_disconnect.assert_awaited_once()


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
@pytest.mark.usefixtures("client")
async def test_unload_removes_disabled_zone_entity(
    hass: HomeAssistant,
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


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("MV", id="status_event"),
        # Reaches both coordinators' callbacks, the Audyssey one first.
        pytest.param("PS", id="audyssey_event"),
    ],
)
async def test_telnet_push_logs_recovery_once(
    hass: HomeAssistant,
    client: MagicMock,
    fire_telnet_event: Callable[[str, str, str], None],
    caplog: pytest.LogCaptureFixture,
    event: str,
) -> None:
    """A push ending an outage logs the recovery once, as the drop was."""
    entry = await setup_denonavr(hass)
    coordinator = entry.runtime_data.coordinator
    audyssey_coordinator = entry.runtime_data.audyssey_coordinator
    mark_unavailable(coordinator, AvrNetworkError("Connection refused", "test"))
    assert not audyssey_coordinator.last_update_success

    fire_telnet_event("Main", event, "")
    fire_telnet_event("Main", event, "")

    assert coordinator.last_update_success
    assert audyssey_coordinator.last_update_success
    assert caplog.text.count("data recovered") == 1


async def test_telnet_push_does_not_log_recovery_while_healthy(
    hass: HomeAssistant,
    client: MagicMock,
    fire_telnet_event: Callable[[str, str, str], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pushes on a healthy receiver recover nothing."""
    await setup_denonavr(hass)

    fire_telnet_event("Main", "MV", "")
    fire_telnet_event("Main", "PS", "")

    assert "data recovered" not in caplog.text
