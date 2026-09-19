"""The tests for the denonavr integration setup and teardown."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import MagicMock

from denonavr.exceptions import (
    AvrCommandError,
    AvrForbiddenError,
    AvrNetworkError,
    AvrTimoutError,
)
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    CONF_ZONE2,
    CONF_ZONE3,
    DOMAIN,
    SETTLED_REFRESH_DELAY,
)
from homeassistant.components.denonavr.coordinator import mark_unavailable
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import (
    TEST_HOST,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_UNIQUE_ID,
    advance_time,
    get_entity_id,
    setup_denonavr,
)

from tests.common import async_fire_time_changed


async def test_setup_skips_redundant_settings_refresh_with_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Setup fetches the settings once with Telnet and polling both on.

    Each fetch is a full AppCommand0300 round trip, and setup runs again on
    every reload.
    """
    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: True}
    )

    assert client.async_update_settings.await_count == 1


async def test_setup_forces_settings_fetch_with_telnet_but_no_polling(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Setup still fetches the settings once when Telnet is on but polling is off.

    Telnet only pushes Audyssey data on a change, never on connect, so
    without forcing this fetch, async_refresh_settings's own Telnet-healthy
    skip would leave these entities unavailable indefinitely.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.dynamic_eq = None

    async def _populate(*_args: object, **_kwargs: object) -> None:
        client.dynamic_eq = True

    client.async_update_settings.side_effect = _populate

    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: False}
    )

    assert client.async_update_settings.await_count == 1
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


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
    entry = await setup_denonavr(hass)

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
    entry = await setup_denonavr(hass, options={CONF_USE_TELNET: True})

    assert entry.state is state


async def test_setup_entry_not_ready_on_missing_receiver_info(
    hass: HomeAssistant, client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """A receiver that does not identify itself is retried, not loaded.

    It is checked before Telnet connects, so a retry leaks no connection. The
    reason goes into the retry, which Home Assistant logs itself, not into an
    error of its own on every attempt.
    """
    client.manufacturer = None
    entry = await setup_denonavr(hass, options={CONF_USE_TELNET: True})

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.reason == (
        f"Receiver at {TEST_HOST} did not identify itself: manufacturer None,"
        f" name {TEST_NAME}, model {TEST_MODEL}, type {TEST_RECEIVER_TYPE}"
    )
    assert all(record.levelno < logging.WARNING for record in caplog.records)
    client.async_telnet_connect.assert_not_awaited()


@pytest.mark.parametrize(
    ("options", "await_count"),
    [
        pytest.param({CONF_USE_TELNET: True}, 1, id="telnet"),
        pytest.param({}, 0, id="no_telnet"),
    ],
)
async def test_telnet_disconnect_on_home_assistant_stop(
    hass: HomeAssistant,
    client: MagicMock,
    options: dict[str, Any],
    await_count: int,
) -> None:
    """Stopping Home Assistant disconnects Telnet only if it was used."""
    await setup_denonavr(hass, options=options)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert client.async_telnet_disconnect.await_count == await_count


async def test_unload_disconnects_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Unloading the entry must disconnect Telnet, if it was used."""
    entry = await setup_denonavr(hass, options={CONF_USE_TELNET: True})

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
    entry = await setup_denonavr(hass, options={zone_option: False})

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
    "events",
    [
        # A status push says nothing of the settings, so the outage ends with
        # the push that does.
        pytest.param(("MV", "MV", "PS"), id="status_event_first"),
        # Reaches both coordinators' callbacks, the settings one first.
        pytest.param(("PS", "PS"), id="settings_event"),
    ],
)
async def test_telnet_push_logs_recovery_once(
    hass: HomeAssistant,
    client: MagicMock,
    fire_telnet_event: Callable[[str, str, str], None],
    caplog: pytest.LogCaptureFixture,
    events: tuple[str, ...],
) -> None:
    """A push ending an outage logs the recovery once, as the drop was."""
    client.telnet_connected = True
    client.telnet_healthy = True
    entry = await setup_denonavr(hass)
    coordinator = entry.runtime_data.coordinator
    settings_coordinator = entry.runtime_data.settings_coordinator
    err = AvrNetworkError("Connection refused", "test")
    mark_unavailable(coordinator, err)
    # Healthy Telnet hands nothing over; a failed update_audyssey marks both.
    assert settings_coordinator.last_update_success
    mark_unavailable(settings_coordinator, err)

    for event in events:
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


async def test_change_undone_within_the_wait_leaves_nothing_pending(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A change back to the source last read cancels the one it undid.

    Otherwise that source would count as already requested, and changing to
    it again would never re-read.
    """
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    coordinator = entry.runtime_data.coordinator

    client.input_func = "Blu-ray"
    await coordinator.async_refresh()
    await advance_time(hass, freezer, 2)
    client.input_func = "TV-Box"
    await coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    await coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_change_during_a_slow_settings_read_is_reread(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """A change pushed while a settings read outlasts its wait is read after it.

    A debouncer drops a call that falls due while it is still running one.
    """
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count
    read_released = asyncio.Event()

    async def _slow_read(**kwargs: float) -> None:
        await read_released.wait()

    client.async_update_settings.side_effect = _slow_read
    client.input_func = "Blu-ray"
    await entry.runtime_data.coordinator.async_refresh()
    # The wait, then any action debounce behind it; not advance_time(), which
    # would wait for the blocked read.
    for seconds in (SETTLED_REFRESH_DELAY, 1):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
    assert client.async_update_settings.await_count == settings_reads + 1

    client.input_func = "Game"
    fire_telnet_event("Main", "SI", "GAME")
    for seconds in (SETTLED_REFRESH_DELAY, 1):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
    read_released.set()
    await hass.async_block_till_done()
    client.async_update_settings.side_effect = None

    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 2


async def test_failed_settings_reread_waits_for_the_next_change(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A re-read that failed is retried on the next change, not every refresh.

    Unpolled, each status refresh marks the settings available again, so a
    read the receiver keeps refusing would fail, and log, on every one.
    """
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    client.async_update_settings.side_effect = AvrForbiddenError("Forbidden", "POST")
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    await entry.runtime_data.coordinator.async_refresh()
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1

    client.async_update_settings.side_effect = None
    client.input_func = "Game"
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


async def test_change_rereads_settings_with_telnet_healthy(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """The re-read after a change reads even while Telnet is healthy.

    Telnet never reports some of what the change moves, such as whether the
    stream takes an LFE change. Later pushes of the same source do not
    ask again.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.input_func = "TV-Box"
    await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    fire_telnet_event("Main", "SI", "BD")
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1

    fire_telnet_event("Main", "SI", "BD")
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_skipped_settings_refresh_does_not_count_as_a_read(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """A settings refresh skipped under Telnet leaves the change its re-read.

    As a periodic poll does when it lands between the change and the push
    reporting it.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    await entry.runtime_data.settings_coordinator.async_refresh()
    assert client.async_update_settings.await_count == settings_reads

    fire_telnet_event("Main", "SI", "BD")
    await _wait_out_settled_refresh(hass, freezer)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_skipped_settings_refresh_keeps_the_wait(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """A settings refresh skipped within the wait does not restart it.

    It read nothing, so the push after it must not request the re-read again.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.input_func = "TV-Box"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count

    client.input_func = "Blu-ray"
    fire_telnet_event("Main", "SI", "BD")
    await advance_time(hass, freezer, 3)
    await entry.runtime_data.settings_coordinator.async_refresh()
    assert client.async_update_settings.await_count == settings_reads

    await advance_time(hass, freezer, 1)
    fire_telnet_event("Main", "MV", "50")
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 4)
    assert client.async_update_settings.await_count == settings_reads + 1


async def test_settled_settings_read_does_not_join_one_in_flight(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """The settled read after a change waits out a forced read, then reads.

    That one started before the change settled, so it saw the old sound mode
    or the receiver halfway through switching.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.sound_mode_raw = "Stereo"
    entry = await setup_denonavr(hass)
    settings_reads = client.async_update_settings.await_count
    release = asyncio.Event()

    async def _held_read(*args: object, **kwargs: object) -> None:
        await release.wait()

    client.async_update_settings.side_effect = _held_read
    in_flight = hass.async_create_task(
        entry.runtime_data.settings_coordinator.async_refresh_forced()
    )
    client.sound_mode_raw = "DOLBY AUDIO-DD+ +DSUR"
    fire_telnet_event("Main", "MS", "DOLBY AUDIO-DD+ +DSUR")
    # Not advance_time(): its async_block_till_done() would wait on the read
    # held here. The settled read starts eagerly and queues on the lock.
    freezer.tick(timedelta(seconds=SETTLED_REFRESH_DELAY))
    async_fire_time_changed(hass)
    release.set()
    await in_flight
    await hass.async_block_till_done()

    assert client.async_update_settings.await_count == settings_reads + 2


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
