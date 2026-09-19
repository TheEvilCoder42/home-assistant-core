"""Fixtures shared across denonavr tests."""

from collections import defaultdict
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

from denonavr.const import ALL_TELNET_EVENTS, POWER_ON
import pytest

from . import (
    TEST_HOST,
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_ZONE,
)

type TelnetCallback = Callable[[str, str, str], None]


@pytest.fixture
def fast_action_refresh_debounce() -> Generator[None]:
    """Patch the action-refresh debounce cooldown to zero.

    Every action schedules a debounced confirmation refresh; the real
    cooldown would make each test wait it out for nothing.
    """
    with patch(
        "homeassistant.components.denonavr.coordinator.ACTION_REFRESH_DEBOUNCE_COOLDOWN",
        0,
    ):
        yield


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
        client = mock_client_class.return_value
        client.name = TEST_NAME
        client.host = TEST_HOST
        client.model_name = TEST_MODEL
        client.serial_number = TEST_SERIALNUMBER
        client.manufacturer = TEST_MANUFACTURER
        client.receiver_type = TEST_RECEIVER_TYPE
        client.zone = TEST_ZONE
        client.power = POWER_ON
        client.input_func_list = []
        client.sound_mode_list = []
        client.zones = {TEST_ZONE: client}
        client.telnet_connected = False
        client.telnet_healthy = False
        client.dynamic_eq = True
        client.reference_level_offset = "0dB"
        client.dynamic_volume = "Off"
        client.multi_eq = "Reference"
        client.multi_eq_setting_list = [
            "Off",
            "Flat",
            "L/R Bypass",
            "Reference",
            "Manual",
        ]
        client.eco_mode = "Auto"
        client.dimmer = "Bright"
        client.auto_standby = "OFF"
        client.audio_delay = 140
        client.auto_lip_sync = True
        client.lfe = -2
        client.lfe_adjustable = True
        client.subwoofer = True
        client.subwoofer_adjustable = True
        # An idle receiver reports no level of either kind, so the tests that
        # want the dynamically added entities set these themselves. An
        # auto-generated MagicMock would be iterated by the platform.
        client.subwoofer_levels = None
        client.subwoofer_level_status = True
        client.channel_volumes = None
        yield client


@pytest.fixture
def fire_telnet_event(client: MagicMock) -> TelnetCallback:
    """Record the Telnet callbacks registered on the receiver.

    Returns a function firing one event at them the way denonavr does: the
    event's own callbacks first, then those registered for every event.
    """
    callbacks: defaultdict[str, list[TelnetCallback]] = defaultdict(list)

    def _register(event: str, callback: TelnetCallback) -> None:
        callbacks[event].append(callback)

    def _unregister(event: str, callback: TelnetCallback) -> None:
        callbacks[event].remove(callback)

    client.register_callback.side_effect = _register
    client.unregister_callback.side_effect = _unregister

    def _fire(zone: str, event: str, parameter: str) -> None:
        for callback in (*callbacks[event], *callbacks[ALL_TELNET_EVENTS]):
            callback(zone, event, parameter)

    return _fire
