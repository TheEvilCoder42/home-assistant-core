"""The tests for the denonavr number platform."""

from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError
import pytest
from syrupy.assertion import SnapshotAssertion

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

from . import TEST_HOST, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


@pytest.mark.usefixtures("client")
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
    """Setting the audio delay sends whole milliseconds, which is all PSDELAY takes.

    The state shows the value sent until the receiver reports it back, and
    only an exact match releases it to follow the receiver again.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "audio_delay")

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    client.async_delay.assert_awaited_once_with(expected)
    assert hass.states.get(entity_id).state == str(expected)

    client.audio_delay = expected
    await entry.runtime_data.settings_coordinator.async_refresh()
    client.audio_delay = 180
    await entry.runtime_data.settings_coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "180"


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
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 49.6},
            blocking=True,
        )

    assert err.value.translation_key == "set_failed"
    assert (
        str(err.value)
        == f"Setting {entity_id} to 50 ms failed on {TEST_HOST}: Command rejected"
    )


async def test_audio_delay_none_reads_unknown_not_unavailable(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """A missing audio delay reads unknown, as DenonAvrNumber.available allows.

    denonavr drops the value on an input source change, and the delay stays
    settable while it is missing.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "audio_delay")
    assert hass.states.get(entity_id).state == "140"

    client.audio_delay = None
    await async_update_entity(hass, entity_id)

    assert hass.states.get(entity_id).state == STATE_UNKNOWN
