"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.components.denonavr.number import (
    NUMBER_TYPES,
    DenonAvrNumberEntityDescription,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import UNDEFINED

from . import TEST_HOST, TEST_UNIQUE_ID, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")

# The AVR-X1700H declares one subwoofer; TWO_SUBWOOFERS stands in for a
# model with more.
ONE_SUBWOOFER = {"Subwoofer": 0.0}
TWO_SUBWOOFERS = {"Subwoofer": 0.0, "Subwoofer 2": -1.5}


def _reporting(client: MagicMock, levels: dict[str, float]) -> None:
    """Make the receiver report exactly these subwoofer levels."""
    client.subwoofer_levels = levels
    client.subwoofer_level.side_effect = levels.get


def _subwoofer_unique_ids(
    entity_registry: er.EntityRegistry, entry_id: str
) -> list[str]:
    """Return the subwoofer level unique_ids, in registration order.

    Filtered rather than compared whole: other number entities register on
    the same entry.
    """
    return [
        registry_entry.unique_id
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry_id
        )
        if registry_entry.domain == NUMBER_DOMAIN
        and "-subwoofer_level_" in registry_entry.unique_id
    ]


async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    client: MagicMock,
) -> None:
    """Test the number entities and their registry entries."""
    # An idle receiver reports no levels, which would leave nothing to snapshot.
    _reporting(client, TWO_SUBWOOFERS)
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


async def test_subwoofer_levels_are_created_from_what_the_receiver_reports(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """One entity per subwoofer the receiver reports."""
    _reporting(client, TWO_SUBWOOFERS)
    entry = await setup_denonavr(hass)

    assert _subwoofer_unique_ids(entity_registry, entry.entry_id) == [
        f"{TEST_UNIQUE_ID}-subwoofer_level_1",
        f"{TEST_UNIQUE_ID}-subwoofer_level_2",
    ]


async def test_a_subwoofer_appearing_later_gets_an_entity(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The readable set follows what is playing, so it can't be built at setup.

    A receiver idle at setup reports nothing at all and only names its
    subwoofers once audio is actually playing.
    """
    entry = await setup_denonavr(hass)
    assert (
        entity_registry.async_get_entity_id(
            NUMBER_DOMAIN, DOMAIN, f"{TEST_UNIQUE_ID}-subwoofer_level_1"
        )
        is None
    )

    _reporting(client, ONE_SUBWOOFER)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(
        get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    )
    assert state
    assert state.state == "0.0"


async def test_a_subwoofer_is_not_added_twice(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """Every refresh reports the same subwoofer again; only the first adds it."""
    _reporting(client, ONE_SUBWOOFER)
    entry = await setup_denonavr(hass)

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _subwoofer_unique_ids(entity_registry, entry.entry_id) == [
        f"{TEST_UNIQUE_ID}-subwoofer_level_1"
    ]


async def test_a_subwoofer_dropping_out_stays_unknown_rather_than_disappearing(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """Entities are never removed - the readable set moves with the source."""
    _reporting(client, ONE_SUBWOOFER)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")

    _reporting(client, {})
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_subwoofer_level_unavailable_when_not_adjustable(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """A shut gate is unavailable, not a confident 0.0 dB.

    GetSubwooferLevel's status closes whenever no signal is present or
    subwoofer output is off, and the receiver then neither reports a
    level nor accepts one.
    """
    _reporting(client, ONE_SUBWOOFER)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    assert hass.states.get(entity_id).state == "0.0"

    client.subwoofer_level_status = False
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def test_setting_a_subwoofer_level_that_is_refused_raises(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The receiver acknowledges a refused write, so the library's raise must surface.

    It answers OK, returns 200 and echoes the unchanged level back on
    telnet - traffic a client watching for a push reads as success.
    """
    _reporting(client, ONE_SUBWOOFER)
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    client.async_set_subwoofer_level.side_effect = AvrCommandError(
        "not adjustable", "PSSWL"
    )

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 3},
            blocking=True,
        )

    assert err.value.translation_key == "set_failed"
    assert (
        str(err.value)
        == f"Setting {entity_id} to 3.0 dB failed on {TEST_HOST}: not adjustable"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(3, 3.0, id="whole_decibel"),
        pytest.param(-1.5, -1.5, id="half_decibel"),
        pytest.param(2.4, 2.5, id="rounded_to_the_nearest_half_decibel"),
    ],
)
async def test_set_subwoofer_level(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    value: float,
    expected: float,
) -> None:
    """Setting a level sends the named subwoofer and a value on the receiver's scale.

    denonavr rejects anything off the half-decibel grid outright, so an
    in-between value is rounded rather than refused.
    """
    _reporting(client, ONE_SUBWOOFER)
    await setup_denonavr(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: get_entity_id(
                entity_registry, NUMBER_DOMAIN, "subwoofer_level_1"
            ),
            ATTR_VALUE: value,
        },
        blocking=True,
    )

    client.async_set_subwoofer_level.assert_awaited_once_with("Subwoofer", expected)
