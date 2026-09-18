"""The tests for the denonavr switch platform."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import MagicMock, create_autospec, patch

from denonavr import DenonAVR
from denonavr.const import MAIN_ZONE, POWER_OFF, POWER_ON, POWER_STANDBY, ZONE2
from denonavr.exceptions import AvrCommandError, AvrNetworkError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    PENDING_VALUE_TIMEOUT,
)
from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from . import (
    TEST_HOST,
    TEST_ZONE,
    get_entity_id,
    setup_denonavr,
    wait_for_debounced_refresh,
)

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
    sound mode is a status value and the settings coordinator does not
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
    "power",
    [
        pytest.param(POWER_STANDBY, id="standby"),
        pytest.param(POWER_OFF, id="off"),
    ],
)
async def test_write_refused_while_the_receiver_is_off(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    power: str,
) -> None:
    """A write the receiver would drop is refused before anything is sent.

    It answers the write and changes nothing, so an optimistic state would
    show and snap back. The switch stays available: standby is where the
    receiver rests, and it still reports its settings there.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    client.power = power

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    assert err.value.translation_key == "receiver_off"
    assert (
        str(err.value)
        == f"Cannot change {entity_id} while the zone it applies to is off"
    )
    client.async_dynamic_eq_off.assert_not_awaited()
    assert hass.states.get(entity_id).state == STATE_ON


async def test_write_sent_while_the_power_is_unknown(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A power not read yet lets the write through."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    client.power = None

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    client.async_dynamic_eq_off.assert_awaited_once()
    assert hass.states.get(entity_id).state == STATE_OFF


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
    A connectivity failure does. It marks the status coordinator, and with
    Telnet down and no settings poll of its own the settings one follows.
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
    ("telnet_healthy", "state_after_failure"),
    [
        pytest.param(True, STATE_ON, id="telnet_healthy"),
        pytest.param(False, STATE_UNAVAILABLE, id="telnet_down"),
    ],
)
async def test_unreachable_receiver_marks_the_status_coordinator(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    telnet_healthy: bool,
    state_after_failure: str,
) -> None:
    """A command's connectivity failure reaches settings only through status.

    With Telnet healthy and no settings poll, nothing but a settings push
    would clear a failure marked on it directly, and HA skips calls to an
    unavailable entity, so the user could not even retry.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    await setup_denonavr(hass, {CONF_USE_TELNET: True})
    client.telnet_healthy = telnet_healthy
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")
    media_player_id = get_entity_id(entity_registry, MEDIA_PLAYER_DOMAIN, TEST_ZONE)
    client.async_dynamic_eq_on.side_effect = AvrNetworkError(
        "Connection refused", "SetAudyssey"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    assert hass.states.get(entity_id).state == state_after_failure
    assert hass.states.get(media_player_id).state == STATE_UNAVAILABLE

    # A failed status coordinator reads on its next poll, Telnet or not.
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_ON
    assert hass.states.get(media_player_id).state != STATE_UNAVAILABLE


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


async def test_removal_cancels_a_pending_state_expiry(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A removed entity's pending state must not expire into a receiver read."""
    client.async_update_settings.side_effect = lambda *a, **k: None  # stays True

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    # Flushes the action's own confirmation refresh, well short of the expiry.
    freezer.tick(timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    entity_registry.async_remove(entity_id)
    await hass.async_block_till_done()
    reads = client.async_update_settings.await_count

    freezer.tick(timedelta(seconds=PENDING_VALUE_TIMEOUT + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_settings.await_count == reads


@pytest.mark.parametrize(
    ("options", "side_effect", "expected_state"),
    [
        pytest.param(
            {CONF_USE_TELNET: True},
            AvrNetworkError("Network error", "test"),
            STATE_OFF,
            id="failed_without_a_poll",
        ),
        pytest.param(
            {CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: True},
            AvrNetworkError("Network error", "test"),
            STATE_UNAVAILABLE,
            id="failed_with_a_poll",
        ),
        pytest.param(
            {CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: True},
            None,
            STATE_OFF,
            id="available_with_a_poll",
        ),
    ],
)
async def test_telnet_push_clears_a_failure_only_without_a_poll(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
    options: dict[str, bool],
    side_effect: Exception | None,
    expected_state: str,
) -> None:
    """A failed poll reads, and is better evidence than a push.

    A push clearing that failure would make the poll skip, hiding a
    settings-only HTTP failure behind stale values. It still carries the
    new value.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.async_update_settings.side_effect = side_effect
    await setup_denonavr(hass, options)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    client.dynamic_eq = False
    fire_telnet_event("Main", "PS", "DYNEQ OFF")
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == expected_state


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
    ("key", "command"),
    [pytest.param("auto_lip_sync", "async_auto_lip_sync_off", id="auto_lip_sync")],
)
async def test_any_zone_setting_sent_with_only_zone2_on(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    key: str,
    command: str,
) -> None:
    """Sent with the main zone off: the receiver applies it while any zone is on."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, key)
    zone2 = create_autospec(DenonAVR, instance=True)
    zone2.power = POWER_ON
    client.zones = {MAIN_ZONE: client, ZONE2: zone2}
    client.power = POWER_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )

    getattr(client, command).assert_awaited_once()


@pytest.mark.parametrize(
    ("event", "parameter"),
    [
        pytest.param("OP", "ALSSET OFF", id="denon"),
        pytest.param("SS", "HOSALS OFF", id="marantz"),
    ],
)
async def test_auto_lip_sync_follows_its_telnet_push(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
    event: str,
    parameter: str,
) -> None:
    """A change made on the receiver shows while Telnet is healthy.

    It arrives on OP or SS, which notify the status coordinator rather than
    the settings one the switch reads with.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.auto_lip_sync = True
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "auto_lip_sync")
    assert hass.states.get(entity_id).state == STATE_ON

    client.auto_lip_sync = False
    fire_telnet_event("Main", event, parameter)

    assert hass.states.get(entity_id).state == STATE_OFF
