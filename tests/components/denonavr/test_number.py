"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.number import (
    NUMBER_TYPES,
    DenonAvrNumberEntityDescription,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.typing import UNDEFINED

from . import TEST_HOST, TEST_UNIQUE_ID, get_entity_id, setup_denonavr

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
            ATTR_ENTITY_ID: get_entity_id(
                entity_registry, NUMBER_DOMAIN, "audio_delay"
            ),
            ATTR_VALUE: value,
        },
        blocking=True,
    )

    client.async_delay.assert_awaited_once_with(expected)


async def test_set_audio_delay_failure(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """A rejected command names the value the way the state shows it, with its unit."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "audio_delay")
    client.async_delay.side_effect = AvrCommandError("Command rejected", "PSDELAY")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 50},
            blocking=True,
        )

    assert err.value.translation_key == "set_failed"
    assert (
        str(err.value)
        == f"Setting {entity_id} to 50.0 ms failed on {TEST_HOST}: Command rejected"
    )


async def test_audio_delay_unknown_after_input_source_change(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The audio delay is unknown after an input source change, not stale.

    The receiver stores it per input source, so denonavr drops the
    value it read for the previous one rather than reporting it for the
    new one.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "audio_delay")
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

    state = hass.states.get(get_entity_id(entity_registry, NUMBER_DOMAIN, key))
    assert state
    assert state.state == expected


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
        {
            ATTR_ENTITY_ID: get_entity_id(entity_registry, NUMBER_DOMAIN, key),
            ATTR_VALUE: value,
        },
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
