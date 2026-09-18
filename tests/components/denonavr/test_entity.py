"""The tests for the denonavr entities' shared availability rules."""

from unittest.mock import MagicMock

import pytest

from homeassistant.components.denonavr.config_flow import DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import TEST_UNIQUE_ID, setup_denonavr

# Bass, treble and the tone control toggle share one availability rule,
# so every case below is asserted against all three.
TONE_CONTROL_ENTITIES = [
    pytest.param(NUMBER_DOMAIN, "bass", id="bass"),
    pytest.param(NUMBER_DOMAIN, "treble", id="treble"),
    pytest.param(SWITCH_DOMAIN, "tone_control", id="tone_control"),
]


def _entity_id(entity_registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Look up an entity_id by its unique_id suffix."""
    entity_id = entity_registry.async_get_entity_id(
        domain, DOMAIN, f"{TEST_UNIQUE_ID}-{key}"
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
@pytest.mark.parametrize(
    ("support_tone_control", "dynamic_eq"),
    [
        pytest.param(False, False, id="receiver_has_no_tone_control"),
        pytest.param(True, True, id="dynamic_eq_freezes_tone_control"),
    ],
)
async def test_tone_control_unavailable(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
    support_tone_control: bool,
    dynamic_eq: bool,
) -> None:
    """Tone control is unavailable without the capability, or under Dynamic EQ."""
    client.support_tone_control = support_tone_control
    client.dynamic_eq = dynamic_eq
    await setup_denonavr(hass)

    state = hass.states.get(_entity_id(entity_registry, domain, key))
    assert state
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
@pytest.mark.parametrize(
    ("dynamic_eq", "tone_control_status"),
    [
        # Unknown Dynamic EQ must not hide the entities: it comes from
        # the settings coordinator, which may not have run yet.
        pytest.param(None, True, id="dynamic_eq_unknown"),
        # tone_control_status is not part of the gate - the reference
        # receiver reported it False while tone control worked, and
        # True while every write was refused.
        pytest.param(False, True, id="status_disagrees"),
        pytest.param(False, False, id="status_agrees"),
    ],
)
async def test_tone_control_available(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
    dynamic_eq: bool | None,
    tone_control_status: bool,
) -> None:
    """Tone control is available whenever the receiver supports it and Dynamic EQ is not on."""
    client.dynamic_eq = dynamic_eq
    client.tone_control_status = tone_control_status
    await setup_denonavr(hass)

    state = hass.states.get(_entity_id(entity_registry, domain, key))
    assert state
    assert state.state != STATE_UNAVAILABLE
