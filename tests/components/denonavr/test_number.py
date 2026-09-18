"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.components.denonavr.number import (
    NUMBER_TYPES,
    DenonAvrNumberEntityDescription,
)
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
    UnitOfSoundPressure,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.typing import UNDEFINED

from . import TEST_UNIQUE_ID, setup_denonavr


def _entity_id(entity_registry: er.EntityRegistry, key: str) -> str:
    """Look up a number entity_id by its unique_id suffix."""
    entity_id = entity_registry.async_get_entity_id(
        NUMBER_DOMAIN, DOMAIN, f"{TEST_UNIQUE_ID}-{key}"
    )
    assert entity_id is not None
    return entity_id


@pytest.fixture(autouse=True)
def _fast_action_refresh_debounce():
    """Patch the action-refresh debounce cooldown down for every test.

    See the matching fixture/comment in test_select.py.
    """
    with patch(
        "homeassistant.components.denonavr.coordinator.ACTION_REFRESH_DEBOUNCE_COOLDOWN",
        0,
    ):
        yield


@pytest.mark.parametrize(
    "description", NUMBER_TYPES, ids=lambda description: description.key
)
def test_description_names_itself_through_translations(
    description: DenonAvrNumberEntityDescription,
) -> None:
    """Descriptions carry a translation key and no literal name."""
    assert description.translation_key == description.key
    assert description.name is UNDEFINED


@pytest.mark.usefixtures("client")
async def test_one_entity_per_description(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """The platform registers exactly the static descriptions, and nothing else."""
    entry = await setup_denonavr(hass)

    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert {
        registry_entry.unique_id
        for registry_entry in entries
        if registry_entry.domain == NUMBER_DOMAIN
    } == {f"{TEST_UNIQUE_ID}-{description.key}" for description in NUMBER_TYPES}


@pytest.mark.usefixtures("client")
async def test_audio_delay_state(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """The audio delay reports the receiver's value, in milliseconds."""
    await setup_denonavr(hass)

    state = hass.states.get(_entity_id(entity_registry, "audio_delay"))
    assert state
    assert state.state == "140"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfTime.MILLISECONDS
    assert state.attributes[ATTR_MIN] == 0
    assert state.attributes[ATTR_MAX] == 500


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(200, 200, id="whole_millisecond"),
        pytest.param(199.6, 200, id="rounded_to_the_nearest_millisecond"),
    ],
)
async def test_set_audio_delay(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    value: float,
    expected: int,
) -> None:
    """Setting the audio delay sends whole milliseconds, which is all PSDELAY takes."""
    await setup_denonavr(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(entity_registry, "audio_delay"),
            ATTR_VALUE: value,
        },
        blocking=True,
    )

    client.async_delay.assert_awaited_once_with(expected)


async def test_audio_delay_unknown_after_input_source_change(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The audio delay is unknown after an input source change, not stale.

    The receiver stores it per input source, so denonavr drops the
    value it read for the previous one rather than reporting it for the
    new one.
    """
    await setup_denonavr(hass)
    entity_id = _entity_id(entity_registry, "audio_delay")
    assert hass.states.get(entity_id).state == "140"

    client.audio_delay = None
    await async_update_entity(hass, entity_id)

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


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

    state = hass.states.get(_entity_id(entity_registry, key))
    assert state
    assert state.state == expected
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == UnitOfSoundPressure.DECIBEL
    assert state.attributes[ATTR_MIN] == -6
    assert state.attributes[ATTR_MAX] == 6


@pytest.mark.parametrize(
    ("key", "method", "value", "expected"),
    [
        pytest.param("bass", "async_set_bass", 4, 10, id="bass_above_zero"),
        pytest.param("bass", "async_set_bass", -6, 0, id="bass_at_the_minimum"),
        pytest.param("treble", "async_set_treble", -4, 2, id="treble_below_zero"),
        pytest.param("treble", "async_set_treble", 2.6, 9, id="treble_rounded"),
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
) -> None:
    """Setting bass or treble converts the dB back to the raw 0..12 scale."""
    client.dynamic_eq = False
    await setup_denonavr(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: _entity_id(entity_registry, key), ATTR_VALUE: value},
        blocking=True,
    )

    getattr(client, method).assert_awaited_once_with(expected)


@pytest.mark.parametrize("key", ["bass", "treble"])
async def test_tone_control_unknown_when_receiver_reports_no_value(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    key: str,
) -> None:
    """An empty GetToneControl payload reads unknown, and must not raise.

    The receiver answers with empty values for long stretches, which
    denonavr surfaces as None - offsetting that would be a TypeError.
    """
    client.dynamic_eq = False
    setattr(client, key, None)
    await setup_denonavr(hass)

    state = hass.states.get(_entity_id(entity_registry, key))
    assert state
    assert state.state == STATE_UNKNOWN
