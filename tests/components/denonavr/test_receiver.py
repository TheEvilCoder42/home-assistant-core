"""Test the ConnectDenonAVR helper directly."""

from homeassistant.components.denonavr.receiver import ConnectDenonAVR


def test_zones_dict_includes_enabled_zones() -> None:
    """Zone2/Zone3 are only added to the zones dict when their option is on."""
    connect = ConnectDenonAVR(
        "1.2.3.4",
        2,
        False,
        True,
        True,
        False,
        False,
        lambda: None,
    )

    assert connect._zones == {"Zone2": None, "Zone3": None}


def test_zones_dict_excludes_disabled_zones() -> None:
    """No extra zones are added when both options are off."""
    connect = ConnectDenonAVR(
        "1.2.3.4",
        2,
        False,
        False,
        False,
        False,
        False,
        lambda: None,
    )

    assert connect._zones == {}
