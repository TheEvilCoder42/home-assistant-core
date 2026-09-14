"""The tests for the denonavr integration setup and teardown."""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

from denonavr.exceptions import AvrNetworkError
import pytest

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    CONF_ZONE2,
    CONF_ZONE3,
    DOMAIN,
)
from homeassistant.components.denonavr.coordinator import mark_unavailable
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import TEST_UNIQUE_ID, get_entity_id, setup_denonavr


async def test_setup_skips_redundant_audyssey_refresh_with_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Setup fetches Audyssey once with Telnet and "Update Audyssey settings" on.

    Each fetch is a full AppCommand0300 round trip, and setup runs again on
    every reload.
    """
    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: True}
    )

    assert client.async_update_audyssey.await_count == 1


async def test_setup_forces_audyssey_fetch_with_telnet_but_no_polling(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Setup still fetches Audyssey once when Telnet is on but polling is off.

    Telnet only pushes Audyssey data on a change, never on connect, so
    without forcing this fetch, async_refresh_audyssey's own Telnet-healthy
    skip would leave these entities unavailable indefinitely.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.dynamic_eq = None

    async def _populate(*_args: object, **_kwargs: object) -> None:
        client.dynamic_eq = True

    client.async_update_audyssey.side_effect = _populate

    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: False}
    )

    assert client.async_update_audyssey.await_count == 1
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_setup_entry_not_ready_on_connection_error(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A connection failure during setup must be retried, not fail permanently."""
    client.async_setup.side_effect = AvrNetworkError("Connection refused", "GET")
    entry = await setup_denonavr(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_proceeds_despite_missing_receiver_info(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Document current behavior: incomplete receiver info doesn't block setup.

    async_connect_receiver() returns False when the receiver's identifying
    fields are missing, but nothing checks that return value.
    """
    client.manufacturer = None
    entry = await setup_denonavr(hass)

    assert entry.state is ConfigEntryState.LOADED


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
        # A status push says nothing of the Audyssey settings, so the outage
        # ends with the push that does.
        pytest.param(("MV", "MV", "PS"), id="status_event_first"),
        # Reaches both coordinators' callbacks, the Audyssey one first.
        pytest.param(("PS", "PS"), id="audyssey_event"),
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
    audyssey_coordinator = entry.runtime_data.audyssey_coordinator
    err = AvrNetworkError("Connection refused", "test")
    mark_unavailable(coordinator, err)
    # Healthy Telnet hands nothing over; a failed update_audyssey marks both.
    assert audyssey_coordinator.last_update_success
    mark_unavailable(audyssey_coordinator, err)

    for event in events:
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
