"""The tests for the denonavr select platform."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from unittest.mock import MagicMock, patch

from denonavr.exceptions import AvrCommandError, AvrForbiddenError, AvrNetworkError
from freezegun.api import FrozenDateTimeFactory
import pytest
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.denonavr.const import (
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    COORDINATOR_UPDATE_INTERVAL,
    PENDING_VALUE_TIMEOUT,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_OPTION,
    SERVICE_SELECT_OPTION,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from . import (
    TEST_HOST,
    advance_time,
    get_entity_id,
    setup_denonavr,
    wait_for_debounced_refresh,
)

from tests.common import async_fire_time_changed, snapshot_platform

pytestmark = pytest.mark.usefixtures("fast_action_refresh_debounce")


async def _select_option(hass: HomeAssistant, entity_id: str, option: str) -> None:
    """Select an option through the service call."""
    await hass.services.async_call(
        SELECT_DOMAIN,
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: entity_id, ATTR_OPTION: option},
        blocking=True,
    )


@pytest.mark.usefixtures("client")
async def test_entities(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the select entities and their registry entries."""
    with patch("homeassistant.components.denonavr.PLATFORMS", [Platform.SELECT]):
        entry = await setup_denonavr(hass)

    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)


async def test_reference_level_offset_unavailable_when_dynamic_eq_off(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Test the reference level offset select is unavailable without Dynamic EQ."""
    client.dynamic_eq = False
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "reference_level_offset")
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("multi_eq", "expected"),
    [
        pytest.param("Off", STATE_UNAVAILABLE, id="multeq_off"),
        pytest.param("Flat", "off", id="multeq_on"),
        pytest.param(None, "off", id="multeq_unknown"),
    ],
)
async def test_dynamic_volume_follows_multeq(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    multi_eq: str | None,
    expected: str,
) -> None:
    """Dynamic Volume is unavailable while MultEQ is Off.

    The receiver forces it off there, and a change silently turns MultEQ
    back on. An unknown MultEQ leaves the select available.
    """
    client.multi_eq = multi_eq
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dynamic_volume")
    assert hass.states.get(entity_id).state == expected


@pytest.mark.parametrize("sound_mode", ["DIRECT", "PURE DIRECT"])
@pytest.mark.parametrize(
    ("key", "option"),
    [
        pytest.param("multi_eq", "reference", id="multi_eq"),
        pytest.param("dynamic_volume", "off", id="dynamic_volume"),
        pytest.param("reference_level_offset", "0db", id="reference_level_offset"),
    ],
)
async def test_audyssey_follows_direct_through_a_status_refresh(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    sound_mode: str,
    key: str,
    option: str,
) -> None:
    """Audyssey selects are unavailable in Direct, seen by a status refresh alone.

    Direct bypasses Audyssey and the receiver drops every command for it. The
    sound mode is a status value and the Audyssey coordinator does not
    refresh here, so the selects have to follow the status coordinator too.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, key)
    assert hass.states.get(entity_id).state == option
    audyssey_reads = client.async_update_audyssey.await_count

    client.sound_mode = sound_mode
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.sound_mode = "STEREO"
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == option
    assert client.async_update_audyssey.await_count == audyssey_reads


async def test_multi_eq_options_follow_the_receiver(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Audyssey MultEQ offers only what the receiver takes over its connection.

    Without Telnet the receiver cannot be set to Manual.
    """
    client.multi_eq_setting_list = ["Off", "Flat", "L/R Bypass", "Reference"]
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "multi_eq")
    assert hass.states.get(entity_id).attributes["options"] == [
        "off",
        "flat",
        "l_r_bypass",
        "reference",
    ]


async def test_unknown_value_is_unavailable(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A value outside the option keys is not shown as one of them."""
    client.dimmer = "Unexpected"
    await setup_denonavr(hass)

    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("key", "option", "command", "value"),
    [
        pytest.param(
            "reference_level_offset",
            "5db",
            "async_set_reflevoffset",
            "+5dB",
            id="reference_level_offset",
        ),
        pytest.param(
            "dynamic_volume",
            "heavy",
            "async_set_dynamicvol",
            "Heavy",
            id="dynamic_volume",
        ),
        pytest.param(
            "multi_eq", "l_r_bypass", "async_set_multieq", "L/R Bypass", id="multi_eq"
        ),
        pytest.param("eco_mode", "on", "async_eco_mode", "On", id="eco_mode"),
        pytest.param("dimmer", "dark", "async_dimmer", "Dark", id="dimmer"),
        pytest.param(
            "auto_standby", "30m", "async_auto_standby", "30M", id="auto_standby"
        ),
    ],
)
async def test_select_option(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    key: str,
    option: str,
    command: str,
    value: str,
) -> None:
    """Selecting an option sends the receiver's value and shows the option."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, key)

    await _select_option(hass, entity_id, option)

    getattr(client, command).assert_awaited_once_with(value)
    assert hass.states.get(entity_id).state == option


@pytest.mark.parametrize(
    ("error", "available", "error_message", "logs"),
    [
        pytest.param(
            AvrCommandError(
                "Reference level could only be set when DynamicEQ is active",
                "SetAudyssey",
            ),
            True,
            "Reference level could only be set when DynamicEQ is active",
            0,
            id="rejected_command",
        ),
        pytest.param(
            AvrForbiddenError("Forbidden", "SetAudyssey"),
            True,
            "Forbidden",
            0,
            id="forbidden",
        ),
        pytest.param(
            AvrNetworkError("Connection refused", "SetAudyssey"),
            False,
            "Connection refused",
            1,
            id="unreachable_receiver",
        ),
    ],
)
async def test_select_option_raises_on_avr_error(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    available: bool,
    error_message: str,
    logs: int,
) -> None:
    """A receiver error while selecting is surfaced to the user.

    A connectivity failure also marks both coordinators unavailable, since
    an unreachable receiver found through an Audyssey setting is the same
    news as one found through a media_player command. It is logged once, not
    once per coordinator.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "reference_level_offset")

    client.async_set_reflevoffset.side_effect = error

    with pytest.raises(HomeAssistantError) as err:
        await _select_option(hass, entity_id, "5db")

    assert err.value.translation_key == "set_failed"
    assert (
        str(err.value)
        == f"Setting {entity_id} to 5db failed on {TEST_HOST}: {error_message}"
    )
    assert entry.runtime_data.coordinator.last_update_success is available
    assert entry.runtime_data.audyssey_coordinator.last_update_success is available
    assert (hass.states.get(entity_id).state != STATE_UNAVAILABLE) is available
    assert caplog.text.count("Error requesting denonavr_") == logs


@pytest.mark.parametrize(
    ("options", "audyssey_available"),
    [
        pytest.param({}, False, id="audyssey_has_no_poll"),
        pytest.param({CONF_UPDATE_AUDYSSEY: True}, True, id="audyssey_polls"),
    ],
)
async def test_general_failure_reaches_audyssey_only_where_it_cannot_read(
    hass: HomeAssistant,
    client: MagicMock,
    options: dict[str, bool],
    audyssey_available: bool,
) -> None:
    """Without a poll of its own it has to be handed the verdict.

    With one it has already reached the receiver, and Audyssey selects
    showing data the receiver just answered for beats hiding them over a
    failure on the other coordinator's query.
    """
    entry = await setup_denonavr(hass, options=options)

    client.async_update.side_effect = AvrNetworkError("Connection refused", "GET")
    await entry.runtime_data.coordinator.async_refresh()

    assert (
        entry.runtime_data.audyssey_coordinator.last_update_success
        is audyssey_available
    )


async def test_unavailable_after_connectivity_error_then_recovers(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A connectivity-type refresh failure marks the entity unavailable.

    A later successful refresh recovers. Calls async_refresh() directly
    because the assertions need a synchronous refresh rather than the
    debounced one an action uses.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    client.async_update.side_effect = AvrNetworkError("Connection refused", "GET")
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.async_update.side_effect = None
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_dimmer_refreshes_and_shows_new_state_immediately(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Selecting a dimmer option confirms it with one refresh."""
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    async def _apply_dimmer_change(*args: object, **kwargs: object) -> None:
        client.dimmer = "Dark"

    # Installed after setup so its initial refresh does not already flip dimmer.
    client.async_update.side_effect = _apply_dimmer_change

    await _select_option(hass, entity_id, "dark")
    # blocking=True returns before the debounced confirmation refresh runs.
    await wait_for_debounced_refresh(hass)

    # should_poll=False, so setup's refresh and this action's confirmation
    # are the only two reads.
    assert client.async_update.await_count == 2
    assert hass.states.get(entity_id).state == "dark"


async def test_eco_mode_and_auto_standby_also_refresh_immediately(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Eco mode and auto standby also confirm their own action."""
    await setup_denonavr(hass)
    baseline_calls = client.async_update.await_count

    await _select_option(
        hass, get_entity_id(entity_registry, SELECT_DOMAIN, "eco_mode"), "off"
    )
    await wait_for_debounced_refresh(hass)

    await _select_option(
        hass, get_entity_id(entity_registry, SELECT_DOMAIN, "auto_standby"), "15m"
    )
    await wait_for_debounced_refresh(hass)

    # One read per action: should_poll=False, so HA adds no post-call poll.
    assert client.async_update.await_count == baseline_calls + 2


async def test_reference_level_offset_always_refreshes_after_change(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Audyssey-group settings always confirm a change.

    "Update Audyssey settings" governs the recurring poll alone, and it is
    off by default, so gating this refresh on it would leave the UI showing
    the old value of a change the receiver did apply.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "reference_level_offset")

    # Setup does one initial Audyssey fetch per config entry.
    baseline_calls = client.async_update_audyssey.await_count

    await _select_option(hass, entity_id, "5db")
    await wait_for_debounced_refresh(hass)

    # Just one call, since this is the only entity acting - HA's own
    # post-service-call poll doesn't apply here (should_poll=False).
    assert client.async_update_audyssey.await_count == baseline_calls + 1


async def test_coordinators_serialize_command_and_refresh(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A select action and an Audyssey refresh on the shared lock don't overlap.

    Running a command concurrently with a refresh on the other coordinator
    proves the shared lock serializes them, which asserting that the two
    lock objects are identical would not.
    """
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    call_order = []

    async def _slow_dimmer_set(option: str) -> None:
        call_order.append("start-dimmer")
        await asyncio.sleep(0.05)
        client.dimmer = option
        call_order.append("end-dimmer")

    async def _slow_audyssey_update(*args: object, **kwargs: object) -> None:
        call_order.append("start-audyssey")
        await asyncio.sleep(0.05)
        call_order.append("end-audyssey")

    client.async_dimmer.side_effect = _slow_dimmer_set
    client.async_update_audyssey.side_effect = _slow_audyssey_update

    await asyncio.gather(
        _select_option(hass, entity_id, "dark"),
        entry.runtime_data.audyssey_coordinator.async_refresh(),
    )

    assert call_order in (
        ["start-dimmer", "end-dimmer", "start-audyssey", "end-audyssey"],
        ["start-audyssey", "end-audyssey", "start-dimmer", "end-dimmer"],
    )


async def test_audyssey_coordinator_polls_when_option_on(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The Audyssey coordinator actually polls on a schedule when the option is on.

    Exercises the real behavior (a call once the interval elapses)
    rather than just asserting update_interval was set, which would
    still pass even if the recurring poll's own listener registration
    were broken.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
    calls_before = client.async_update_audyssey.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_audyssey.await_count > calls_before


async def test_audyssey_coordinator_skips_poll_when_telnet_healthy(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """A scheduled Audyssey poll is skipped once Telnet already keeps it current.

    Mirrors async_refresh_status's own guard for the general
    coordinator - Telnet already pushes these settings live (see
    __init__.py's Telnet listener), so a receiver where this HTTP
    query takes ~10s shouldn't be hit with it again every interval.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
    client.telnet_connected = True
    client.telnet_healthy = True
    calls_before = client.async_update_audyssey.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_audyssey.await_count == calls_before


async def test_audyssey_coordinator_does_not_poll_when_option_off(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without the option the Audyssey coordinator does not poll on a schedule.

    It still refreshes on demand, such as right after an action.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    calls_before = client.async_update_audyssey.await_count

    freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert client.async_update_audyssey.await_count == calls_before


async def test_setup_skips_redundant_audyssey_refresh_with_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Setup fetches Audyssey once with Telnet and "Update Audyssey settings" on.

    Each fetch can take ~10s, and setup runs again on every reload.
    """
    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: True}
    )

    assert client.async_update_audyssey.await_count == 1


async def test_setup_forces_audyssey_fetch_with_telnet_but_no_polling(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Setup still fetches Audyssey once when Telnet is on but polling is off.

    Telnet only pushes Audyssey data on a change, never on connect, so
    without forcing this fetch, async_refresh_audyssey's own Telnet-healthy
    skip would leave these entities unavailable indefinitely.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    client.dynamic_eq = None
    client.reference_level_offset = None

    async def _populate(*_args: object, **_kwargs: object) -> None:
        client.dynamic_eq = True
        client.reference_level_offset = "0dB"

    client.async_update_audyssey.side_effect = _populate

    await setup_denonavr(
        hass, options={CONF_USE_TELNET: True, CONF_UPDATE_AUDYSSEY: False}
    )

    assert client.async_update_audyssey.await_count == 1
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "reference_level_offset")
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_refresh_failure_does_not_fail_an_already_successful_action(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A refresh failure must not fail an already-successful command.

    The user's requested change already applied; only the confirmation
    query failed, which should just leave the optimistic value in
    place rather than surface as an error.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    client.async_update.side_effect = AvrCommandError("Timed out", "GetDimmer")

    await _select_option(hass, entity_id, "dark")

    client.async_dimmer.assert_awaited_once_with("Dark")
    assert hass.states.get(entity_id).state == "dark"


async def test_rapid_consecutive_selections_do_not_race(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Two select_option calls fired back-to-back on the same entity must not race.

    PARALLEL_UPDATES only serializes calls targeting several entities at
    once, so two calls to this one entity are left to entity.py's lock.
    """
    call_order = []

    async def _slow_dimmer_set(value: str) -> None:
        call_order.append(f"start-{value}")
        await asyncio.sleep(0.05)
        client.dimmer = value
        call_order.append(f"end-{value}")

    client.async_dimmer.side_effect = _slow_dimmer_set

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    # gather starts the calls in order, and the lock is FIFO.
    await asyncio.gather(
        _select_option(hass, entity_id, "dark"),
        _select_option(hass, entity_id, "dim"),
    )

    assert call_order == ["start-Dark", "end-Dark", "start-Dim", "end-Dim"]
    assert hass.states.get(entity_id).state == "dim"


@pytest.mark.parametrize(
    ("key", "event", "attribute", "value", "state"),
    [
        pytest.param("multi_eq", "PS", "multi_eq", "Flat", "flat", id="audyssey"),
        pytest.param("dimmer", "DIM", "dimmer", "Dark", "dark", id="status"),
    ],
)
async def test_telnet_push_updates_entities_without_media_player(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
    key: str,
    event: str,
    attribute: str,
    value: str,
    state: str,
) -> None:
    """A Telnet push reaches the selects on either coordinator.

    Registered on the receiver rather than on an entity, whose callback
    would run only while that entity is enabled. The polls skip while Telnet
    is healthy, so the push is the only thing that could update them.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    with patch("homeassistant.components.denonavr.PLATFORMS", [Platform.SELECT]):
        await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, key)

    setattr(client, attribute, value)
    fire_telnet_event("Main", event, "")

    assert hass.states.get(entity_id).state == state


@pytest.mark.parametrize(
    "status_failed",
    [
        pytest.param(False, id="healthy"),
        # The push then also clears the failure.
        pytest.param(True, id="after_failure"),
    ],
)
async def test_telnet_push_keeps_an_actions_confirmation_refresh(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
    status_failed: bool,
) -> None:
    """An unrelated push must not cancel the refresh confirming an action."""
    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")
    baseline_calls = client.async_update.await_count

    await _select_option(hass, entity_id, "dark")
    entry.runtime_data.coordinator.last_update_success = not status_failed
    fire_telnet_event("Main", "MV", "50")
    await wait_for_debounced_refresh(hass)

    assert entry.runtime_data.coordinator.last_update_success
    assert client.async_update.await_count == baseline_calls + 1


async def test_audyssey_entities_not_unavailable_on_fresh_setup(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """Audyssey-dependent entities aren't unavailable after a fresh setup.

    Nothing in the regular poll loop fetches Audyssey data unless
    "Update Audyssey settings" is on - without the one-time initial
    fetch, these entities (reference_level_offset especially, since it
    also gates on dynamic_eq) would stay unavailable indefinitely.
    """
    client.dynamic_eq = None
    client.reference_level_offset = None
    client.dynamic_volume = None
    client.multi_eq = None

    async def _populate_audyssey(*args: object, **kwargs: object) -> None:
        client.dynamic_eq = True
        client.reference_level_offset = "0dB"
        client.dynamic_volume = "Off"
        client.multi_eq = "Reference"

    client.async_update_audyssey.side_effect = _populate_audyssey

    # The option's default.
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})

    for domain, key in (
        (SWITCH_DOMAIN, "dynamic_eq"),
        (SELECT_DOMAIN, "reference_level_offset"),
        (SELECT_DOMAIN, "dynamic_volume"),
        (SELECT_DOMAIN, "multi_eq"),
    ):
        entity_id = get_entity_id(entity_registry, domain, key)
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_option_shown_immediately_even_if_refresh_reads_back_stale_value(
    hass: HomeAssistant, client: MagicMock, entity_registry: er.EntityRegistry
) -> None:
    """A stale confirmation refresh must not revert to the previous value."""
    # The refresh reports the old value, as if the command had not settled yet.
    client.async_update.side_effect = lambda *a, **k: None  # dimmer stays "Bright"

    entry = await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    await _select_option(hass, entity_id, "dark")
    # Let the debounced confirmation refresh run its stale read.
    await wait_for_debounced_refresh(hass)

    client.async_dimmer.assert_awaited_once_with("Dark")
    assert hass.states.get(entity_id).state == "dark"

    client.dimmer = "Dark"
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "dark"

    # Shows through only if the read-back above cleared the pending value.
    client.dimmer = "Bright"
    await entry.runtime_data.coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "bright"


async def test_pending_option_expires_instead_of_masking_forever(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The pending override must expire rather than mask reality forever.

    A command that silently did not apply, or an external change landing
    while a value is pending, leaves the receiver's value never catching
    up, and the override has to give way to it.
    """
    client.async_update.side_effect = lambda *a, **k: None  # dimmer stays "Bright"

    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    await _select_option(hass, entity_id, "dark")
    assert hass.states.get(entity_id).state == "dark"

    # The front panel changes it to something else entirely while the
    # override is pending.
    client.dimmer = "Dim"
    await advance_time(hass, freezer, PENDING_VALUE_TIMEOUT + 1)

    assert hass.states.get(entity_id).state == "dim"


@pytest.mark.usefixtures("client")
async def test_second_pending_value_cancels_first_ones_expiry_timer(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A second command before the first's pending value expires restarts it.

    Otherwise the first command's timer would drop the second's value early.
    """
    await setup_denonavr(hass)
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    await _select_option(hass, entity_id, "dark")
    await advance_time(hass, freezer, PENDING_VALUE_TIMEOUT / 2)
    await _select_option(hass, entity_id, "dim")
    await advance_time(hass, freezer, PENDING_VALUE_TIMEOUT / 2 + 1)

    assert hass.states.get(entity_id).state == "dim"


@pytest.mark.parametrize(
    ("telnet_healthy", "pref_disable_polling"),
    [
        # An ordinary refresh skips the read while Telnet is healthy, and
        # expiry means no Telnet push confirmed the value.
        pytest.param(True, False, id="telnet_healthy"),
        pytest.param(False, True, id="polling_disabled"),
    ],
)
async def test_pending_expiry_reads_the_receiver(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    telnet_healthy: bool,
    pref_disable_polling: bool,
) -> None:
    """Expiry asks for one more read, not just shows the stale value.

    Without it, a command that was simply slow to apply would be stuck
    showing the pre-command value when no poll reads it again.
    """
    await setup_denonavr(hass, pref_disable_polling=pref_disable_polling)
    client.telnet_connected = telnet_healthy
    client.telnet_healthy = telnet_healthy
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, "dimmer")

    await _select_option(hass, entity_id, "dark")
    # Well short of the expiry, so only the confirming refresh runs.
    await advance_time(hass, freezer, 1)
    calls_after_action = client.async_update.await_count

    await advance_time(hass, freezer, PENDING_VALUE_TIMEOUT)

    assert client.async_update.await_count == calls_after_action + 1


async def test_expiries_of_settings_changed_together_share_one_refresh(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Settings that expire together must not each read the receiver.

    Every one of these reads is a full Audyssey round trip, and a refresh
    already running when the next expiry fires started well after the
    command that expiry gave up on, so it confirms that one too.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})

    await _select_option(
        hass, get_entity_id(entity_registry, SELECT_DOMAIN, "multi_eq"), "flat"
    )
    await _select_option(
        hass, get_entity_id(entity_registry, SELECT_DOMAIN, "dynamic_volume"), "heavy"
    )
    # Well short of the expiry, so only the confirming refreshes run and
    # the count below covers the two expiries alone. The receiver never
    # reports the new values, so both stay pending.
    await advance_time(hass, freezer, 1)
    calls_after_actions = client.async_update_audyssey.await_count

    async def _suspending_update(*args: object, **kwargs: object) -> None:
        # Yields so the second expiry arrives while the first is still
        # refreshing; without it the mock never suspends.
        await asyncio.sleep(0)

    client.async_update_audyssey.side_effect = _suspending_update

    await advance_time(hass, freezer, PENDING_VALUE_TIMEOUT + 1)

    assert client.async_update_audyssey.await_count == calls_after_actions + 1


async def test_audyssey_poll_needs_an_entity_not_just_internal_wiring(
    hass: HomeAssistant, client: MagicMock, freezer: FrozenDateTimeFactory
) -> None:
    """The cross-coordinator wiring alone must not keep the poll running.

    Loading no platforms leaves that wiring as the only subscriber, so a
    poll here would be one no entity ever asked for.
    """
    with patch("homeassistant.components.denonavr.PLATFORMS", []):
        await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: True})
        calls_before = client.async_update_audyssey.await_count

        freezer.tick(timedelta(seconds=COORDINATOR_UPDATE_INTERVAL + 1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert client.async_update_audyssey.await_count == calls_before


@pytest.mark.parametrize(
    ("key", "command", "option", "event"),
    [
        pytest.param("multi_eq", "async_set_multieq", "flat", "PS", id="audyssey"),
        pytest.param("dimmer", "async_dimmer", "dark", "DIM", id="status"),
    ],
)
async def test_telnet_update_clears_a_connectivity_failure(
    hass: HomeAssistant,
    client: MagicMock,
    entity_registry: er.EntityRegistry,
    fire_telnet_event: Callable[[str, str, str], None],
    key: str,
    command: str,
    option: str,
    event: str,
) -> None:
    """A Telnet push has to restore availability, not only notify listeners.

    With "Update Audyssey settings" off the Audyssey coordinator has no poll
    of its own, and with polling disabled neither has, so notifying alone
    would leave these entities unavailable while Telnet keeps them current.
    """
    await setup_denonavr(hass, options={CONF_UPDATE_AUDYSSEY: False})
    entity_id = get_entity_id(entity_registry, SELECT_DOMAIN, key)

    getattr(client, command).side_effect = AvrNetworkError(
        "Connection refused", "SetAudyssey"
    )
    with pytest.raises(HomeAssistantError):
        await _select_option(hass, entity_id, option)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    fire_telnet_event("Main", event, "")
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
