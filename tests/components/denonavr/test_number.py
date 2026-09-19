"""The tests for the denonavr number platform."""

from collections.abc import Callable
from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import DOMAIN, SETTLED_REFRESH_DELAY
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from . import TEST_HOST, TEST_UNIQUE_ID, advance_time, get_entity_id, setup_denonavr

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


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
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

    The receiver closes GetSubwooferLevel's status whenever it will not
    take a level, and then reports none either.
    """
    _reporting(client, ONE_SUBWOOFER)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    assert hass.states.get(entity_id).state == "0.0"

    client.subwoofer_level_status = False
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


async def _setup_with_telnet(hass: HomeAssistant, client: MagicMock) -> None:
    """Set up under healthy Telnet, with one subwoofer and its gate open."""
    client.telnet_connected = True
    client.telnet_healthy = True
    _reporting(client, ONE_SUBWOOFER)
    await setup_denonavr(hass)


def _gate_closes_on_read(client: MagicMock) -> None:
    """Close the gate at the next status read, not before.

    Entities read the receiver live, so closing it outright would show at the
    next state write, read or not.
    """

    async def _read() -> None:
        client.subwoofer_level_status = False

    client.async_update.side_effect = _read


@pytest.mark.parametrize(
    ("event", "before", "after"),
    [
        pytest.param("SS", "INFAISSIG 02", "INFAISSIG 12", id="signal_code"),
        pytest.param(
            "OP",
            "INFINS 22222222111111111111",
            "INFINS 11111111111111111111",
            id="input_channel_map",
        ),
        pytest.param("PS", "SWR ON", "SWR OFF", id="subwoofer_output"),
        # Subwoofer output applies only in Stereo, and leaving Stereo pushes
        # no PSSWR.
        pytest.param("MS", "STEREO", "DOLBY AUDIO-DSUR", id="sound_mode"),
    ],
)
async def test_subwoofer_gate_push_rereads_status_once_settled(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
    event: str,
    before: str,
    after: str,
) -> None:
    """Under Telnet, a push that can move the gate reads it once the receiver settles.

    Only the status read reports the gate, and it is skipped while Telnet is
    healthy, so the settled read is forced. The first push after setup is a
    change too.
    """
    await _setup_with_telnet(hass, client)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    reads = client.async_update.await_count
    fire_telnet_event("Main", event, before)
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY)
    assert client.async_update.await_count == reads + 1

    _gate_closes_on_read(client)
    fire_telnet_event("Main", event, after)
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 1)
    assert client.async_update.await_count == reads + 1
    assert hass.states.get(entity_id).state == "0.0"

    await advance_time(hass, freezer, 1)
    assert client.async_update.await_count == reads + 2
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("zone", "event", "parameter"),
    [
        pytest.param("Main", "MS", "STEREO", id="identical_repeat"),
        pytest.param("Zone2", "MS", "DOLBY AUDIO-DSUR", id="zone2_sound_mode"),
        pytest.param("Main", "PS", "SWL 50", id="other_ps_parameter"),
        pytest.param("Main", "SS", "INFAISFSV 48K", id="other_ss_parameter"),
    ],
)
async def test_other_pushes_do_not_reread_the_subwoofer_gate(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    fire_telnet_event: Callable[[str, str, str], None],
    zone: str,
    event: str,
    parameter: str,
) -> None:
    """Only a main-zone gate push with a new value reads the gate."""
    await _setup_with_telnet(hass, client)
    fire_telnet_event("Main", "MS", "STEREO")
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY)
    reads = client.async_update.await_count

    fire_telnet_event(zone, event, parameter)
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY)

    assert client.async_update.await_count == reads


async def test_subwoofer_output_command_rereads_the_gate_under_telnet(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning Subwoofer output off reads the gate even with Telnet healthy.

    Through the settled status read every settings command requests, not
    through the PSSWR push that follows it.
    """
    await _setup_with_telnet(hass, client)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")
    reads = client.async_update.await_count
    _gate_closes_on_read(client)

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: get_entity_id(entity_registry, SWITCH_DOMAIN, "subwoofer")},
        blocking=True,
    )
    await advance_time(hass, freezer, SETTLED_REFRESH_DELAY - 1)
    assert hass.states.get(entity_id).state == "0.0"

    await advance_time(hass, freezer, 1)
    assert client.async_update.await_count == reads + 1
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
        pytest.param(3.3, 3.5, id="rounded_to_the_nearest_half_decibel"),
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
    in-between value is rounded rather than refused. The state shows the
    rounded value until the receiver reports it back.
    """
    _reporting(client, ONE_SUBWOOFER)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "subwoofer_level_1")

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    client.async_set_subwoofer_level.assert_awaited_once_with("Subwoofer", expected)
    assert hass.states.get(entity_id).state == str(expected)

    _reporting(client, {"Subwoofer": expected})
    await entry.runtime_data.coordinator.async_refresh()
    _reporting(client, {"Subwoofer": -2.0})
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "-2.0"
