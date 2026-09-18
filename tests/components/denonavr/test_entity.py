"""The tests for the denonavr entities' shared availability rules."""

from unittest.mock import MagicMock

import pytest

from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import TEST_UNIQUE_ID, get_entity_id, setup_denonavr

# Bass, treble and the tone control toggle share one availability rule,
# so every case below is asserted against all three.
TONE_CONTROL_ENTITIES = [
    pytest.param(NUMBER_DOMAIN, "bass", id="bass"),
    pytest.param(NUMBER_DOMAIN, "treble", id="treble"),
    pytest.param(SWITCH_DOMAIN, "tone_control", id="tone_control"),
]


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
async def test_tone_control_unavailable_under_dynamic_eq(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
) -> None:
    """Dynamic EQ freezes tone control."""
    client.dynamic_eq = True
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, domain, key))
    assert state
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
@pytest.mark.parametrize(
    "dynamic_eq",
    [
        # Unknown Dynamic EQ must not hide the entities: it comes from
        # the Audyssey coordinator, which may not have run yet.
        pytest.param(None, id="dynamic_eq_unknown"),
        pytest.param(False, id="dynamic_eq_off"),
    ],
)
async def test_tone_control_available(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
    dynamic_eq: bool | None,
) -> None:
    """Tone control is available whenever the receiver supports it and Dynamic EQ is not on."""
    client.dynamic_eq = dynamic_eq
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, domain, key))
    assert state
    assert state.state != STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
async def test_tone_control_not_created_without_support(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
) -> None:
    """A receiver without tone control gets no tone control entities."""
    client.support_tone_control = False
    await setup_denonavr(hass)

    assert (
        entity_registry.async_get_entity_id(domain, DOMAIN, f"{TEST_UNIQUE_ID}-{key}")
        is None
    )


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
async def test_tone_control_unavailable_when_support_is_lost(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
) -> None:
    """The library can drop the capability after setup, which gates the entities."""
    client.dynamic_eq = False
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, domain, key)
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    client.support_tone_control = False
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(("domain", "key"), TONE_CONTROL_ENTITIES)
async def test_tone_control_follows_dynamic_eq_without_a_status_poll(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    domain: str,
    key: str,
) -> None:
    """An Audyssey refresh alone frees tone control once Dynamic EQ reads off.

    Tone control rides the status coordinator, but only the Audyssey
    coordinator reads Dynamic EQ back. Refreshed directly rather than through
    the Dynamic EQ switch, whose command may also refresh the status.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, domain, key)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.dynamic_eq = False
    status_reads = client.async_update.await_count

    await entry.runtime_data.audyssey_coordinator.async_refresh()
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
