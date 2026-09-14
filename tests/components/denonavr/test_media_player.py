"""The tests for the denonavr media player platform."""

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock, patch

from denonavr.const import POWER_ON
from denonavr.exceptions import (
    AvrCommandError,
    AvrForbiddenError,
    AvrIncompleteResponseError,
    AvrInvalidResponseError,
    AvrNetworkError,
    AvrProcessingError,
    AvrTimoutError,
    DenonAvrError,
)
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.components import media_player
from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_TYPE,
    DOMAIN,
)
from homeassistant.components.denonavr.const import ATTR_DYNAMIC_EQ
from homeassistant.components.denonavr.services import (
    ATTR_COMMAND,
    SERVICE_GET_COMMAND,
    SERVICE_SET_DYNAMIC_EQ,
    SERVICE_UPDATE_AUDYSSEY,
)
from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_ALBUM_NAME,
    ATTR_MEDIA_ARTIST,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_TITLE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    ATTR_SOUND_MODE,
    SERVICE_SELECT_SOUND_MODE,
    SERVICE_SELECT_SOURCE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    CONF_HOST,
    CONF_MODEL,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PLAY_PAUSE,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import (
    TEST_HOST,
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_UNIQUE_ID,
    TEST_ZONE,
)

from tests.common import MockConfigEntry, async_fire_time_changed

ENTITY_ID = f"{media_player.DOMAIN}.{TEST_NAME}"


async def setup_denonavr(
    hass: HomeAssistant,
    serial_number: str | None = TEST_SERIALNUMBER,
    options: dict | None = None,
) -> MockConfigEntry:
    """Initialize media_player for tests."""
    entry_data = {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: serial_number,
    }

    mock_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_UNIQUE_ID if serial_number else None,
        data=entry_data,
        options=options or {},
    )

    mock_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)

    assert state
    assert state.name == TEST_NAME

    return mock_entry


@pytest.mark.usefixtures("client")
async def test_setup_without_serial_number(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    """Test a receiver reporting no serial number still gets its media player."""
    entry = await setup_denonavr(hass, serial_number=None)

    assert device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )


@pytest.mark.parametrize(
    ("exception", "marks_unavailable"),
    [
        pytest.param(AvrTimoutError("Timed out", "SetVolume"), True, id="timeout"),
        pytest.param(AvrForbiddenError("Forbidden", "SetVolume"), True, id="forbidden"),
        pytest.param(
            AvrInvalidResponseError("Bad XML", "SetVolume"),
            True,
            id="invalid_response",
        ),
        pytest.param(
            AvrProcessingError("Update not complete", "SetVolume"),
            False,
            id="processing_error",
        ),
        pytest.param(
            AvrCommandError("Could not set volume", "SetVolume"),
            False,
            id="command_error",
        ),
        pytest.param(DenonAvrError("Unexpected"), False, id="generic_denon_error"),
    ],
)
async def test_action_error_branches(
    hass: HomeAssistant,
    client: MagicMock,
    exception: Exception,
    marks_unavailable: bool,
) -> None:
    """Each async_log_errors exception branch logs and, for some, marks unavailable.

    Uses volume_up as a stand-in action - every method sharing this
    decorator behaves identically for each of these exception types.
    """
    entry = await setup_denonavr(hass)
    client.async_volume_up.side_effect = exception

    await hass.services.async_call(
        media_player.DOMAIN,
        SERVICE_VOLUME_UP,
        {ATTR_ENTITY_ID: ENTITY_ID},
        blocking=True,
    )

    assert entry.runtime_data.coordinator.last_update_success is not marks_unavailable
    assert (hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE) is marks_unavailable


async def test_telnet_callback_filters_and_writes_state(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """The telnet callback ignores events that don't concern this entity/zone."""
    await setup_denonavr(hass)
    telnet_callback = next(
        call.args[1]
        for call in client.register_callback.call_args_list
        if getattr(call.args[1], "__self__", None).__class__.__name__ == "DenonDevice"
    )

    with patch(
        "homeassistant.components.denonavr.media_player.DenonDevice.async_write_ha_state"
    ) as mock_write:
        telnet_callback("SomeOtherZone", "PS", "DYNEQ ON")
        mock_write.assert_not_called()

        telnet_callback(TEST_ZONE, "XX", "irrelevant")
        mock_write.assert_not_called()

        telnet_callback(TEST_ZONE, "NSE", "1notfour")
        mock_write.assert_not_called()

        telnet_callback(TEST_ZONE, "HD", "NOTALBUM")
        mock_write.assert_not_called()

        telnet_callback(TEST_ZONE, "PS", "DYNEQ ON")
        mock_write.assert_called_once()


async def test_dynamic_eq_attribute_updates_from_audyssey_coordinator(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """The dynamic_eq attribute refreshes when the Audyssey coordinator does.

    It's Audyssey-scoped data, but this entity's own coordinator
    subscription (from CoordinatorEntity) only covers general status -
    without a separate subscription to the Audyssey coordinator too,
    a switch toggle, the update/set services, or a periodic Audyssey
    refresh would leave this attribute stale until something unrelated
    (e.g. Telnet or the next general poll) happened to rewrite state.
    """
    entry = await setup_denonavr(hass)
    client.power = POWER_ON
    client.dynamic_eq = True
    entry.runtime_data.audyssey_coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_DYNAMIC_EQ] is True

    client.dynamic_eq = False
    entry.runtime_data.audyssey_coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_DYNAMIC_EQ] is False


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        pytest.param(None, None, id="unknown"),
        pytest.param(20.0, 1.0, id="set"),
    ],
)
async def test_volume_level(
    hass: HomeAssistant,
    client: MagicMock,
    volume: float | None,
    expected: float | None,
) -> None:
    """Volume is converted from Denon's range, or reported as unknown."""
    client.volume = volume
    await setup_denonavr(hass)

    state = hass.states.get(ENTITY_ID)
    assert state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL) == expected


async def test_supported_features_advertises_media_modes_for_netaudio(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Play/pause/track-skip features are only advertised for netaudio sources."""
    client.input_func = "Online Music"
    client.netaudio_func_list = ["Online Music"]
    await setup_denonavr(hass)

    features = hass.states.get(ENTITY_ID).attributes[ATTR_SUPPORTED_FEATURES]
    assert features & media_player.MediaPlayerEntityFeature.PLAY_MEDIA


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param("playing", "music", id="playing"),
        pytest.param("on", "channel", id="on"),
    ],
)
async def test_media_content_type(
    hass: HomeAssistant, client: MagicMock, state: str, expected: str
) -> None:
    """Playing/paused reports music, everything else reports channel."""
    client.state = state
    await setup_denonavr(hass)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_CONTENT_TYPE] == expected


@pytest.mark.parametrize(
    ("attribute_setup", "expected"),
    [
        pytest.param(
            {
                "input_func": "Tuner",
                "playing_func_list": ["Tuner"],
                "title": None,
                "frequency": "87.5",
                "image_url": None,
            },
            "87.5",
            id="frequency_fallback",
        ),
        pytest.param(
            {
                "input_func": "Tuner",
                "playing_func_list": ["Tuner"],
                "title": "A Title",
                "image_url": None,
            },
            "A Title",
            id="title_present",
        ),
        pytest.param(
            {"input_func": "AUX", "playing_func_list": []},
            "AUX",
            id="not_a_playing_func",
        ),
    ],
)
async def test_media_title(
    hass: HomeAssistant,
    client: MagicMock,
    attribute_setup: dict[str, object],
    expected: str,
) -> None:
    """The title falls back to the input name, then the tuned frequency."""
    for name, value in attribute_setup.items():
        setattr(client, name, value)
    await setup_denonavr(hass)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_TITLE] == expected


@pytest.mark.parametrize(
    ("artist", "band", "expected"),
    [
        pytest.param(None, "FM", "FM", id="band_fallback"),
        pytest.param("The Artist", "FM", "The Artist", id="artist_present"),
    ],
)
async def test_media_artist(
    hass: HomeAssistant,
    client: MagicMock,
    artist: str | None,
    band: str,
    expected: str,
) -> None:
    """The artist falls back to the tuner band when there's no artist."""
    client.artist = artist
    client.band = band
    await setup_denonavr(hass)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_ARTIST] == expected


@pytest.mark.parametrize(
    ("album", "station", "expected"),
    [
        pytest.param(None, "Some Station", "Some Station", id="station_fallback"),
        pytest.param("An Album", "Some Station", "An Album", id="album_present"),
    ],
)
async def test_media_album_name(
    hass: HomeAssistant,
    client: MagicMock,
    album: str | None,
    station: str,
    expected: str,
) -> None:
    """The album name falls back to the tuner station when there's no album."""
    client.album = album
    client.station = station
    await setup_denonavr(hass)

    assert hass.states.get(ENTITY_ID).attributes[ATTR_MEDIA_ALBUM_NAME] == expected


async def test_extra_state_attributes_hidden_when_powered_off(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """No extra attributes are reported while the receiver is powered off."""
    client.power = "STANDBY"
    await setup_denonavr(hass)

    state = hass.states.get(ENTITY_ID)
    assert ATTR_DYNAMIC_EQ not in state.attributes


@pytest.mark.parametrize(
    ("service", "service_data", "receiver_method"),
    [
        pytest.param(SERVICE_MEDIA_PLAY_PAUSE, {}, "async_toggle_play_pause"),
        pytest.param(SERVICE_MEDIA_PLAY, {}, "async_play"),
        pytest.param(SERVICE_MEDIA_PAUSE, {}, "async_pause"),
        pytest.param(SERVICE_MEDIA_STOP, {}, "async_stop"),
        pytest.param(SERVICE_MEDIA_PREVIOUS_TRACK, {}, "async_previous_track"),
        pytest.param(SERVICE_MEDIA_NEXT_TRACK, {}, "async_next_track"),
        pytest.param(
            SERVICE_SELECT_SOURCE,
            {ATTR_INPUT_SOURCE: "AUX"},
            "async_set_input_func",
        ),
        pytest.param(
            SERVICE_SELECT_SOUND_MODE,
            {ATTR_SOUND_MODE: "Music"},
            "async_set_sound_mode",
        ),
        pytest.param(SERVICE_TURN_ON, {}, "async_power_on"),
        pytest.param(SERVICE_TURN_OFF, {}, "async_power_off"),
        pytest.param(SERVICE_VOLUME_DOWN, {}, "async_volume_down"),
    ],
)
async def test_simple_command_wrappers(
    hass: HomeAssistant,
    client: MagicMock,
    service: str,
    service_data: dict[str, str],
    receiver_method: str,
) -> None:
    """Each thin command wrapper calls its matching receiver method."""
    # Needed for the play/pause/stop/track-skip services, which are
    # only advertised while the current input is a netaudio source.
    client.input_func = "NET"
    client.netaudio_func_list = ["NET"]
    await setup_denonavr(hass)

    await hass.services.async_call(
        media_player.DOMAIN,
        service,
        {ATTR_ENTITY_ID: ENTITY_ID, **service_data},
        blocking=True,
    )

    getattr(client, receiver_method).assert_awaited_once()


@pytest.mark.parametrize(
    ("volume", "expected_denon_volume"),
    [
        pytest.param(0.8, -0.0, id="mid_range"),
        pytest.param(1.0, 18.0, id="clamped_to_max"),
    ],
)
async def test_set_volume_level_converts_and_clamps(
    hass: HomeAssistant,
    client: MagicMock,
    volume: float,
    expected_denon_volume: float,
) -> None:
    """Volume is converted to Denon's range and clamped at its maximum."""
    await setup_denonavr(hass)

    await hass.services.async_call(
        media_player.DOMAIN,
        SERVICE_VOLUME_SET,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_MEDIA_VOLUME_LEVEL: volume},
        blocking=True,
    )

    client.async_set_volume.assert_awaited_once_with(expected_denon_volume)


async def test_mute_volume(hass: HomeAssistant, client: MagicMock) -> None:
    """The mute service calls through to the receiver."""
    await setup_denonavr(hass)

    await hass.services.async_call(
        media_player.DOMAIN,
        SERVICE_VOLUME_MUTE,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_MEDIA_VOLUME_MUTED: True},
        blocking=True,
    )

    client.async_mute.assert_awaited_once_with(True)


async def test_get_command(hass: HomeAssistant, client: MagicMock) -> None:
    """Test generic command functionality."""
    await setup_denonavr(hass)

    data = {
        ATTR_ENTITY_ID: ENTITY_ID,
        ATTR_COMMAND: "test_command",
    }
    await hass.services.async_call(DOMAIN, SERVICE_GET_COMMAND, data)
    await hass.async_block_till_done()

    client.async_get_command.assert_awaited_with("test_command")


async def test_update_audyssey(hass: HomeAssistant, client: MagicMock) -> None:
    """Test that dynamic eq method works."""
    await setup_denonavr(hass)

    # The Audyssey coordinator also fetches this once at setup (see
    # homeassistant/components/denonavr/coordinator.py), so the mock
    # has already been called by the time the service below runs -
    # assert the service adds exactly one more call, rather than a
    # fixed total.
    calls_before_service = client.async_update_audyssey.call_count

    # Verify call
    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_AUDYSSEY,
        {
            ATTR_ENTITY_ID: ENTITY_ID,
        },
    )
    await hass.async_block_till_done()

    assert client.async_update_audyssey.call_count == calls_before_service + 1


async def test_update_audyssey_forces_fetch_with_healthy_telnet(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """The explicit action must still fetch even if Telnet already looks healthy.

    Otherwise this action would silently do nothing whenever Telnet is
    on and connected - the same Telnet-healthy skip that lets
    scheduled polls save an HTTP round-trip would swallow this
    explicit, on-demand one too.
    """
    client.telnet_connected = True
    client.telnet_healthy = True
    await setup_denonavr(hass, options={"use_telnet": True})

    calls_before_service = client.async_update_audyssey.call_count

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_AUDYSSEY,
        {ATTR_ENTITY_ID: ENTITY_ID},
    )
    await hass.async_block_till_done()

    assert client.async_update_audyssey.call_count == calls_before_service + 1


async def test_update_audyssey_restores_availability(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A successful call recovers Audyssey entities from a prior failure.

    Calling the receiver directly instead of going through the
    coordinator would change its properties without updating the
    Audyssey coordinator's last_update_success, so a prior failure
    would keep every Audyssey-backed entity unavailable even after
    this succeeds.
    """
    entry = await setup_denonavr(hass)
    entry.runtime_data.audyssey_coordinator.last_update_success = False

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_AUDYSSEY,
        {ATTR_ENTITY_ID: ENTITY_ID},
    )
    await hass.async_block_till_done()

    assert entry.runtime_data.audyssey_coordinator.last_update_success is True


async def test_update_audyssey_connectivity_error_marks_media_player_unavailable(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A connectivity failure here also affects the general coordinator.

    This entity's own availability is tied to the general coordinator,
    not the Audyssey one it's routed through here - without also
    marking that one unavailable, a connectivity failure would leave
    this entity looking available despite just confirming the
    receiver itself is unreachable.
    """
    entry = await setup_denonavr(hass)
    client.async_update_audyssey.side_effect = AvrNetworkError(
        "Connection refused", "GetAudyssey"
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_AUDYSSEY,
        {ATTR_ENTITY_ID: ENTITY_ID},
    )
    await hass.async_block_till_done()

    assert entry.runtime_data.coordinator.last_update_success is False


async def test_dynamic_eq(hass: HomeAssistant, client: MagicMock) -> None:
    """Test that dynamic eq method works."""
    await setup_denonavr(hass)

    data = {
        ATTR_ENTITY_ID: ENTITY_ID,
        ATTR_DYNAMIC_EQ: True,
    }
    # Verify on call
    await hass.services.async_call(DOMAIN, SERVICE_SET_DYNAMIC_EQ, data)
    await hass.async_block_till_done()

    # Verify off call
    data[ATTR_DYNAMIC_EQ] = False
    await hass.services.async_call(DOMAIN, SERVICE_SET_DYNAMIC_EQ, data)
    await hass.async_block_till_done()

    client.async_dynamic_eq_on.assert_called_once()
    client.async_dynamic_eq_off.assert_called_once()


async def test_set_dynamic_eq_connectivity_error_marks_audyssey_unavailable(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """A connectivity failure here also affects the Audyssey coordinator.

    This command is Audyssey-scoped, sent directly to the receiver
    rather than through that coordinator - so on a connectivity
    failure, only marking the general coordinator unavailable (what
    the decorator already does) would leave Audyssey-backed entities
    still showing available with stale data.
    """
    entry = await setup_denonavr(hass)
    client.async_dynamic_eq_on.side_effect = AvrNetworkError(
        "Connection refused", "SetAudyssey"
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_DYNAMIC_EQ,
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_DYNAMIC_EQ: True},
    )
    await hass.async_block_till_done()

    assert entry.runtime_data.audyssey_coordinator.last_update_success is False


async def test_set_dynamic_eq_always_refreshes_audyssey(
    hass: HomeAssistant, client: MagicMock
) -> None:
    """Refreshes Audyssey after this action regardless of the option.

    "Update Audyssey settings" only governs the recurring poll - this
    action just changed Audyssey-scoped data directly, so the new
    select/switch entities need to hear about it either way.
    """
    with patch(
        "homeassistant.components.denonavr.coordinator.ACTION_REFRESH_DEBOUNCE_COOLDOWN",
        0,
    ):
        await setup_denonavr(hass, options={"update_audyssey": False})
        calls_before = client.async_update_audyssey.await_count

        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_DYNAMIC_EQ,
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_DYNAMIC_EQ: False},
        )
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    assert client.async_update_audyssey.await_count > calls_before


@pytest.mark.parametrize(
    "exception",
    [
        pytest.param(
            AvrInvalidResponseError("XML parse error", "GET"),
            id="invalid_response",
        ),
        pytest.param(
            AvrIncompleteResponseError("Incomplete", "GET"),
            id="incomplete_response",
        ),
    ],
)
async def test_malformed_response_marks_unavailable(
    hass: HomeAssistant,
    client: MagicMock,
    freezer: FrozenDateTimeFactory,
    exception: Exception,
) -> None:
    """Test that malformed response errors mark the entity unavailable."""
    await setup_denonavr(hass)

    state = hass.states.get(ENTITY_ID)
    assert state.state != STATE_UNAVAILABLE

    # Force polling by disabling telnet, then trigger the error
    client.telnet_connected = False
    client.telnet_healthy = False
    client.async_update.side_effect = exception
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_UNAVAILABLE
