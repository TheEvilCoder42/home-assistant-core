"""Fixtures shared across denonavr tests."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from . import (
    TEST_MANUFACTURER,
    TEST_MODEL,
    TEST_NAME,
    TEST_RECEIVER_TYPE,
    TEST_SERIALNUMBER,
    TEST_ZONE,
)


@pytest.fixture(name="client")
def client_fixture() -> Generator[MagicMock]:
    """Patch of client library for tests.

    Every platform (media_player, select, switch) is set up alongside
    whichever one a given test file targets, since they all share one
    config entry - so this always includes the Audyssey/device-setting
    attributes those platforms read, even for test files that don't
    exercise them directly. Leaving them as auto-generated MagicMocks
    makes the entity registry's stored "capabilities.options" for the
    select entities an unserializable mock, which crashes the whole
    test's teardown when it tries to write the registry.
    """
    with (
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR",
            autospec=True,
        ) as mock_client_class,
        patch("homeassistant.components.denonavr.config_flow.denonavr.async_discover"),
    ):
        mock_client_class.return_value.name = TEST_NAME
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
        mock_client_class.return_value.reference_level_offset = "0dB"
        mock_client_class.return_value.reference_level_offset_setting_list = [
            "0dB",
            "+5dB",
            "+10dB",
            "+15dB",
        ]
        mock_client_class.return_value.dynamic_volume = "Off"
        mock_client_class.return_value.dynamic_volume_setting_list = [
            "Off",
            "Light",
            "Medium",
            "Heavy",
        ]
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
