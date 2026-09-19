"""The tests for denonavr's coordinator module-level refresh functions."""

from unittest.mock import AsyncMock, MagicMock

from denonavr.exceptions import AvrCommandError

from homeassistant.components.denonavr.coordinator import async_refresh_settings


def _receiver_with_zones() -> tuple[MagicMock, MagicMock]:
    """Build a fake receiver with a Main and a Zone2 zone."""
    main = MagicMock()
    main.name = "Main Receiver"
    main.telnet_connected = False
    main.telnet_healthy = False
    main.async_update_settings = AsyncMock()
    zone2 = MagicMock()
    zone2.zone = "Zone2"
    zone2.async_update_settings = AsyncMock()
    main.zones = {"Main": main, "Zone2": zone2}
    return main, zone2


async def test_async_refresh_settings_refreshes_every_zone() -> None:
    """Each zone is its own object with its own cached copy of these settings.

    denonavr's async_update_settings() only updates the zone it's
    called on - a multi-zone receiver's Zone2/Zone3 media players would
    otherwise never get their own Audyssey data refreshed at all.
    """
    main, zone2 = _receiver_with_zones()

    await async_refresh_settings(main)

    main.async_update_settings.assert_awaited_once()
    zone2.async_update_settings.assert_awaited_once()


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


async def test_async_refresh_settings_skips_every_zone_when_telnet_healthy() -> None:
    """The Telnet-healthy skip applies to every zone at once, checked only once."""
    main, zone2 = _receiver_with_zones()
    main.telnet_connected = True
    main.telnet_healthy = True

    await async_refresh_settings(main)

    main.async_update_settings.assert_not_awaited()
    zone2.async_update_settings.assert_not_awaited()


async def test_async_refresh_settings_forced_bypasses_the_telnet_healthy_skip() -> None:
    """force=True refreshes anyway: Telnet never pushes these on connect.

    Without it the one-time initial fetch in __init__.py would be
    skipped on a Telnet setup, leaving the backing entities
    unavailable indefinitely.
    """
    main, zone2 = _receiver_with_zones()
    main.telnet_connected = True
    main.telnet_healthy = True

    await async_refresh_settings(main, force=True)

    main.async_update_settings.assert_awaited_once()
    zone2.async_update_settings.assert_awaited_once()


async def test_async_refresh_settings_continues_after_one_zones_command_error() -> None:
    """A rejected/unsupported command in one zone doesn't abort the others.

    Matches async_refresh_status's own per-zone resilience - not every
    zone need support Audyssey identically, and one zone's
    AvrCommandError shouldn't leave every other zone's data unrefreshed.
    """
    main, zone2 = _receiver_with_zones()
    main.async_update_settings = AsyncMock(
        side_effect=AvrCommandError("not supported", "GetAudyssey")
    )

    await async_refresh_settings(main)

    zone2.async_update_settings.assert_awaited_once()
