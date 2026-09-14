"""Test how the denonavr integration connects to the receiver."""

from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.denonavr.const import CONF_ZONE2, CONF_ZONE3
from homeassistant.core import HomeAssistant

from . import setup_denonavr


@pytest.mark.parametrize(
    ("zone2", "zone3", "add_zones"),
    [
        pytest.param(False, False, {}, id="main_only"),
        pytest.param(True, False, {"Zone2": None}, id="zone2"),
        pytest.param(False, True, {"Zone3": None}, id="zone3"),
        pytest.param(True, True, {"Zone2": None, "Zone3": None}, id="both"),
    ],
)
async def test_zone_options_reach_the_library(
    hass: HomeAssistant,
    client: MagicMock,
    zone2: bool,
    zone3: bool,
    add_zones: dict[str, str | None],
) -> None:
    """The library is asked for Zone2/Zone3 only when their option is on."""
    with patch(
        "homeassistant.components.denonavr.receiver.DenonAVR", return_value=client
    ) as denonavr_class:
        await setup_denonavr(hass, options={CONF_ZONE2: zone2, CONF_ZONE3: zone3})

    assert denonavr_class.call_args.kwargs["add_zones"] == add_zones
