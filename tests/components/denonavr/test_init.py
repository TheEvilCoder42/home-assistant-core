"""The tests for the denonavr integration setup."""

from unittest.mock import MagicMock

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import get_entity_id, setup_denonavr


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
