"""The tests for the denonavr number platform."""

from collections.abc import Callable
import logging
from unittest.mock import MagicMock, patch

from denonavr.const import CHANNEL_MAP
from denonavr.exceptions import AvrCommandError, AvrNetworkError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import DOMAIN, SETTLED_REFRESH_DELAY
from homeassistant.components.denonavr.number import _channel_level_description
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
from homeassistant.helpers.translation import async_get_translations

from . import TEST_HOST, TEST_UNIQUE_ID, advance_time, get_entity_id, setup_denonavr

from tests.common import snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


# The AVR-X1700H declares one subwoofer; TWO_SUBWOOFERS stands in for a
# model with more.
ONE_SUBWOOFER = {"Subwoofer": 0.0}


TWO_SUBWOOFERS = {"Subwoofer": 0.0, "Subwoofer 2": -1.5}


# Measured on the AVR-X1700H: stereo playing reports the front pair and
# the subwoofer, and a surround mode adds the centre channel on top.
STEREO_CHANNELS = {"Front Left": 0.0, "Front Right": -1.5, "Subwoofer": 2.0}


SURROUND_CHANNELS = STEREO_CHANNELS | {"Center": 1.0}


def _reporting(client: MagicMock, levels: dict[str, float]) -> None:
    """Make the receiver report exactly these subwoofer levels."""
    client.subwoofer_levels = levels
    client.subwoofer_level.side_effect = levels.get


def _reporting_channels(client: MagicMock, levels: dict[str, float]) -> None:
    """Make the receiver report exactly these channel levels."""
    client.channel_volumes = levels
    client.channel_volume.side_effect = levels.get


def _dynamic_unique_ids(
    entity_registry: er.EntityRegistry, entry_id: str, key_prefix: str
) -> list[str]:
    """Return the dynamic unique_ids with one key prefix, in registration order.

    Filtered rather than compared whole: other number entities register on
    the same entry.
    """
    return [
        registry_entry.unique_id
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry_id
        )
        if registry_entry.domain == NUMBER_DOMAIN
        and registry_entry.unique_id.startswith(f"{TEST_UNIQUE_ID}-{key_prefix}")
    ]


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
    # An idle receiver reports no levels, which would leave nothing to snapshot.
    _reporting(client, TWO_SUBWOOFERS)
    _reporting_channels(client, STEREO_CHANNELS)
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


@pytest.mark.parametrize(
    ("report", "levels", "prefix", "keys"),
    [
        pytest.param(
            _reporting,
            TWO_SUBWOOFERS,
            "subwoofer_level_",
            ["subwoofer_level_1", "subwoofer_level_2"],
            id="subwoofer",
        ),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            "channel_level_",
            [
                "channel_level_front_left",
                "channel_level_front_right",
                "channel_level_subwoofer",
            ],
            id="channel",
        ),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_levels_are_created_from_what_the_receiver_reports(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    report: Callable[[MagicMock, dict[str, float]], None],
    levels: dict[str, float],
    prefix: str,
    keys: list[str],
) -> None:
    """One entity per name the receiver reports, and none for the rest.

    denonavr knows 34 channel names; a 3.1 receiver playing stereo
    reports three of them.
    """
    report(client, levels)
    entry = await setup_denonavr(hass)

    assert _dynamic_unique_ids(entity_registry, entry.entry_id, prefix) == [
        f"{TEST_UNIQUE_ID}-{key}" for key in keys
    ]


@pytest.mark.parametrize(
    ("report", "initial", "later", "key", "state"),
    [
        pytest.param(
            _reporting,
            {},
            ONE_SUBWOOFER,
            "subwoofer_level_1",
            "0.0",
            id="subwoofer",
        ),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            SURROUND_CHANNELS,
            "channel_level_center",
            "1.0",
            id="channel",
        ),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_level_appearing_later_gets_an_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    report: Callable[[MagicMock, dict[str, float]], None],
    initial: dict[str, float],
    later: dict[str, float],
    key: str,
    state: str,
) -> None:
    """The readable set follows what is playing, so it can't be built at setup.

    A receiver idle at setup reports no subwoofer at all, and switching from
    stereo to a surround mode is what adds the centre channel; nothing about
    the receiver's speaker layout announces either beforehand.
    """
    report(client, initial)
    entry = await setup_denonavr(hass)
    assert (
        entity_registry.async_get_entity_id(
            NUMBER_DOMAIN, DOMAIN, f"{TEST_UNIQUE_ID}-{key}"
        )
        is None
    )

    report(client, later)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    entity = hass.states.get(get_entity_id(entity_registry, NUMBER_DOMAIN, key))
    assert entity
    assert entity.state == state


@pytest.mark.parametrize(
    ("report", "levels", "prefix", "keys"),
    [
        pytest.param(
            _reporting,
            ONE_SUBWOOFER,
            "subwoofer_level_",
            ["subwoofer_level_1"],
            id="subwoofer",
        ),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            "channel_level_",
            [
                "channel_level_front_left",
                "channel_level_front_right",
                "channel_level_subwoofer",
            ],
            id="channel",
        ),
    ],
)
# Enabled: a second add of a disabled entity is dropped without an error.
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_level_is_not_added_twice(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    caplog: pytest.LogCaptureFixture,
    report: Callable[[MagicMock, dict[str, float]], None],
    levels: dict[str, float],
    prefix: str,
    keys: list[str],
) -> None:
    """Every refresh reports the same names again; only the first adds them.

    The registry holds one entry each either way: a second add is dropped,
    and only the error logged for it tells.
    """
    report(client, levels)
    entry = await setup_denonavr(hass)

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _dynamic_unique_ids(entity_registry, entry.entry_id, prefix) == [
        f"{TEST_UNIQUE_ID}-{key}" for key in keys
    ]
    assert all(record.levelno < logging.ERROR for record in caplog.records)


@pytest.mark.parametrize(
    ("report", "levels", "key"),
    [
        pytest.param(_reporting, ONE_SUBWOOFER, "subwoofer_level_1", id="subwoofer"),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            "channel_level_front_left",
            id="channel",
        ),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_level_dropping_out_stays_unknown_rather_than_disappearing(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    report: Callable[[MagicMock, dict[str, float]], None],
    levels: dict[str, float],
    key: str,
) -> None:
    """Entities are never removed - the readable set moves with what plays.

    The channel set empties whenever playback stops, and the receiver then
    drops a write without an error, so unknown is the honest state.
    """
    report(client, levels)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)

    report(client, {})
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


@pytest.mark.parametrize(
    ("report", "levels", "key", "setter"),
    [
        pytest.param(
            _reporting,
            ONE_SUBWOOFER,
            "subwoofer_level_1",
            "async_set_subwoofer_level",
            id="subwoofer",
        ),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            "channel_level_front_left",
            "async_channel_volume",
            id="channel",
        ),
    ],
)
@pytest.mark.parametrize(
    ("error", "available", "error_message"),
    [
        pytest.param(
            AvrCommandError("refused", "CV"), True, "refused", id="rejected_command"
        ),
        pytest.param(
            AvrNetworkError("Connection refused", "CV"),
            False,
            "Connection refused",
            id="unreachable_receiver",
        ),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_set_level_raises_on_avr_error(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    report: Callable[[MagicMock, dict[str, float]], None],
    levels: dict[str, float],
    key: str,
    setter: str,
    error: Exception,
    available: bool,
    error_message: str,
) -> None:
    """A receiver error surfaces rather than leaving an optimistic value.

    A refused write is acknowledged by the receiver, which echoes the
    unchanged level back, so the library's raise must reach the user. A
    connectivity failure marks the status coordinator these entities are
    on, and the settings one follows it while Telnet is down and settings
    does not poll.
    """
    report(client, levels)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)
    getattr(client, setter).side_effect = error

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
        == f"Setting {entity_id} to 3.0 dB failed on {TEST_HOST}: {error_message}"
    )
    assert entry.runtime_data.coordinator.last_update_success is available
    assert entry.runtime_data.settings_coordinator.last_update_success is available


@pytest.mark.parametrize(
    ("report", "levels", "key", "setter", "name"),
    [
        pytest.param(
            _reporting,
            ONE_SUBWOOFER,
            "subwoofer_level_1",
            "async_set_subwoofer_level",
            "Subwoofer",
            id="subwoofer",
        ),
        pytest.param(
            _reporting_channels,
            STEREO_CHANNELS,
            "channel_level_front_left",
            "async_channel_volume",
            "Front Left",
            id="channel",
        ),
    ],
)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(3, 3.0, id="whole_decibel"),
        pytest.param(-1.5, -1.5, id="half_decibel"),
        pytest.param(3.3, 3.5, id="rounded_to_the_nearest_half_decibel"),
    ],
)
@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_set_level(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    client: MagicMock,
    report: Callable[[MagicMock, dict[str, float]], None],
    levels: dict[str, float],
    key: str,
    setter: str,
    name: str,
    value: float,
    expected: float,
) -> None:
    """Setting a level sends the entity's own name and a value in dB.

    denonavr rejects anything off the half-decibel grid outright, so an
    in-between value is rounded rather than refused. The state shows the
    rounded value until the receiver reports it back.
    """
    report(client, levels)
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, key)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )

    getattr(client, setter).assert_awaited_once_with(name, expected)
    assert hass.states.get(entity_id).state == str(expected)

    report(client, levels | {name: expected})
    await entry.runtime_data.coordinator.async_refresh()
    report(client, levels | {name: -2.0})
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "-2.0"


async def test_a_channel_appearing_later_is_registered_disabled(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, client: MagicMock
) -> None:
    """A newly reported channel is registered, disabled, and gets no state.

    Channel levels are disabled by default, so the listener's add must still
    create the registry entry for the user to enable.
    """
    _reporting_channels(client, STEREO_CHANNELS)
    entry = await setup_denonavr(hass)

    _reporting_channels(client, SURROUND_CHANNELS)
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = get_entity_id(entity_registry, NUMBER_DOMAIN, "channel_level_center")
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry
    assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def test_every_channel_has_a_translated_name(hass: HomeAssistant) -> None:
    """Each channel denonavr maps has its own name in strings.json.

    A missing one would leave the entity nameless, and the snapshots only
    cover the channels a test receiver reports.
    """
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])

    assert [
        channel
        for channel in CHANNEL_MAP.values()
        if f"component.{DOMAIN}.entity.number.{_channel_level_description(channel).key}.name"
        not in translations
    ] == []
