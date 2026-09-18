"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the number entities and their registry entries."""
    # Dynamic EQ would leave bass and treble unavailable, hiding their values.
    client.dynamic_eq = False
    with patch("homeassistant.components.denonavr.PLATFORMS", [Platform.NUMBER]):
        entry = await setup_denonavr(hass)

    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param("bass", "4", id="bass_above_zero"),
        pytest.param("treble", "-4", id="treble_below_zero"),
    ],
)
async def test_tone_control_state(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    key: str,
    expected: str,
) -> None:
    """Bass and treble report the dB the receiver shows, not its raw 0..12 value."""
    client.dynamic_eq = False
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, NUMBER_DOMAIN, key))
    assert state
    assert state.state == expected


@pytest.mark.parametrize(
    ("key", "method", "value", "expected", "state"),
    [
        pytest.param("bass", "async_set_bass", 4, 10, "4", id="bass_above_zero"),
        pytest.param("bass", "async_set_bass", -6, 0, "-6", id="bass_at_the_minimum"),
        pytest.param("treble", "async_set_treble", -4, 2, "-4", id="treble_below_zero"),
        pytest.param("treble", "async_set_treble", 2.6, 9, "3", id="treble_rounded"),
    ],
)
async def test_set_tone_control(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    key: str,
    method: str,
    value: float,
    expected: int,
    state: str,
) -> None:
    """Setting bass or treble converts the dB back to the raw 0..12 scale.

    The state shows the value sent until the receiver reports it back on
    its raw scale, which releases it to follow the receiver again.
    """
    client.dynamic_eq = False
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    getattr(client, method).assert_awaited_once_with(expected)
    assert hass.states.get(entity_id).state == state

    setattr(client, key, expected)
    await entry.runtime_data.coordinator.async_refresh()
    setattr(client, key, 3)
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "-3"


@pytest.mark.parametrize("key", ["bass", "treble"])
async def test_tone_control_unknown_when_receiver_reports_no_value(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    key: str,
) -> None:
    """A bass or treble denonavr has not read yet is unknown, and must not raise.

    The receiver can answer GetToneControl blank from setup on, and
    denonavr keeps None until a value arrives - offsetting that would be
    a TypeError.
    """
    client.dynamic_eq = False
    setattr(client, key, None)
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, NUMBER_DOMAIN, key))
    assert state
    assert state.state == STATE_UNKNOWN
