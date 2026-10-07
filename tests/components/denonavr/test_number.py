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
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from . import TEST_HOST, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
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
    ("key", "method", "value", "expected", "state"),
    [
        pytest.param("bass", "async_set_bass", 4, 10, "4", id="bass_above_zero"),
        pytest.param("bass", "async_set_bass", -6, 0, "-6", id="bass_at_the_minimum"),
        pytest.param("treble", "async_set_treble", -4, 2, "-4", id="treble_below_zero"),
        pytest.param("treble", "async_set_treble", 2.6, 9, "3", id="treble_rounded"),
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
    state: str,
) -> None:
    """Setting bass or treble converts the dB back to the raw 0..12 scale.

    The state shows the value sent until the receiver reports it back on
    its raw scale, which releases it to follow the receiver again.
    """
    client.dynamic_eq = False
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    getattr(client, method).assert_awaited_once_with(expected)
    assert hass.states.get(entity_id).state == state

    setattr(client, key, expected)
    await entry.runtime_data.coordinator.async_refresh()
    setattr(client, key, 3)
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "-3"


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


@pytest.mark.parametrize(
    ("key", "setter", "attribute", "value", "expected", "readback"),
    [
        pytest.param(
            "audio_delay",
            "async_delay",
            "audio_delay",
            200,
            200,
            180,
            id="audio_delay_whole",
        ),
        pytest.param(
            "audio_delay",
            "async_delay",
            "audio_delay",
            199.6,
            200,
            180,
            id="audio_delay_rounded",
        ),
        pytest.param("lfe_level", "async_lfe", "lfe", -5, -5, -3, id="lfe_level_whole"),
        pytest.param(
            "lfe_level", "async_lfe", "lfe", -4.6, -5, -3, id="lfe_level_rounded"
        ),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_set_value(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    key: str,
    setter: str,
    attribute: str,
    value: float,
    expected: int,
    readback: int,
) -> None:
    """Setting a value sends a whole step as an int, which is all the receiver takes.

    The library formats the LFE level with str().zfill(), so a float would
    reach the receiver as PSLFE 5.0 - accepted with a 200 and silently
    ignored. The state shows the value sent until the receiver reports it
    back, and only an exact match releases it to follow the receiver again.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    set_fn = getattr(client, setter)
    set_fn.assert_awaited_once_with(expected)
    # assert_awaited_once_with() holds for 200.0 too.
    assert type(set_fn.await_args.args[0]) is int
    assert hass.states.get(entity_id).state == str(expected)

    setattr(client, attribute, expected)
    await entry.runtime_data.settings_coordinator.async_refresh()
    setattr(client, attribute, readback)
    await entry.runtime_data.settings_coordinator.async_refresh()
    assert hass.states.get(entity_id).state == str(readback)


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
