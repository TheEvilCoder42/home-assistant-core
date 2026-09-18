"""The tests for denonavr's coordinator module-level refresh functions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

from denonavr.exceptions import (
    AvrCommandError,
    AvrIncompleteResponseError,
    AvrNetworkError,
)
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    COORDINATOR_UPDATE_INTERVAL,
    DOMAIN,
)
from homeassistant.components.denonavr.coordinator import (
    DenonAvrDataUpdateCoordinator,
    async_refresh_settings,
    async_refresh_status,
    mark_unavailable,
)
from homeassistant.core import HomeAssistant

from . import setup_denonavr

from tests.common import MockConfigEntry, async_fire_time_changed

# Both refresh functions carry the same per-zone contract, so each case below
# runs against both.
REFRESH_FUNCTIONS = pytest.mark.parametrize(
    ("refresh", "update_method"),
    [
        pytest.param(async_refresh_status, "async_update", id="status"),
        pytest.param(async_refresh_settings, "async_update_settings", id="settings"),
    ],
)


def _receiver_with_zones() -> tuple[MagicMock, MagicMock]:
    """Build a fake receiver with a Main and a Zone2 zone."""
    main = MagicMock()
    main.name = "Main Receiver"
    main.telnet_connected = False
    main.telnet_healthy = False
    main.async_update = AsyncMock()
    main.async_update_settings = AsyncMock()
    main.async_update_surround_parameters = AsyncMock()
    main.async_update_speaker_preset = AsyncMock()
    zone2 = MagicMock()
    zone2.zone = "Zone2"
    zone2.async_update = AsyncMock()
    zone2.async_update_settings = AsyncMock()
    zone2.async_update_surround_parameters = AsyncMock()
    zone2.async_update_speaker_preset = AsyncMock()
    main.zones = {"Main": main, "Zone2": zone2}
    return main, zone2


@REFRESH_FUNCTIONS
async def test_refresh_reaches_every_zone(
    refresh: Callable[..., Awaitable[None]], update_method: str
) -> None:
    """Each zone caches its own state.

    Both update calls only touch the zone they are called on, so Zone2 and
    Zone3 would otherwise never be refreshed.
    """
    main, zone2 = _receiver_with_zones()

    await refresh(main)

    getattr(main, update_method).assert_awaited_once()
    getattr(zone2, update_method).assert_awaited_once()


async def test_async_refresh_settings_shares_one_cache_id() -> None:
    """The zones' refreshes cost one request between them, not one each.

    The AppCommand0300 body carries no zone, so denonavr answers the
    later zones out of the first zone's request when they are all given
    the same cache id.
    """
    main, zone2 = _receiver_with_zones()

    await async_refresh_settings(main)

    main_cache_id = main.async_update_settings.await_args.kwargs["cache_id"]
    zone2_cache_id = zone2.async_update_settings.await_args.kwargs["cache_id"]
    assert main_cache_id is not None
    assert zone2_cache_id == main_cache_id


async def test_async_refresh_settings_uses_a_new_cache_id_each_refresh() -> None:
    """A reused cache id would hand the zones the settings from last time."""
    main, _ = _receiver_with_zones()

    await async_refresh_settings(main)
    first = main.async_update_settings.await_args.kwargs["cache_id"]
    await async_refresh_settings(main)
    second = main.async_update_settings.await_args.kwargs["cache_id"]

    assert first != second


@REFRESH_FUNCTIONS
async def test_refresh_skips_every_zone_when_telnet_healthy(
    refresh: Callable[..., Awaitable[None]], update_method: str
) -> None:
    """The Telnet-healthy skip applies to every zone at once, checked only once."""
    main, zone2 = _receiver_with_zones()
    main.telnet_connected = True
    main.telnet_healthy = True

    await refresh(main)

    getattr(main, update_method).assert_not_awaited()
    getattr(zone2, update_method).assert_not_awaited()


@pytest.mark.parametrize(
    ("telnet_healthy", "force", "read"),
    [
        pytest.param(False, False, True, id="without_telnet"),
        pytest.param(True, False, False, id="skipped"),
        pytest.param(True, True, True, id="forced"),
    ],
)
async def test_settings_refresh_returns_whether_it_read(
    telnet_healthy: bool, force: bool, read: bool
) -> None:
    """The caller records what the settings were read under only on a read."""
    main, _ = _receiver_with_zones()
    main.telnet_connected = telnet_healthy
    main.telnet_healthy = telnet_healthy

    assert await async_refresh_settings(main, force=force) is read
    assert main.async_update_settings.await_count == int(read)


@REFRESH_FUNCTIONS
async def test_refresh_continues_after_one_zones_command_error(
    refresh: Callable[..., Awaitable[None]], update_method: str
) -> None:
    """A rejected command in one zone doesn't abort the others.

    Zones do not all support the same settings, and one zone's
    AvrCommandError shouldn't leave the rest unrefreshed.
    """
    main, zone2 = _receiver_with_zones()
    getattr(main, update_method).side_effect = AvrCommandError("not supported", "Get")

    await refresh(main)

    getattr(zone2, update_method).assert_awaited_once()


@REFRESH_FUNCTIONS
async def test_refresh_stops_every_zone_on_a_connectivity_error(
    refresh: Callable[..., Awaitable[None]], update_method: str
) -> None:
    """A connectivity error means the receiver itself is unreachable.

    It re-raises so the whole update fails, rather than the remaining zones
    each reporting the same failure.
    """
    main, zone2 = _receiver_with_zones()
    getattr(main, update_method).side_effect = AvrNetworkError("Network error", "test")

    with pytest.raises(AvrNetworkError):
        await refresh(main)

    getattr(zone2, update_method).assert_not_awaited()


async def test_settings_refresh_tolerates_a_receiver_without_audyssey() -> None:
    """A receiver that does not know the query answers it short.

    denonavr tolerates the AvrProcessingError form of that but not this one,
    and a missing feature is not an unreachable receiver.
    """
    main, zone2 = _receiver_with_zones()
    main.async_update_settings.side_effect = AvrIncompleteResponseError(
        "Invalid length of response XML", "test"
    )

    await async_refresh_settings(main)

    zone2.async_update_settings.assert_awaited_once()


async def test_status_refresh_still_fails_on_an_incomplete_response() -> None:
    """Only the settings query carries a tag a receiver may not know."""
    main, zone2 = _receiver_with_zones()
    main.async_update.side_effect = AvrIncompleteResponseError(
        "Invalid length of response XML", "test"
    )

    with pytest.raises(AvrIncompleteResponseError):
        await async_refresh_status(main)

    zone2.async_update.assert_not_awaited()


async def test_async_refresh_settings_reads_the_post_loop_values_once() -> None:
    """The surround parameters and the speaker preset follow the zones' loop.

    They are receiver-wide, and with the same cache id denonavr answers
    them out of the zones' AppCommand0300 request instead of a second one.
    That only works once that request has completed, hence after the loop.
    """
    main, zone2 = _receiver_with_zones()
    calls = MagicMock()
    calls.attach_mock(main.async_update_settings, "main_settings")
    calls.attach_mock(zone2.async_update_settings, "zone2_settings")
    calls.attach_mock(main.async_update_surround_parameters, "surround_parameters")
    calls.attach_mock(main.async_update_speaker_preset, "speaker_preset")

    await async_refresh_settings(main)

    cache_id = main.async_update_settings.await_args.kwargs["cache_id"]
    assert cache_id is not None
    assert calls.mock_calls == [
        call.main_settings(cache_id=cache_id),
        call.zone2_settings(cache_id=cache_id),
        call.surround_parameters(global_update=True, cache_id=cache_id),
        call.speaker_preset(global_update=True, cache_id=cache_id),
    ]
    zone2.async_update_surround_parameters.assert_not_awaited()
    zone2.async_update_speaker_preset.assert_not_awaited()


@pytest.mark.parametrize(
    "failing",
    [
        pytest.param("async_update_settings", id="zone_settings"),
        pytest.param("async_update_surround_parameters", id="surround_parameters"),
        pytest.param("async_update_speaker_preset", id="speaker_preset"),
    ],
)
async def test_async_refresh_settings_survives_an_error_in_any_read(
    failing: str,
) -> None:
    """A receiver lacking one of the reads must not fail the refresh or the others."""
    main, zone2 = _receiver_with_zones()
    getattr(main, failing).side_effect = AvrCommandError("not supported", "test")

    assert await async_refresh_settings(main) is True

    zone2.async_update_settings.assert_awaited_once()
    main.async_update_surround_parameters.assert_awaited_once()
    main.async_update_speaker_preset.assert_awaited_once()


def _coordinator(
    hass: HomeAssistant, refresh_fn: AsyncMock, *, pref_disable_polling: bool = False
) -> DenonAvrDataUpdateCoordinator:
    """Build a coordinator polling every 30s through refresh_fn."""
    entry = MockConfigEntry(domain=DOMAIN, pref_disable_polling=pref_disable_polling)
    entry.add_to_hass(hass)
    main, _ = _receiver_with_zones()
    return DenonAvrDataUpdateCoordinator(
        hass, entry, main, asyncio.Lock(), "test", timedelta(seconds=30), refresh_fn
    )


async def test_a_failure_while_waiting_for_the_lock_forces_the_read(
    hass: HomeAssistant,
) -> None:
    """The skip is decided once the lock is held, not before waiting for it.

    A refresh queued behind a failing command would otherwise skip on its stale
    decision and clear the failure it was handed while waiting.
    """
    refresh_fn = AsyncMock()
    coordinator = _coordinator(hass, refresh_fn)
    await coordinator.async_refresh()

    async with coordinator.lock:
        refresh = hass.async_create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        mark_unavailable(coordinator, AvrNetworkError("Connection refused", "GET"))

    await refresh

    assert refresh_fn.await_args.kwargs["force"] is True


@pytest.mark.parametrize(
    ("pref_disable_polling", "polls"),
    [
        pytest.param(False, True, id="polling"),
        pytest.param(True, False, id="polling_disabled"),
    ],
)
async def test_polls_only_with_polling_enabled(
    hass: HomeAssistant, pref_disable_polling: bool, polls: bool
) -> None:
    """An interval alone does not mean the coordinator is still asking.

    _schedule_refresh() returns early on pref_disable_polling, so the other
    coordinator has to hand this one its verdict.
    """
    coordinator = _coordinator(
        hass, AsyncMock(), pref_disable_polling=pref_disable_polling
    )

    assert coordinator.polls is polls


async def test_internal_listener_does_not_start_the_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The integration's own cross-coordinator wiring must not drive polling.

    async_add_listener() starts the interval for its first listener, so wiring
    the coordinators together through it would poll with every entity disabled.
    """
    refresh_fn = AsyncMock()
    coordinator = _coordinator(hass, refresh_fn)
    coordinator.async_add_internal_listener(lambda: None)

    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    refresh_fn.assert_not_awaited()

    # An actual entity subscribing is what may start it.
    coordinator.async_add_listener(lambda: None)
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    refresh_fn.assert_awaited()


async def test_internal_listener_runs_once_per_refresh(hass: HomeAssistant) -> None:
    """Not polling on its own must not cost it the updates it exists for."""
    calls = 0

    def _record() -> None:
        nonlocal calls
        calls += 1

    coordinator = _coordinator(hass, AsyncMock())
    coordinator.async_add_internal_listener(_record)

    await coordinator.async_refresh()

    assert calls == 1


async def test_removing_an_internal_listener_stops_its_updates(
    hass: HomeAssistant,
) -> None:
    """Config entry unload has to be able to detach the wiring again."""
    calls = 0

    def _record() -> None:
        nonlocal calls
        calls += 1

    coordinator = _coordinator(hass, AsyncMock())
    remove = coordinator.async_add_internal_listener(_record)
    await coordinator.async_refresh()

    remove()
    await coordinator.async_refresh()

    assert calls == 1


@pytest.mark.parametrize(
    ("options", "settings_available"),
    [
        pytest.param({}, False, id="settings_has_no_poll"),
        pytest.param({CONF_UPDATE_AUDYSSEY: True}, True, id="settings_polls"),
    ],
)
async def test_general_failure_reaches_settings_only_where_it_cannot_read(
    hass: HomeAssistant,
    client: MagicMock,
    options: dict[str, bool],
    settings_available: bool,
) -> None:
    """Without a poll of its own it has to be handed the verdict.

    With one it has already reached the receiver, and Audyssey selects
    showing data the receiver just answered for beats hiding them over a
    failure on the other coordinator's query.
    """
    entry = await setup_denonavr(hass, options=options)

    client.async_update.side_effect = AvrNetworkError("Connection refused", "GET")
    await entry.runtime_data.coordinator.async_refresh()

    assert (
        entry.runtime_data.settings_coordinator.last_update_success
        is settings_available
    )


async def test_settings_coordinator_polls_when_option_on(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The settings coordinator actually polls on a schedule when the option is on.

    Exercises the real behavior (a call once the interval elapses)
    rather than just asserting update_interval was set, which would
    still pass even if the recurring poll's own listener registration
    were broken.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
    calls_before = client.async_update_settings.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_settings.await_count > calls_before


async def test_settings_coordinator_skips_poll_when_telnet_healthy(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A scheduled settings poll is skipped once Telnet already keeps it current.

    Mirrors async_refresh_status's own guard for the general
    coordinator - Telnet already pushes these settings live (see
    __init__.py's Telnet listener), so the receiver shouldn't be sent a
    full AppCommand0300 round trip again every interval.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
    client.telnet_connected = True
    client.telnet_healthy = True
    calls_before = client.async_update_settings.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_settings.await_count == calls_before


async def test_settings_coordinator_does_not_poll_when_option_off(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without the option the settings coordinator does not poll on a schedule.

    It still refreshes on demand, such as right after an action.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    calls_before = client.async_update_settings.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_settings.await_count == calls_before


async def test_settings_poll_needs_an_entity_not_just_internal_wiring(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The cross-coordinator wiring alone must not keep the poll running.

    Loading no platforms leaves that wiring as the only subscriber, so a
    poll here would be one no entity ever asked for.
    """
    with patch("homeassistant.components.denonavr.PLATFORMS", []):
        await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
        calls_before = client.async_update_settings.await_count

        freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert client.async_update_settings.await_count == calls_before
