"""The tests for denonavr's coordinator module-level refresh functions."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, call

from denonavr.exceptions import (
    AvrCommandError,
    AvrIncompleteResponseError,
    AvrNetworkError,
)
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.components.denonavr.coordinator import (
    DenonAvrDataUpdateCoordinator,
    async_refresh_settings,
    async_refresh_status,
    mark_unavailable,
)
from homeassistant.core import HomeAssistant

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


async def test_async_refresh_settings_reads_the_preset_from_the_loops_request() -> None:
    """The preset is fetched once, after the zones, under their cache id.

    It has its own entry point, which async_update_settings() doesn't
    call. The shared cache id lets it read the request the zones already
    made, but only once that request has finished.
    """
    main, zone2 = _receiver_with_zones()
    manager = MagicMock()
    manager.attach_mock(main.async_update_settings, "main_settings")
    manager.attach_mock(zone2.async_update_settings, "zone2_settings")
    manager.attach_mock(main.async_update_speaker_preset, "speaker_preset")

    await async_refresh_settings(main)

    cache_id = main.async_update_settings.await_args.kwargs["cache_id"]
    assert cache_id is not None
    assert manager.mock_calls == [
        call.main_settings(cache_id=cache_id),
        call.zone2_settings(cache_id=cache_id),
        call.speaker_preset(global_update=True, cache_id=cache_id),
    ]
    zone2.async_update_speaker_preset.assert_not_awaited()


async def test_async_refresh_settings_skips_the_speaker_preset_when_telnet_healthy() -> (
    None
):
    """The Telnet-healthy skip covers the speaker preset too."""
    main, _ = _receiver_with_zones()
    main.telnet_connected = True
    main.telnet_healthy = True

    await async_refresh_settings(main)

    main.async_update_speaker_preset.assert_not_awaited()


async def test_async_refresh_settings_continues_after_a_speaker_preset_error() -> None:
    """A receiver without the speaker preset command doesn't fail the refresh."""
    main, _ = _receiver_with_zones()
    main.async_update_speaker_preset = AsyncMock(
        side_effect=AvrCommandError("not supported", "GetSpeakerPreset")
    )

    await async_refresh_settings(main)

    main.async_update_speaker_preset.assert_awaited_once()


async def test_async_refresh_settings_fetches_the_preset_after_a_zone_error() -> None:
    """One zone's failure doesn't stop the receiver-wide preset fetch."""
    main, _ = _receiver_with_zones()
    main.async_update_settings = AsyncMock(
        side_effect=AvrCommandError("not supported", "GetAudyssey")
    )

    await async_refresh_settings(main)

    main.async_update_speaker_preset.assert_awaited_once()


async def test_async_refresh_settings_reads_the_surround_parameters_once() -> None:
    """The surround parameters are read after the zones, under their cache id.

    They are receiver-wide, and with the same cache id denonavr answers
    them out of the zones' AppCommand0300 request instead of a second one.
    That only works once that request has completed, hence after the loop.
    """
    main, zone2 = _receiver_with_zones()
    calls = MagicMock()
    calls.attach_mock(main.async_update_settings, "main_settings")
    calls.attach_mock(zone2.async_update_settings, "zone2_settings")
    calls.attach_mock(main.async_update_surround_parameters, "surround_parameters")

    await async_refresh_settings(main)

    cache_id = main.async_update_settings.await_args.kwargs["cache_id"]
    assert cache_id is not None
    assert calls.mock_calls == [
        call.main_settings(cache_id=cache_id),
        call.zone2_settings(cache_id=cache_id),
        call.surround_parameters(global_update=True, cache_id=cache_id),
    ]
    zone2.async_update_surround_parameters.assert_not_awaited()


async def test_async_refresh_settings_survives_a_surround_parameter_error() -> None:
    """An error from the surround parameters must not fail the settings refresh."""
    main, zone2 = _receiver_with_zones()
    main.async_update_surround_parameters.side_effect = AvrCommandError(
        "not supported", "GetSurroundParameter"
    )

    await async_refresh_settings(main)

    main.async_update_settings.assert_awaited_once()
    zone2.async_update_settings.assert_awaited_once()


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
    ("peer_available", "force"),
    [
        pytest.param(True, False, id="peer_available"),
        pytest.param(False, True, id="peer_failed"),
    ],
)
async def test_a_failed_peer_forces_the_read(
    hass: HomeAssistant, peer_available: bool, force: bool
) -> None:
    """A skip must not hide the other coordinator's failure either.

    With Telnet healthy this poll would otherwise never ask the receiver the
    other coordinator just failed to reach, nor confirm that it is back.
    """
    refresh_fn = AsyncMock()
    coordinator = _coordinator(hass, refresh_fn)
    coordinator.peer = _coordinator(hass, AsyncMock())
    coordinator.peer.last_update_success = peer_available

    await coordinator.async_refresh()

    assert refresh_fn.await_args.kwargs["force"] is force


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


async def test_internal_listener_is_still_notified(hass: HomeAssistant) -> None:
    """Not polling on its own must not cost it the updates it exists for."""
    called = False

    def _record() -> None:
        nonlocal called
        called = True

    coordinator = _coordinator(hass, AsyncMock())
    coordinator.async_add_internal_listener(_record)

    coordinator.async_update_listeners()

    assert called


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
    coordinator.async_update_listeners()

    remove()
    coordinator.async_update_listeners()

    assert calls == 1
