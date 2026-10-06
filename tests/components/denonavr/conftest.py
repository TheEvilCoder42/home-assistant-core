"""Fixtures shared across denonavr tests."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

from denonavr.const import POWER_ON
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
        yield client
