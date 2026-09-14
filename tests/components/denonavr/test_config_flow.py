"""Test the DenonAVR config flow."""

from typing import Any
from unittest.mock import patch

from denonavr.exceptions import AvrTimoutError, DenonAvrError
import pytest

from homeassistant import config_entries
from homeassistant.components.denonavr.config_flow import (
    CONF_MANUFACTURER,
    CONF_SERIAL_NUMBER,
    CONF_SHOW_ALL_SOURCES,
    CONF_TYPE,
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    CONF_ZONE2,
    CONF_ZONE3,
    DOMAIN,
)
from homeassistant.const import CONF_HOST, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import (
    ATTR_UPNP_MANUFACTURER,
    ATTR_UPNP_MODEL_NAME,
    ATTR_UPNP_SERIAL,
    SsdpServiceInfo,
)

from tests.common import MockConfigEntry

TEST_HOST = "1.2.3.4"
TEST_HOST2 = "5.6.7.8"
TEST_NAME = "Test_Receiver"
TEST_MODEL = "model5"
TEST_IGNORED_MODEL = "HEOS 7"
TEST_RECEIVER_TYPE = "avr-x"
TEST_SERIALNUMBER = "123456789"
TEST_MANUFACTURER = "Denon"
TEST_UPDATE_AUDYSSEY = False
TEST_SSDP_LOCATION = f"http://{TEST_HOST}/"
TEST_UNIQUE_ID = f"{TEST_MODEL}-{TEST_SERIALNUMBER}"
TEST_DISCOVER_1_RECEIVER = [{CONF_HOST: TEST_HOST}]
TEST_DISCOVER_2_RECEIVER = [{CONF_HOST: TEST_HOST}, {CONF_HOST: TEST_HOST2}]


@pytest.fixture(name="denonavr_connect", autouse=True)
def denonavr_connect_fixture():
    """Mock denonavr connection and entry setup."""
    with (
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.async_setup",
            return_value=None,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.async_update",
            return_value=None,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.support_sound_mode",
            return_value=True,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.name",
            TEST_NAME,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.model_name",
            TEST_MODEL,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.serial_number",
            TEST_SERIALNUMBER,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.manufacturer",
            TEST_MANUFACTURER,
        ),
        patch(
            "homeassistant.components.denonavr.receiver.DenonAVR.receiver_type",
            TEST_RECEIVER_TYPE,
        ),
        patch(
            "homeassistant.components.denonavr.async_setup_entry",
            return_value=True,
        ),
    ):
        yield


async def test_config_flow_manual_host_success(hass: HomeAssistant) -> None:
    """Successful flow manually initialized by the user.

    Host specified.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: TEST_HOST},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    assert result["options"] == {CONF_USE_TELNET: True}


async def test_config_flow_manual_discover_1_success(hass: HomeAssistant) -> None:
    """Successful flow manually initialized by the user.

    Without the host specified and 1 receiver discovered.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.config_flow.denonavr.async_discover",
        return_value=TEST_DISCOVER_1_RECEIVER,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    assert result["options"] == {CONF_USE_TELNET: True}


async def test_config_flow_manual_discover_2_success(hass: HomeAssistant) -> None:
    """Successful flow manually initialized by the user.

    Without the host specified and 2 receiver discovered.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.config_flow.denonavr.async_discover",
        return_value=TEST_DISCOVER_2_RECEIVER,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select"
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"select_host": TEST_HOST2},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST2,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    assert result["options"] == {CONF_USE_TELNET: True}


async def test_config_flow_manual_discover_error(hass: HomeAssistant) -> None:
    """Failed flow manually initialized by the user.

    Without the host specified and no receiver discovered.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.config_flow.denonavr.async_discover",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "discovery_error"}


async def test_config_flow_manual_host_no_serial(hass: HomeAssistant) -> None:
    """Successful flow manually initialized by the user.

    Host specified and an error getting the serial number.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.receiver.DenonAVR.serial_number",
        None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: TEST_HOST},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: None,
    }


CANNOT_CONNECT = pytest.mark.parametrize(
    ("attribute", "patch_kwargs"),
    [
        pytest.param(
            "async_setup",
            {"side_effect": AvrTimoutError("Timeout", "async_setup")},
            id="timeout",
        ),
        pytest.param(
            "async_setup",
            {"side_effect": DenonAvrError("Unexpected")},
            id="unexpected_error",
        ),
        # A receiver in standby answers without its device info.
        pytest.param("receiver_type", {"new": None}, id="no_device_info"),
    ],
)


@CANNOT_CONNECT
async def test_config_flow_manual_host_cannot_connect(
    hass: HomeAssistant, attribute: str, patch_kwargs: dict[str, Any]
) -> None:
    """A receiver that cannot be reached shows the error, and a retry succeeds."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        f"homeassistant.components.denonavr.receiver.DenonAVR.{attribute}",
        **patch_kwargs,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: TEST_HOST},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: TEST_HOST},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == TEST_HOST


@CANNOT_CONNECT
async def test_config_flow_select_cannot_connect(
    hass: HomeAssistant, attribute: str, patch_kwargs: dict[str, Any]
) -> None:
    """A selected receiver that cannot be reached can be selected again."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "homeassistant.components.denonavr.config_flow.denonavr.async_discover",
        return_value=TEST_DISCOVER_2_RECEIVER,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    with patch(
        f"homeassistant.components.denonavr.receiver.DenonAVR.{attribute}",
        **patch_kwargs,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"select_host": TEST_HOST2},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "select"
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"select_host": TEST_HOST2},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == TEST_HOST2


@CANNOT_CONNECT
async def test_config_flow_ssdp_cannot_connect(
    hass: HomeAssistant, attribute: str, patch_kwargs: dict[str, Any]
) -> None:
    """A discovered receiver that cannot be reached can be confirmed again."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=SsdpServiceInfo(
            ssdp_usn="mock_usn",
            ssdp_st="mock_st",
            ssdp_location=TEST_SSDP_LOCATION,
            upnp={
                ATTR_UPNP_MANUFACTURER: TEST_MANUFACTURER,
                ATTR_UPNP_MODEL_NAME: TEST_MODEL,
                ATTR_UPNP_SERIAL: TEST_SERIALNUMBER,
            },
        ),
    )

    with patch(
        f"homeassistant.components.denonavr.receiver.DenonAVR.{attribute}",
        **patch_kwargs,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == TEST_HOST


async def test_config_flow_manual_host_already_configured(
    hass: HomeAssistant,
) -> None:
    """A receiver already set up under its unique ID is not added twice."""
    MockConfigEntry(domain=DOMAIN, unique_id=TEST_UNIQUE_ID).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: TEST_HOST2},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_config_flow_ssdp(hass: HomeAssistant) -> None:
    """Successful flow initialized by ssdp discovery."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=SsdpServiceInfo(
            ssdp_usn="mock_usn",
            ssdp_st="mock_st",
            ssdp_location=TEST_SSDP_LOCATION,
            upnp={
                ATTR_UPNP_MANUFACTURER: TEST_MANUFACTURER,
                ATTR_UPNP_MODEL_NAME: TEST_MODEL,
                ATTR_UPNP_SERIAL: TEST_SERIALNUMBER,
            },
        ),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
    }
    assert result["options"] == {CONF_USE_TELNET: True}


async def test_config_flow_ssdp_not_denon(hass: HomeAssistant) -> None:
    """Failed flow initialized by ssdp discovery.

    Not supported manufacturer.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=SsdpServiceInfo(
            ssdp_usn="mock_usn",
            ssdp_st="mock_st",
            ssdp_location=TEST_SSDP_LOCATION,
            upnp={
                ATTR_UPNP_MANUFACTURER: "NotSupported",
                ATTR_UPNP_MODEL_NAME: TEST_MODEL,
                ATTR_UPNP_SERIAL: TEST_SERIALNUMBER,
            },
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_denonavr_manufacturer"


async def test_config_flow_ssdp_missing_info(hass: HomeAssistant) -> None:
    """Failed flow initialized by ssdp discovery.

    Missing information.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=SsdpServiceInfo(
            ssdp_usn="mock_usn",
            ssdp_st="mock_st",
            ssdp_location=TEST_SSDP_LOCATION,
            upnp={
                ATTR_UPNP_MANUFACTURER: TEST_MANUFACTURER,
            },
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_denonavr_missing"


async def test_config_flow_ssdp_ignored_model(hass: HomeAssistant) -> None:
    """Failed flow initialized by ssdp discovery.

    Model in the ignored models list.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_SSDP},
        data=SsdpServiceInfo(
            ssdp_usn="mock_usn",
            ssdp_st="mock_st",
            ssdp_location=TEST_SSDP_LOCATION,
            upnp={
                ATTR_UPNP_MANUFACTURER: TEST_MANUFACTURER,
                ATTR_UPNP_MODEL_NAME: TEST_IGNORED_MODEL,
                ATTR_UPNP_SERIAL: TEST_SERIALNUMBER,
            },
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_denonavr_manufacturer"


async def test_options_flow(hass: HomeAssistant) -> None:
    """Test specifying non default settings using options flow."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_UNIQUE_ID,
        data={
            CONF_HOST: TEST_HOST,
            CONF_MODEL: TEST_MODEL,
            CONF_TYPE: TEST_RECEIVER_TYPE,
            CONF_MANUFACTURER: TEST_MANUFACTURER,
            CONF_SERIAL_NUMBER: TEST_SERIALNUMBER,
            CONF_UPDATE_AUDYSSEY: TEST_UPDATE_AUDYSSEY,
        },
        title=TEST_NAME,
    )
    config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SHOW_ALL_SOURCES: True,
            CONF_ZONE2: True,
            CONF_ZONE3: True,
            CONF_USE_TELNET: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_SHOW_ALL_SOURCES: True,
        CONF_ZONE2: True,
        CONF_ZONE3: True,
        CONF_UPDATE_AUDYSSEY: False,
        CONF_USE_TELNET: False,
    }


async def test_config_flow_manual_host_no_serial_double_config(
    hass: HomeAssistant,
) -> None:
    """Failed flow manually initialized by the user twice.

    Host specified and an error getting the serial number.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.receiver.DenonAVR.serial_number",
        None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: TEST_HOST},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == TEST_NAME
    assert result["data"] == {
        CONF_HOST: TEST_HOST,
        CONF_MODEL: TEST_MODEL,
        CONF_TYPE: TEST_RECEIVER_TYPE,
        CONF_MANUFACTURER: TEST_MANUFACTURER,
        CONF_SERIAL_NUMBER: None,
    }

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with patch(
        "homeassistant.components.denonavr.receiver.DenonAVR.serial_number",
        None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_HOST: TEST_HOST},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
