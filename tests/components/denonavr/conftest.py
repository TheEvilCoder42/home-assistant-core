"""Fixtures shared across denonavr tests."""

from collections import defaultdict
from collections.abc import Callable, Generator
from unittest.mock import MagicMock, patch

from denonavr.const import ALL_TELNET_EVENTS
import pytest

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
