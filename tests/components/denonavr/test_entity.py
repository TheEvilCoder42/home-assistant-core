"""The tests for the denonavr entities' shared availability rules."""

from unittest.mock import MagicMock

import pytest

from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import get_entity_id, setup_denonavr

# Bass, treble and the tone control toggle share one availability rule,
# so every case below is asserted against all three.
TONE_CONTROL_ENTITIES = [
    pytest.param(NUMBER_DOMAIN, "bass", id="bass"),
    pytest.param(NUMBER_DOMAIN, "treble", id="treble"),
    pytest.param(SWITCH_DOMAIN, "tone_control", id="tone_control"),
]


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

    state = hass.states.get(get_entity_id(entity_registry, domain, key))
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

    state = hass.states.get(get_entity_id(entity_registry, domain, key))
    assert state
    assert state.state != STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
async def test_tone_control_follows_dynamic_eq_without_a_status_poll(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
) -> None:
    """A settings refresh alone frees tone control once Dynamic EQ reads off.

    Tone control rides the status coordinator, but only the settings
    coordinator reads Dynamic EQ back. Refreshed directly rather than through
    the Dynamic EQ switch, whose command may also refresh the status.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, domain, key)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.dynamic_eq = False
    status_reads = client.async_update.await_count

    await entry.runtime_data.settings_coordinator.async_refresh()
    await hass.async_block_till_done()

    assert client.async_update.await_count == status_reads
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
@pytest.mark.parametrize("sound_mode", ["DIRECT", "PURE DIRECT"])
async def test_tone_control_follows_direct(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
    sound_mode: str,
) -> None:
    """Tone control is unavailable in Direct, seen by a status refresh.

    The receiver drops every tone control write in Direct while Telnet keeps
    answering the stored values, so the values alone cannot tell.
    """
    client.dynamic_eq = False
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, domain, key)
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    client.sound_mode = sound_mode
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.sound_mode = "STEREO"
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
