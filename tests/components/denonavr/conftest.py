"""Fixtures shared across denonavr tests."""

from collections import defaultdict
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, create_autospec, patch

from denonavr import DenonAVR
from denonavr.const import ALL_TELNET_EVENTS, ZONE2, ZONE3
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
        mock_client_class.return_value.volume = -40.0
        # Reported, and no volume limit configured, which the library reads as 18.0.
        mock_client_class.return_value.max_volume = 18.0
        mock_client_class.return_value.max_volume_known = True

        mock_client_class.return_value.dynamic_eq = True
        mock_client_class.return_value.reference_level_offset = "0dB"
        mock_client_class.return_value.dynamic_volume = "Off"
        mock_client_class.return_value.multi_eq = "Reference"
        mock_client_class.return_value.multi_eq_setting_list = [
            "Off",
            "Flat",
            "L/R Bypass",
            "Reference",
            "Manual",
        ]
        mock_client_class.return_value.eco_mode = "Auto"
        mock_client_class.return_value.dimmer = "Bright"
        mock_client_class.return_value.auto_standby = "OFF"
        yield mock_client_class.return_value


def _add_zone(client: MagicMock, zone: str) -> MagicMock:
    """Give the mocked receiver a secondary zone, and return that zone's object.

    A zone is its own receiver object in the library, holding its own
    copy of every per-zone value, so a zone entity has to be given it
    rather than the main one.
    """
    zone_client = create_autospec(DenonAVR, instance=True)
    zone_client.name = f"{TEST_NAME} {zone}"
    zone_client.host = TEST_HOST
    zone_client.zone = zone
    zone_client.input_func_list = []
    zone_client.sound_mode_list = []
    zone_client.volume = -40.0
    zone_client.max_volume = 18.0
    zone_client.max_volume_known = True
    client.zones = {**client.zones, zone: zone_client}
    return zone_client


@pytest.fixture(name="zone2_client")
def zone2_client_fixture(client: MagicMock) -> MagicMock:
    """Give the mocked receiver a Zone 2, and return that zone's object."""
    return _add_zone(client, ZONE2)


@pytest.fixture(name="zone3_client")
def zone3_client_fixture(client: MagicMock) -> MagicMock:
    """Give the mocked receiver a Zone 3, and return that zone's object."""
    return _add_zone(client, ZONE3)


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
