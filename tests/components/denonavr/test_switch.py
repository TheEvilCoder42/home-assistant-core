"""The tests for the denonavr switch platform."""

import asyncio
from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError, AvrNetworkError
import pytest

from homeassistant.components.denonavr.config_flow import DOMAIN
from homeassistant.components.denonavr.const import CONF_UPDATE_AUDYSSEY
from homeassistant.components.denonavr.switch import (
    SWITCH_TYPES,
    DenonAvrSwitchEntityDescription,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import async_update_entity

from . import TEST_NAME, TEST_UNIQUE_ID, setup_denonavr

SWITCH_ENTITY_ID = f"{SWITCH_DOMAIN}.{TEST_NAME.lower()}_dynamic_eq"


@pytest.fixture(autouse=True)
def _fast_action_refresh_debounce():
    """Patch the action-refresh debounce cooldown down for every test.

    See the matching fixture/comment in test_select.py.
    """
    with patch(
        "homeassistant.components.denonavr.coordinator.ACTION_REFRESH_DEBOUNCE_COOLDOWN",
        0,
    ):
        yield


async def _wait_for_debounced_refresh(hass: HomeAssistant) -> None:
    """Let a coordinator's debounced confirmation refresh actually fire.

    See the matching helper/comment in test_select.py.
    """
    await asyncio.sleep(0)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    """Look up an entity_id by its unique_id suffix."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{TEST_UNIQUE_ID}-{key}")
    assert entity_id is not None
    return entity_id


@pytest.mark.parametrize(
    "description", SWITCH_TYPES, ids=lambda description: description.key
)
def test_description_names_itself_through_translations(
    description: DenonAvrSwitchEntityDescription,
) -> None:
    """Every description carries a translation key matching its entity key."""
    assert description.translation_key == description.key


def test_dynamic_eq_keeps_its_literal_fallback_name() -> None:
    """Dynamic EQ names itself literally as well, as a translation fallback.

    Unlike test_number.py's sweep this one can't assert no description
    has a name: dropping Dynamic EQ's would rename the entity.
    """
    description = next(
        description for description in SWITCH_TYPES if description.key == "dynamic_eq"
    )
    assert description.name == "Dynamic EQ"


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
        if registry_entry.domain == SWITCH_DOMAIN
    } == {f"{TEST_UNIQUE_ID}-{description.key}" for description in SWITCH_TYPES}


async def test_dynamic_eq_state_on(hass: HomeAssistant, client: MagicMock) -> None:
    """Test the switch reports on when Dynamic EQ is on."""
    await setup_denonavr(hass)

    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")
    state = hass.states.get(entity_id)
    assert state
    assert state.state == "on"


async def test_dynamic_eq_unavailable_when_unknown(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Test the switch is unavailable when the receiver reports no Dynamic EQ state."""
    client.dynamic_eq = None
    await setup_denonavr(hass)

    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")
    state = hass.states.get(entity_id)
    assert state
    assert state.state == STATE_UNAVAILABLE


async def test_turn_on_dynamic_eq(hass: HomeAssistant, client: MagicMock) -> None:
    """Test turning Dynamic EQ on."""
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    client.async_dynamic_eq_on.assert_awaited_once()


async def test_turn_off_dynamic_eq(hass: HomeAssistant, client: MagicMock) -> None:
    """Test turning Dynamic EQ off."""
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    client.async_dynamic_eq_off.assert_awaited_once()


async def test_turn_on_raises_on_avr_error(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Test that a receiver error while toggling is surfaced to the user."""
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    client.async_dynamic_eq_on.side_effect = AvrCommandError(
        "Could not set DynamicEQ", "SetAudyssey"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )


async def test_reference_level_offset_agrees_with_switch_at_setup(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Select and switch agree on Dynamic EQ state at setup time."""
    client.reference_level_offset = "0dB"
    client.reference_level_offset_setting_list = ["0dB", "+5dB", "+10dB", "+15dB"]
    client.dynamic_volume = "Off"
    client.dynamic_volume_setting_list = ["Off", "Light", "Medium", "Heavy"]
    client.multi_eq = "Reference"
    client.multi_eq_setting_list = ["Off", "Flat", "L/R Bypass", "Reference", "Manual"]
    client.eco_mode = "Auto"
    client.dimmer = "Bright"
    client.auto_standby = "OFF"

    # Dynamic EQ on at setup -> both entities agree it's usable.
    client.dynamic_eq = True
    await setup_denonavr(hass)

    reflevoffset_entity_id = _entity_id(hass, SELECT_DOMAIN, "reference_level_offset")
    switch_entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    assert hass.states.get(switch_entity_id).state == "on"
    assert hass.states.get(reflevoffset_entity_id).state != STATE_UNAVAILABLE


async def test_reference_level_offset_unavailable_at_setup_when_dynamic_eq_off(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Same cross-check, with Dynamic EQ off at setup instead."""
    client.reference_level_offset = "0dB"
    client.reference_level_offset_setting_list = ["0dB", "+5dB", "+10dB", "+15dB"]
    client.dynamic_volume = "Off"
    client.dynamic_volume_setting_list = ["Off", "Light", "Medium", "Heavy"]
    client.multi_eq = "Reference"
    client.multi_eq_setting_list = ["Off", "Flat", "L/R Bypass", "Reference", "Manual"]
    client.eco_mode = "Auto"
    client.dimmer = "Bright"
    client.auto_standby = "OFF"

    client.dynamic_eq = False
    await setup_denonavr(hass)

    reflevoffset_entity_id = _entity_id(hass, SELECT_DOMAIN, "reference_level_offset")
    switch_entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    assert hass.states.get(switch_entity_id).state == "off"
    assert hass.states.get(reflevoffset_entity_id).state == STATE_UNAVAILABLE


async def test_toggling_switch_updates_dependent_select(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Toggling Dynamic EQ off also updates Reference Level Offset.

    Both entities share the same Audyssey coordinator, so refreshing
    after the switch's own action notifies every entity subscribed to
    it, not just the switch itself.
    """
    client.reference_level_offset = "0dB"
    client.reference_level_offset_setting_list = ["0dB", "+5dB", "+10dB", "+15dB"]
    client.dynamic_volume = "Off"
    client.dynamic_volume_setting_list = ["Off", "Light", "Medium", "Heavy"]
    client.multi_eq = "Reference"
    client.multi_eq_setting_list = ["Off", "Flat", "L/R Bypass", "Reference", "Manual"]
    client.eco_mode = "Auto"
    client.dimmer = "Bright"
    client.auto_standby = "OFF"
    client.dynamic_eq = True

    await setup_denonavr(hass)

    reflevoffset_entity_id = _entity_id(hass, SELECT_DOMAIN, "reference_level_offset")
    switch_entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(reflevoffset_entity_id).state != STATE_UNAVAILABLE

    async def _turn_off(*args, **kwargs):
        client.dynamic_eq = False

    client.async_dynamic_eq_off.side_effect = _turn_off

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: switch_entity_id},
        blocking=True,
    )
    await _wait_for_debounced_refresh(hass)

    assert hass.states.get(switch_entity_id).state == "off"
    # The select updates too, from the very same refresh - no separate
    # poll or explicit cross-notification needed.
    assert hass.states.get(reflevoffset_entity_id).state == STATE_UNAVAILABLE


async def test_turn_on_shows_state_immediately_without_polling(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Test the switch reflects the new state right after the call.

    Not only once its own independent poll cycle happens to fire -
    this is the same class of bug reported for the Dimmer select
    entity.
    """
    client.dynamic_eq = False
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")
    assert hass.states.get(entity_id).state == "off"

    async def _turn_on(*args, **kwargs):
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
    assert hass.states.get(entity_id).state == "on"


async def test_turn_on_always_refreshes_audyssey_after_change(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Dynamic EQ refreshes Audyssey data regardless of the "Update Audyssey settings" option (see select.py for why)."""
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

    # Setup already does one initial Audyssey fetch.
    baseline_calls = client.async_update_settings.await_count

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await _wait_for_debounced_refresh(hass)

    # Just one call, since this is the only entity acting - HA's own
    # post-service-call poll doesn't apply here (should_poll=False).
    assert client.async_update_settings.await_count == baseline_calls + 1


async def test_rapid_toggles_do_not_race(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Two turn_on/turn_off calls fired back-to-back must not race.

    See the matching select.py test for why PARALLEL_UPDATES alone
    isn't enough here.
    """
    call_order = []

    async def _slow_on(*args, **kwargs):
        call_order.append("start-on")
        await asyncio.sleep(0.05)
        client.dynamic_eq = True
        call_order.append("end-on")

    async def _slow_off(*args, **kwargs):
        call_order.append("start-off")
        await asyncio.sleep(0.05)
        client.dynamic_eq = False
        call_order.append("end-off")

    client.async_dynamic_eq_on.side_effect = _slow_on
    client.async_dynamic_eq_off.side_effect = _slow_off

    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

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

    assert call_order in (
        ["start-on", "end-on", "start-off", "end-off"],
        ["start-off", "end-off", "start-on", "end-on"],
    )
    expected_state = "on" if client.dynamic_eq else "off"
    assert hass.states.get(entity_id).state == expected_state


async def test_state_shown_immediately_even_if_refresh_reads_back_stale_value(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A stale immediate refresh must not revert a just-set state."""
    # Simulate the receiver's Audyssey refresh responding with the old
    # value, as if the command hadn't internally settled yet.
    client.async_update_settings.side_effect = lambda *a, **k: None  # stays True

    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

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
    assert hass.states.get(entity_id).state == "off"


async def test_pending_state_expires_instead_of_masking_forever(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A pending state must expire rather than mask reality forever."""
    client.async_update_settings.side_effect = lambda *a, **k: None  # stays True

    with patch("homeassistant.components.denonavr.entity.PENDING_VALUE_TIMEOUT", 0.01):
        await setup_denonavr(hass)
        entity_id = _entity_id(hass, SWITCH_DOMAIN, "dynamic_eq")

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        assert hass.states.get(entity_id).state == "off"

        # Receiver actually ends up "on" via some other path, and enough
        # time passes (the patched timeout above is 10ms) that the
        # override should no longer be trusted.
        client.dynamic_eq = True
        await asyncio.sleep(0.02)
        await async_update_entity(hass, entity_id)

        assert hass.states.get(entity_id).state == "on"


async def test_auto_lip_sync_state_on(hass: HomeAssistant, client: MagicMock) -> None:
    """Test the switch reports on when Auto lip sync is on."""
    await setup_denonavr(hass)

    entity_id = _entity_id(hass, SWITCH_DOMAIN, "auto_lip_sync")
    state = hass.states.get(entity_id)
    assert state
    assert state.state == "on"


async def test_auto_lip_sync_unavailable_when_unknown(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A receiver that reports no Auto lip sync state gives unavailable, not off.

    The receiver only reports it over Telnet or through GetAudioDelay,
    so "no value yet" has to be distinguishable from "off".
    """
    client.auto_lip_sync = None
    await setup_denonavr(hass)

    entity_id = _entity_id(hass, SWITCH_DOMAIN, "auto_lip_sync")
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
    service: str,
    called: str,
    not_called: str,
) -> None:
    """Each direction sends its own command, never the toggle.

    async_auto_lip_sync_toggle() decides on a state only the Telnet
    callback ever writes, so on an HTTP-only receiver it reads None and
    always turns the setting on.
    """
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "auto_lip_sync")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    getattr(client, called).assert_awaited_once()
    getattr(client, not_called).assert_not_awaited()
    client.async_auto_lip_sync_toggle.assert_not_awaited()


async def test_auto_lip_sync_connectivity_error_marks_both_unavailable(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A connectivity failure on set marks both coordinators unavailable.

    Same symmetry the Audyssey-backed select entities already have (see
    test_select.py): the receiver being unreachable is receiver-wide
    news, not news about this one setting.
    """
    entry = await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "auto_lip_sync")

    client.async_auto_lip_sync_on.side_effect = AvrNetworkError(
        "Connection refused", "SetAudioDelay"
    )

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )

    assert entry.runtime_data.settings_coordinator.last_update_success is False
    assert entry.runtime_data.coordinator.last_update_success is False


@pytest.mark.parametrize(
    ("tone_control_adjust", "tone_control_status", "expected"),
    [
        pytest.param(True, False, "on", id="on_while_status_disagrees"),
        pytest.param(False, True, "off", id="off_while_status_disagrees"),
    ],
)
async def test_tone_control_reflects_adjust_not_status(
    hass: HomeAssistant,
    client: MagicMock,
    tone_control_adjust: bool,
    tone_control_status: bool,
    expected: str,
) -> None:
    """The switch tracks tone_control_adjust, which is the field that follows the toggle."""
    client.dynamic_eq = False
    client.tone_control_adjust = tone_control_adjust
    client.tone_control_status = tone_control_status
    await setup_denonavr(hass)

    entity_id = _entity_id(hass, SWITCH_DOMAIN, "tone_control")
    assert hass.states.get(entity_id).state == expected


@pytest.mark.parametrize(
    ("service", "method"),
    [
        pytest.param(SERVICE_TURN_ON, "async_enable_tone_control", id="turn_on"),
        pytest.param(SERVICE_TURN_OFF, "async_disable_tone_control", id="turn_off"),
    ],
)
async def test_toggle_tone_control(
    hass: HomeAssistant, client: MagicMock, service: str, method: str
) -> None:
    """Toggling the switch enables or disables tone control on the receiver."""
    client.dynamic_eq = False
    await setup_denonavr(hass)
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "tone_control")

    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )

    getattr(client, method).assert_awaited_once()
