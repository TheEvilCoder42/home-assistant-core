"""The tests for the denonavr switch platform."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError, AvrNetworkError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    PENDING_VALUE_TIMEOUT,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import TEST_HOST, get_entity_id, setup_denonavr, wait_for_debounced_refresh

from tests.common import async_fire_time_changed, snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


@pytest.mark.usefixtures("client")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the switch entities and their registry entries."""
    with patch("homeassistant.components.denonavr.PLATFORMS", [Platform.SWITCH]):
        entry = await setup_denonavr(hass)

    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_dynamic_eq_unavailable_when_unknown(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Test the switch is unavailable when the receiver reports no Dynamic EQ state."""
    client.dynamic_eq = None
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    state = hass.states.get(entity_id)
    assert state
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("multi_eq", "expected"),
    [
        pytest.param("Off", STATE_UNAVAILABLE, id="multeq_off"),
        pytest.param("Flat", STATE_OFF, id="multeq_on"),
        pytest.param(None, STATE_OFF, id="multeq_unknown"),
    ],
)
async def test_dynamic_eq_follows_multeq(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    multi_eq: str | None,
    expected: str,
) -> None:
    """Dynamic EQ is unavailable while MultEQ is Off.

    The receiver drops a command to turn it on there. An unknown MultEQ
    leaves the switch available.
    """
    client.dynamic_eq = False
    client.multi_eq = multi_eq
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state == expected


@pytest.mark.parametrize("sound_mode", ["DIRECT", "PURE DIRECT"])
async def test_dynamic_eq_follows_direct_through_a_status_refresh(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    sound_mode: str,
) -> None:
    """Dynamic EQ is unavailable in Direct, seen by a status refresh alone.

    Direct bypasses Audyssey and the receiver drops the command there. The
    sound mode is a status value and the Audyssey coordinator does not
    refresh here, so the switch has to follow the status coordinator too.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state == STATE_ON
    settings_reads = client.async_update_settings.await_count

    client.sound_mode = sound_mode
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.sound_mode = "STEREO"
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    assert client.async_update_settings.await_count == settings_reads


@pytest.mark.parametrize(
    ("service", "command"),
    [
        pytest.param(SERVICE_TURN_ON, "async_dynamic_eq_on", id="turn_on"),
        pytest.param(SERVICE_TURN_OFF, "async_dynamic_eq_off", id="turn_off"),
    ],
)
async def test_turn_dynamic_eq_on_and_off(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    service: str,
    command: str,
) -> None:
    """Test turning Dynamic EQ on and off."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    getattr(client, command).assert_awaited_once()


@pytest.mark.parametrize(
    ("error", "available", "error_message"),
    [
        pytest.param(
            AvrCommandError("Could not set DynamicEQ", "SetAudyssey"),
            True,
            "Could not set DynamicEQ",
            id="rejected_command",
        ),
        pytest.param(
            AvrNetworkError("Connection refused", "SetAudyssey"),
            False,
            "Connection refused",
            id="unreachable_receiver",
        ),
    ],
)
async def test_turn_on_raises_on_avr_error(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    error: Exception,
    available: bool,
    error_message: str,
) -> None:
    """A receiver error while toggling is surfaced to the user.

    A rejected command says nothing about whether the receiver is reachable.
    A connectivity failure does, and it says it about both coordinators
    rather than only the one this entity happens to sit on.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    client.async_dynamic_eq_on.side_effect = error

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    # The rendered message keeps the placeholders and strings.json in step,
    # and shows the error's message rather than its whole args tuple.
    assert err.value.translation_key == "set_failed"
    assert (
        str(err.value)
        == f"Setting {entity_id} to on failed on {TEST_HOST}: {error_message}"
    )
    assert entry.runtime_data.coordinator.last_update_success is available
    assert entry.runtime_data.settings_coordinator.last_update_success is available


@pytest.mark.parametrize(
    ("dynamic_eq", "switch_state", "select_state"),
    [
        pytest.param(True, STATE_ON, "0db", id="dynamic_eq_on"),
        pytest.param(False, STATE_OFF, STATE_UNAVAILABLE, id="dynamic_eq_off"),
    ],
)
async def test_reference_level_offset_agrees_with_switch_at_setup(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    dynamic_eq: bool,
    switch_state: str,
    select_state: str,
) -> None:
    """Select and switch agree on Dynamic EQ state at setup time."""
    client.dynamic_eq = dynamic_eq
    client.reference_level_offset = "0dB"
    await setup_denonavr(hass)

    switch_entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    select_entity_id = get_entity_id(
        entity_registry, SELECT_DOMAIN, "reference_level_offset"
    )
    assert hass.states.get(switch_entity_id).state == switch_state
    assert hass.states.get(select_entity_id).state == select_state


async def test_toggling_switch_updates_dependent_select(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Toggling Audyssey Dynamic EQ off also updates Audyssey reference level offset.

    Both entities share the settings coordinator, so the refresh confirming
    the switch's action notifies the select too.
    """
    client.reference_level_offset = "0dB"
    await setup_denonavr(hass)

    switch_entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    select_entity_id = get_entity_id(
        entity_registry, SELECT_DOMAIN, "reference_level_offset"
    )
    assert hass.states.get(select_entity_id).state != STATE_UNAVAILABLE

    async def _turn_off(*args: object, **kwargs: object) -> None:
        client.dynamic_eq = False

    client.async_dynamic_eq_off.side_effect = _turn_off

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: switch_entity_id},
        blocking=True,
    )
    await wait_for_debounced_refresh(hass)

    assert hass.states.get(switch_entity_id).state == STATE_OFF
    assert hass.states.get(select_entity_id).state == STATE_UNAVAILABLE


async def test_turn_on_shows_state_immediately_without_polling(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Test the switch reflects the new state right after the call.

    Not only once its own poll cycle happens to fire.
    """
    client.dynamic_eq = False
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state == STATE_OFF

    async def _turn_on(*args: object, **kwargs: object) -> None:
        client.dynamic_eq = True

    client.async_dynamic_eq_on.side_effect = _turn_on

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    # No freezer/async_fire_time_changed needed - the state must already
    # be correct as soon as the service call returns.
    assert hass.states.get(entity_id).state == STATE_ON


async def test_turn_on_always_refreshes_settings_after_change(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Dynamic EQ refreshes Audyssey data regardless of the option.

    "Update audio settings periodically" governs the recurring poll alone -
    an action that just changed Audyssey data still confirms itself.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    # Setup already does one initial settings fetch.
    baseline_calls = client.async_update_settings.await_count

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await wait_for_debounced_refresh(hass)

    # Just one call, since this is the only entity acting - HA's own
    # post-service-call poll doesn't apply here (should_poll=False).
    assert client.async_update_settings.await_count == baseline_calls + 1


async def test_rapid_toggles_do_not_race(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Two turn_on/turn_off calls fired back-to-back must not race.

    PARALLEL_UPDATES only serializes a call targeting several entities at
    once, so two calls to this one entity are left to entity.py's lock.
    """
    call_order = []

    async def _slow_on(*args: object, **kwargs: object) -> None:
        call_order.append("start-on")
        await asyncio.sleep(0.05)
        client.dynamic_eq = True
        call_order.append("end-on")

    async def _slow_off(*args: object, **kwargs: object) -> None:
        call_order.append("start-off")
        await asyncio.sleep(0.05)
        client.dynamic_eq = False
        call_order.append("end-off")

    client.async_dynamic_eq_on.side_effect = _slow_on
    client.async_dynamic_eq_off.side_effect = _slow_off

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    # gather starts the calls in order, and the lock is FIFO.
    await asyncio.gather(
        hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
        ),
        hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        ),
    )

    assert call_order == ["start-on", "end-on", "start-off", "end-off"]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_state_shown_immediately_even_if_refresh_reads_back_stale_value(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A stale confirmation refresh must not revert a just-set state."""
    # Simulate the receiver's settings refresh responding with the old
    # value, as if the command hadn't internally settled yet.
    client.async_update_settings.side_effect = lambda *a, **k: None  # stays True

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    # Let the debounced confirmation refresh actually run its stale
    # read, rather than asserting before it's even had a chance to.
    await wait_for_debounced_refresh(hass)

    client.async_dynamic_eq_off.assert_awaited_once()
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_pending_state_expires_instead_of_masking_forever(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pending state must expire rather than mask reality forever."""
    client.async_update_settings.side_effect = lambda *a, **k: None  # stays True

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == STATE_OFF

    # The receiver never reports the new value, so the override has to
    # give way to what it does report.
    freezer.tick(timedelta(seconds=PENDING_VALUE_TIMEOUT + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_ON


async def test_telnet_push_keeps_a_pending_settings_refresh(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
) -> None:
    """A push must not cancel the refresh confirming a Dynamic EQ change.

    Dynamic EQ is receiver-wide and Telnet never pushes the other zones'
    copies, so that refresh is what brings them in step.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    baseline_calls = client.async_update_settings.await_count

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    fire_telnet_event("Main", "PS", "")
    await wait_for_debounced_refresh(hass)

    assert client.async_update_settings.await_count == baseline_calls + 1


async def test_auto_lip_sync_unavailable_when_unknown(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A receiver that reports no Auto lip sync state gives unavailable, not off.

    The receiver only reports it over Telnet or through GetAudioDelay,
    so "no value yet" has to be distinguishable from "off".
    """
    client.auto_lip_sync = None
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "auto_lip_sync")
    state = hass.states.get(entity_id)
    assert state
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("service", "called", "not_called"),
    [
        pytest.param(
            SERVICE_TURN_ON,
            "async_auto_lip_sync_on",
            "async_auto_lip_sync_off",
            id="turn_on",
        ),
        pytest.param(
            SERVICE_TURN_OFF,
            "async_auto_lip_sync_off",
            "async_auto_lip_sync_on",
            id="turn_off",
        ),
    ],
)
async def test_set_auto_lip_sync(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    service: str,
    called: str,
    not_called: str,
) -> None:
    """Each direction sends its own command, never the toggle.

    async_auto_lip_sync_toggle() inverts the last value read, so it raises
    while that is unknown and repeats a change not yet read back.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "auto_lip_sync")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    getattr(client, called).assert_awaited_once()
    getattr(client, not_called).assert_not_awaited()
    client.async_auto_lip_sync_toggle.assert_not_awaited()


@pytest.mark.parametrize(
    ("subwoofer", "expected"),
    [
        pytest.param(True, STATE_ON, id="output_on"),
        pytest.param(False, STATE_OFF, id="output_off"),
        pytest.param(None, STATE_UNAVAILABLE, id="parameter_unreadable"),
    ],
)
async def test_subwoofer_state(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    subwoofer: bool | None,
    expected: str,
) -> None:
    """The subwoofer output reports the receiver's state, or nothing at all.

    None is the normal case rather than an edge one: as measured, the
    receiver answers the parameter only in Stereo without an LFE channel,
    so on an HTTP-only receiver this is unavailable for most of a film.
    Rendering that as "off" would invite a press that writes a state
    nobody asked for.
    """
    client.subwoofer = subwoofer
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, SWITCH_DOMAIN, "subwoofer"))
    assert state
    assert state.state == expected


@pytest.mark.parametrize(
    ("subwoofer_adjustable", "subwoofer"),
    [
        pytest.param(False, None, id="not_adjustable"),
        pytest.param(None, None, id="never_read"),
        pytest.param(False, True, id="not_adjustable_with_a_value"),
        pytest.param(None, True, id="never_read_with_a_value"),
    ],
)
async def test_subwoofer_unavailable_unless_adjustable(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    subwoofer_adjustable: bool | None,
    subwoofer: bool | None,
) -> None:
    """The subwoofer output is unavailable until the receiver confirms it adjustable.

    A value is not enough: Telnet reports the stored state even outside
    Stereo, where the receiver ignores a write.
    """
    client.subwoofer_adjustable = subwoofer_adjustable
    client.subwoofer = subwoofer
    await setup_denonavr(hass)

    state = hass.states.get(get_entity_id(entity_registry, SWITCH_DOMAIN, "subwoofer"))
    assert state
    assert state.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("service", "method"),
    [
        pytest.param(SERVICE_TURN_ON, "async_subwoofer_on", id="on"),
        pytest.param(SERVICE_TURN_OFF, "async_subwoofer_off", id="off"),
    ],
)
async def test_set_subwoofer_never_toggles(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    service: str,
    method: str,
) -> None:
    """Each direction sends its own command rather than a toggle.

    async_subwoofer_toggle() inverts the cached value, so a turn_on
    against a stale True would send OFF.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "subwoofer")

    await hass.services.async_call(
        SWITCH_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )

    getattr(client, method).assert_awaited_once()
    client.async_subwoofer_toggle.assert_not_awaited()
