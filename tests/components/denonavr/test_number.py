"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

from denonavr.const import MAIN_ZONE, ZONE2, ZONE3
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import CONF_ZONE2, CONF_ZONE3
from homeassistant.components.denonavr.number import (
    NUMBER_TYPES,
    DenonAvrNumberEntityDescription,
)
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_STEP,
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import UNDEFINED

from . import TEST_UNIQUE_ID, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

# One per configured zone, from a description built per zone rather
# than from NUMBER_TYPES.
ZONE_NUMBER_KEYS = ("volume",)


async def _set_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Set a number entity's value through its action."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )


@pytest.mark.usefixtures("client", "entity_registry_enabled_by_default")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the number entities and their registry entries."""
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
    """The platform registers the static descriptions plus one per zone."""
    entry = await setup_denonavr(hass)

    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert {
        registry_entry.unique_id
        for registry_entry in entries
        if registry_entry.domain == NUMBER_DOMAIN
    } == {f"{TEST_UNIQUE_ID}-{description.key}" for description in NUMBER_TYPES} | {
        f"{TEST_UNIQUE_ID}-{MAIN_ZONE}-{key}" for key in ZONE_NUMBER_KEYS
    }


@pytest.mark.parametrize(
    ("max_volume", "expected"),
    [
        pytest.param(18.0, 18.0, id="no_limit"),
        pytest.param(-20.0, -20.0, id="limited"),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_volume_ceiling_is_the_configured_limit(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    max_volume: float,
    expected: float,
) -> None:
    """The top of the range is the limit, or the hardware maximum without one."""
    client.max_volume = max_volume
    await setup_denonavr(hass)

    state = hass.states.get(
        get_entity_id(entity_registry, NUMBER_DOMAIN, "Main-volume")
    )
    assert state.state == "-40.0"
    assert state.attributes[ATTR_MAX] == expected


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_volume_ceiling_follows_a_limit_change(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """The ceiling tracks the limit rather than being fixed at setup."""
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "Main-volume")
    assert hass.states.get(entity_id).attributes[ATTR_MAX] == 18.0

    client.max_volume = -10.0
    entry.runtime_data.coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).attributes[ATTR_MAX] == -10.0


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        pytest.param(MAIN_ZONE, 0.5, id="main"),
        pytest.param(ZONE2, 1.0, id="zone2"),
        pytest.param(ZONE3, 1.0, id="zone3"),
    ],
)
@pytest.mark.usefixtures(
    "zone2_client", "zone3_client", "entity_registry_enabled_by_default"
)
async def test_volume_step_follows_the_zone(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    zone: str,
    expected: float,
) -> None:
    """The main zone moves in half steps, the secondary zones in whole decibels."""
    await setup_denonavr(hass, {CONF_ZONE2: True, CONF_ZONE3: True})

    state = hass.states.get(
        get_entity_id(entity_registry, NUMBER_DOMAIN, f"{zone}-volume")
    )
    assert state.attributes[ATTR_STEP] == expected


@pytest.mark.parametrize(
    ("zone", "value", "expected"),
    [
        pytest.param(MAIN_ZONE, -47.5, -47.5, id="main_half_step"),
        pytest.param(MAIN_ZONE, -47.3, -47.5, id="main_off_step"),
        pytest.param(ZONE2, -30.0, -30.0, id="zone2_whole_step"),
        pytest.param(ZONE2, -20.5, -20.0, id="zone2_half_step"),
        pytest.param(ZONE3, -20.7, -21.0, id="zone3_off_step"),
    ],
)
@pytest.mark.usefixtures(
    "zone2_client", "zone3_client", "entity_registry_enabled_by_default"
)
async def test_set_volume_rounds_to_the_zone_step(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    zone: str,
    value: float,
    expected: float,
) -> None:
    """A volume is sent on its own zone's step, to that zone's receiver object.

    The action does not enforce the step, and a secondary zone given a half
    step lands 0.5 dB low over Telnet and ignores it over HTTP.
    """
    await setup_denonavr(hass, {CONF_ZONE2: True, CONF_ZONE3: True})

    await _set_value(
        hass, get_entity_id(entity_registry, NUMBER_DOMAIN, f"{zone}-volume"), value
    )

    client.zones[zone].async_set_volume.assert_awaited_once_with(expected)


@pytest.mark.usefixtures("client", "zone2_client")
async def test_zone_volume_names_carry_the_zone(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """A zone's volume is named after it; the main zone's is not.

    The registry's own name rather than the friendly one, which also
    carries the device name.
    """
    await setup_denonavr(hass, {CONF_ZONE2: True})

    main = entity_registry.async_get(
        get_entity_id(entity_registry, NUMBER_DOMAIN, "Main-volume")
    )
    zone2 = entity_registry.async_get(
        get_entity_id(entity_registry, NUMBER_DOMAIN, "Zone2-volume")
    )
    assert main.original_name == "Volume"
    assert zone2.original_name == "Zone 2 volume"


@pytest.mark.parametrize(
    "zone",
    [
        pytest.param(MAIN_ZONE, id="main"),
        pytest.param(ZONE2, id="zone2"),
    ],
)
@pytest.mark.usefixtures("client", "zone2_client")
async def test_volume_disabled_by_default(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, zone: str
) -> None:
    """Every zone's volume is registered but disabled, so it has no state."""
    await setup_denonavr(hass, {CONF_ZONE2: True})

    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, f"{zone}-volume")
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None
