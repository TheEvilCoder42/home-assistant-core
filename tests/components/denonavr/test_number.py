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
    SIGNAL_STRENGTH_DECIBELS,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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
async def test_lfe_level_state(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """The LFE level reports the receiver's value, in dB.

    Negative and not sign-flipped: denonavr hands HTTP's already-signed
    value straight through, and the domain is symmetric enough that an
    inversion would otherwise look plausible.
    """
    await setup_denonavr(hass)

    state = hass.states.get(_entity_id(entity_registry, "lfe_level"))
    assert state
    assert state.state == "-2"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == SIGNAL_STRENGTH_DECIBELS
    assert state.attributes[ATTR_MIN] == -10
    assert state.attributes[ATTR_MAX] == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(-5, -5, id="whole_decibel"),
        pytest.param(-4.6, -5, id="rounded_to_the_nearest_decibel"),
    ],
)
async def test_set_lfe_level(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    value: float,
    expected: int,
) -> None:
    """Setting the LFE level sends a whole, signed decibel.

    async_lfe formats with str().zfill(), so a float would reach the
    receiver as PSLFE 5.0 - accepted with a 200 and silently ignored.
    """
    await setup_denonavr(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: _entity_id(entity_registry, "lfe_level"),
            ATTR_VALUE: value,
        },
        blocking=True,
    )

    client.async_lfe.assert_awaited_once_with(expected)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(-11, id="below_the_minimum"),
        pytest.param(1, id="above_the_maximum"),
    ],
)
async def test_set_lfe_level_out_of_range_never_reaches_the_receiver(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    value: float,
) -> None:
    """Core's own min/max rejects the call before the library sees it."""
    await setup_denonavr(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {
                ATTR_ENTITY_ID: _entity_id(entity_registry, "lfe_level"),
                ATTR_VALUE: value,
            },
            blocking=True,
        )

    client.async_lfe.assert_not_awaited()


async def test_lfe_level_unknown_when_the_stream_carries_no_lfe(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The LFE level is unknown, not unavailable, when the receiver reports none.

    The receiver answers the parameter only while the incoming stream
    actually has an LFE channel, which on a music or TV source is most
    of the time.
    """
    await setup_denonavr(hass)
    entity_id = _entity_id(entity_registry, "lfe_level")
    assert hass.states.get(entity_id).state == "-2"

    client.lfe = None
    await async_update_entity(hass, entity_id)

    assert hass.states.get(entity_id).state == STATE_UNKNOWN
