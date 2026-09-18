"""Test the denonavr integration setup and teardown."""

from collections.abc import Callable
import logging
from unittest.mock import MagicMock

from denonavr.exceptions import AvrCommandError, AvrNetworkError, AvrTimoutError
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.components.denonavr.const import (
    CONF_ZONE2,
    CONF_ZONE3,
    SETTINGS_TELNET_EVENTS,
    SETTLED_REFRESH_DELAY,
)
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
    advance_time,
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
        # Reaches both coordinators' callbacks, the settings one first.
        pytest.param("PS", id="settings_event"),
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
    settings_coordinator = entry.runtime_data.settings_coordinator
    mark_unavailable(coordinator, AvrNetworkError("Connection refused", "test"))
    assert not settings_coordinator.last_update_success

    fire_telnet_event("Main", event, "")
    fire_telnet_event("Main", event, "")

    assert coordinator.last_update_success
    assert settings_coordinator.last_update_success
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


async def _wait_out_settled_refresh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Let a settled refresh and the action debounce behind it run."""
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY)
    await advance_time(hass, freezer, 1)


@pytest.mark.parametrize(
    ("attribute", "value", "reads"),
    [
        pytest.param("input_func", "TV-Box", 0, id="same_source"),
        pytest.param("input_func", "Blu-ray", 1, id="new_source"),
        pytest.param("sound_mode_raw", "Stereo", 0, id="same_sound_mode"),
        pytest.param("sound_mode_raw", "DOLBY AUDIO-DD+ +DSUR", 1, id="new_sound_mode"),
    ],
)
async def test_change_rereads_settings_once_settled(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    attribute: str,
    value: str,
    reads: int,
) -> None:
    """A source or sound mode change re-reads the settings.

    The audio delay is per source, and whether the LFE level can be set
    follows the sound mode. Not straight away: the receiver takes a few
    seconds to switch. The value read at setup is the baseline rather than a
    change.
    """
    client.input_func = "TV-Box"
    client.sound_mode_raw = "Stereo"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    setattr(client, attribute, value)
    await entry.runtime_data.coordinator.async_refresh()
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 1)
    assert client.async_update_settings.await_count == settings_reads

    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + reads


async def test_settled_refresh_waits_for_the_last_request(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A second change restarts the wait: it moves values of its own."""
    entry = await setup_denonavr(hass)
    settings_coordinator = entry.runtime_data.settings_coordinator
    settings_reads = client.async_update_settings.await_count

    settings_coordinator.async_request_settled_refresh()
    await advance_time(hass, freezer, 3)
    settings_coordinator.async_request_settled_refresh()
    # Past where the first request alone would have read, action debounce too.
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 3)
    await advance_time(hass, freezer, 1)
    assert client.async_update_settings.await_count == settings_reads

    await advance_time(hass, freezer, 2)
    await advance_time(hass, freezer, 1)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_status_refresh_without_a_change_keeps_the_wait(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Only a further change restarts the re-read's wait, not every refresh.

    Each command's confirming read refreshes the status, so a run of volume
    presses would otherwise keep the settings stale for as long as it lasts.
    """
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    await entry.runtime_data.coordinator.async_refresh()
    await advance_time(hass, freezer, 3)
    await entry.runtime_data.coordinator.async_refresh()
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 3)
    await advance_time(hass, freezer, 1)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_failed_settings_reread_is_retried(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The next status refresh retries a re-read that failed."""
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    client.async_update_settings.side_effect = AvrNetworkError(
        "Connection refused", "POST"
    )
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1

    client.async_update_settings.side_effect = None
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 2

    # Read under the new source now: no further re-read.
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 2


async def test_settings_read_after_input_change_needs_no_reread(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A settings read that already saw the new source makes one redundant."""
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)

    client.input_func = "Blu-ray"
    await entry.runtime_data.settings_coordinator.async_refresh()
    settings_reads = client.async_update_settings.await_count
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)

    assert client.async_update_settings.await_count == settings_reads


# Integrations may leave timers behind by default; this test is about one.
@pytest.mark.parametrize("expected_lingering_timers", [False])
async def test_unload_cancels_pending_settings_reread(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A re-read still waiting for the receiver to settle dies with the entry.

    A timer left behind fails the test at teardown.
    """
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    client.input_func = "Blu-ray"
    await entry.runtime_data.coordinator.async_refresh()

    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("event", SETTINGS_TELNET_EVENTS)
async def test_every_settings_telnet_event_notifies_the_coordinator(
    hass: HomeAssistant,
    fire_telnet_event: Callable[[str, str, str], None],
    event: str,
) -> None:
    """The settings coordinator is notified for each event group it owns.

    The Audyssey values and the speaker preset arrive as different
    Telnet events, so registering only one of them leaves the other's
    entities stale after a front-panel change.
    """
    entry = await setup_denonavr(hass)
    listener = MagicMock()
    entry.runtime_data.settings_coordinator.async_add_internal_listener(listener)

    fire_telnet_event("Main", "MV", "50")
    listener.assert_not_called()
    fire_telnet_event("Main", event, "")
    listener.assert_called_once()

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    fire_telnet_event("Main", event, "")
    listener.assert_called_once()
