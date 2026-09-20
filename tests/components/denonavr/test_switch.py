"""The tests for the denonavr switch platform."""

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError, AvrNetworkError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    PENDING_VALUE_TIMEOUT,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_MODEL,
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

from . import (
    TEST_HOST,
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_UNIQUE_ID,
    TEST_ZONE,
    get_entity_id,
)

from tests.common import MockConfigEntry, async_fire_time_changed, snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


@pytest.fixture(name="client")
def client_fixture() -> Generator[MagicMock]:
    """Patch of client library for tests."""
    with (
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR",
            autospec=True,
        ) as mock_client_class,
        patch("homeassistant.components.denonavr.config_flow.denonavr.async_discover"),
    ):
        mock_client_class.return_value.name = TEST_NAME
        mock_client_class.return_value.host = TEST_HOST
        mock_client_class.return_value.model_name = TEST_MODEL
        mock_client_class.return_value.serial_number = TEST_SERIALNUMBER
        mock_client_class.return_value.manufacturer = TEST_MANUFACTURER
        mock_client_class.return_value.receiver_type = TEST_RECEIVER_TYPE
        mock_client_class.return_value.zone = TEST_ZONE
        mock_client_class.return_value.input_func_list = []
        mock_client_class.return_value.sound_mode_list = []
        mock_client_class.return_value.zones = {"Main": mock_client_class.return_value}
        mock_client_class.return_value.telnet_connected = False
        mock_client_class.return_value.telnet_healthy = False
        mock_client_class.return_value.dynamic_eq = True
        yield mock_client_class.return_value


async def setup_denonavr(
    hass: HomeAssistant, options: dict | None = None
) -> MockConfigEntry:
    """Initialize the denonavr integration for tests."""
    entry_data = {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        # What the config flow names it; the device takes it as its name
        # when a test loads no media_player.
        title=TEST_NAME,
        unique_id=TEST_UNIQUE_ID,
        data=entry_data,
        options=options or {},
    )
    mock_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry


async def _wait_for_debounced_refresh(hass: HomeAssistant) -> None:
    """Let a coordinator's debounced confirmation refresh actually fire.

    async_block_till_done() alone returns before the debouncer's own task
    has run, so the sleep hands it the loop first.
    """
    await asyncio.sleep(0)
    await hass.async_block_till_done()


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
    audyssey_reads = client.async_update_audyssey.await_count

    client.sound_mode = sound_mode
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.sound_mode = "STEREO"
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_ON
    assert client.async_update_audyssey.await_count == audyssey_reads


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
    assert entry.runtime_data.audyssey_coordinator.last_update_success is available


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

    Both entities share the Audyssey coordinator, so the refresh confirming
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
    await _wait_for_debounced_refresh(hass)

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


async def test_turn_on_always_refreshes_audyssey_after_change(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Dynamic EQ refreshes Audyssey data regardless of the option.

    "Update Audyssey settings" governs the recurring poll alone - an
    action that just changed Audyssey data still confirms itself.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    entity_id = get_entity_id(entity_registry, SWITCH_DOMAIN, "dynamic_eq")

    # Setup already does one initial Audyssey fetch.
    baseline_calls = client.async_update_audyssey.await_count

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _wait_for_debounced_refresh(hass)

    # Just one call, since this is the only entity acting - HA's own
    # post-service-call poll doesn't apply here (should_poll=False).
    assert client.async_update_audyssey.await_count == baseline_calls + 1


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
    # Simulate the receiver's Audyssey refresh responding with the old
    # value, as if the command hadn't internally settled yet.
    client.async_update_audyssey.side_effect = lambda *a, **k: None  # stays True

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
    await _wait_for_debounced_refresh(hass)

    client.async_dynamic_eq_off.assert_awaited_once()
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_pending_state_expires_instead_of_masking_forever(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A pending state must expire rather than mask reality forever."""
    client.async_update_audyssey.side_effect = lambda *a, **k: None  # stays True

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


async def test_telnet_push_keeps_a_pending_audyssey_refresh(
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
    baseline_calls = client.async_update_audyssey.await_count

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    fire_telnet_event("Main", "PS", "")
    await _wait_for_debounced_refresh(hass)

    assert client.async_update_audyssey.await_count == baseline_calls + 1
