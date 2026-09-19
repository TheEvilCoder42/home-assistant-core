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
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.typing import UNDEFINED

from . import TEST_HOST, TEST_UNIQUE_ID, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


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


@pytest.mark.usefixtures("client")
async def test_lfe_level_disabled_by_default(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """The LFE attenuation is registered, but disabled until the user enables it."""
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "lfe_level")
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(-5, -5, id="whole_decibel"),
        pytest.param(-4.6, -5, id="rounded_to_the_nearest_decibel"),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_set_lfe_level(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    value: float,
    expected: int,
) -> None:
    """Setting the LFE attenuation sends a whole, signed decibel.

    async_lfe formats with str().zfill(), so a float would reach the
    receiver as PSLFE 5.0 - accepted with a 200 and silently ignored.
    """
    await setup_denonavr(hass)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {
            ATTR_ENTITY_ID: get_entity_id(entity_registry, NUMBER_DOMAIN, "lfe_level"),
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
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
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
                ATTR_ENTITY_ID: get_entity_id(
                    entity_registry, NUMBER_DOMAIN, "lfe_level"
                ),
                ATTR_VALUE: value,
            },
            blocking=True,
        )

    client.async_lfe.assert_not_awaited()


@pytest.mark.parametrize(
    ("lfe_adjustable", "lfe"),
    [
        pytest.param(False, None, id="not_adjustable"),
        pytest.param(None, None, id="never_read"),
        pytest.param(False, -2, id="not_adjustable_with_a_value"),
        pytest.param(None, -2, id="never_read_with_a_value"),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_lfe_level_unavailable_unless_adjustable(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    lfe_adjustable: bool | None,
    lfe: int | None,
) -> None:
    """The LFE attenuation is unavailable until the receiver confirms it adjustable.

    A value is not enough: Telnet reports the stored level even while the
    stream has no LFE channel, when the receiver ignores a write.
    """
    client.lfe_adjustable = lfe_adjustable
    client.lfe = lfe
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, NUMBER_DOMAIN, "lfe_level"))
    assert state
    assert state.state == STATE_UNAVAILABLE
