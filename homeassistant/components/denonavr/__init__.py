"""The Denon AVR Network Receivers integration."""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging

from denonavr import DenonAVR
from denonavr.const import ALL_TELNET_EVENTS
from denonavr.exceptions import DenonAvrError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_SHOW_ALL_SOURCES,
    CONF_UPDATE_AUDYSSEY,
    CONF_USE_TELNET,
    CONF_ZONE2,
    CONF_ZONE3,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_SHOW_SOURCES,
    DEFAULT_TIMEOUT,
    DEFAULT_UPDATE_AUDYSSEY,
    DEFAULT_USE_TELNET,
    DEFAULT_ZONE2,
    DEFAULT_ZONE3,
    DOMAIN,
    SETTINGS_TELNET_EVENT,
)
from .coordinator import (
    DenonAvrDataUpdateCoordinator,
    async_refresh_settings,
    async_refresh_status,
    mark_available,
    mark_unavailable,
)
from .receiver import ConnectDenonAVR
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


@dataclass
class DenonAvrData:
    """Runtime data for a Denon AVR config entry."""

    receiver: DenonAVR
    coordinator: DenonAvrDataUpdateCoordinator
    settings_coordinator: DenonAvrDataUpdateCoordinator


type DenonavrConfigEntry = ConfigEntry[DenonAvrData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DenonavrConfigEntry) -> bool:
    """Set up the denonavr components from a config entry."""
    # Connect to receiver
    connect_denonavr = ConnectDenonAVR(
        entry.data[CONF_HOST],
        DEFAULT_TIMEOUT,
        entry.options.get(CONF_SHOW_ALL_SOURCES, DEFAULT_SHOW_SOURCES),
        entry.options.get(CONF_ZONE2, DEFAULT_ZONE2),
        entry.options.get(CONF_ZONE3, DEFAULT_ZONE3),
        entry.options.get(CONF_USE_TELNET, DEFAULT_USE_TELNET),
        lambda: get_async_client(hass),
    )
    try:
        connected = await connect_denonavr.async_connect_receiver()
    except DenonAvrError as ex:
        # Anything the receiver raises here is it not answering properly yet,
        # so the entry retries rather than needing a manual reload.
        raise ConfigEntryNotReady from ex
    receiver = connect_denonavr.receiver
    assert receiver is not None
    if not connected:
        # Telnet is already up by now; a no-op without it.
        await receiver.async_telnet_disconnect()
        raise ConfigEntryNotReady(
            f"Receiver at {entry.data[CONF_HOST]} did not identify itself:"
            f" manufacturer '{receiver.manufacturer}', name '{receiver.name}',"
            f" model '{receiver.model_name}', type '{receiver.receiver_type}'"
        )

    update_audyssey = entry.options.get(CONF_UPDATE_AUDYSSEY, DEFAULT_UPDATE_AUDYSSEY)
    use_telnet = entry.options.get(CONF_USE_TELNET, DEFAULT_USE_TELNET)
    update_interval = timedelta(seconds=COORDINATOR_UPDATE_INTERVAL)

    # Serializes receiver access across both coordinators and every command.
    # Not derived from the receiver: denonavr's attrs classes are unhashable.
    lock = asyncio.Lock()

    coordinator = DenonAvrDataUpdateCoordinator(
        hass,
        entry,
        receiver,
        lock,
        name="status",
        update_interval=update_interval,
        refresh_fn=async_refresh_status,
    )
    # Reads only without Telnet: with it, receiver.py already read status
    # before connecting, so this is skipped. Either read failing is not ready.
    await coordinator.async_config_entry_first_refresh()

    def _watched() -> tuple[str | None, ...]:
        """Status values whose change leaves the settings stale."""
        return (receiver.input_func, receiver.sound_mode_raw)

    # As of the last settings read that returned: one that failed, or was
    # overtaken by a change, is retried on the next status refresh.
    read_under: tuple[str | None, ...] | None = None
    # What the pending re-read was requested for: a status refresh that sees
    # no further change must not restart its wait.
    requested_for: tuple[str | None, ...] | None = None

    async def _refresh_settings(receiver: DenonAVR, *, force: bool = False) -> None:
        nonlocal read_under, requested_for
        watched = _watched()
        try:
            await async_refresh_settings(receiver, force=force)
        finally:
            # Kept when a newer change is waiting for its own read.
            if requested_for == watched:
                requested_for = None
        read_under = watched

    settings_coordinator = DenonAvrDataUpdateCoordinator(
        hass,
        entry,
        receiver,
        lock,
        name="settings",
        # Opt-in because the fetch can take ~10s. It governs the recurring
        # poll alone: entities still confirm their own actions on demand.
        update_interval=update_interval if update_audyssey else None,
        refresh_fn=_refresh_settings,
    )
    coordinator.peer = settings_coordinator
    settings_coordinator.peer = coordinator

    @callback
    def _propagate_connectivity_to_settings() -> None:
        """Reflect the status coordinator's connectivity into this one.

        Only while this coordinator has no poll of its own. One that polls
        reads on its next interval, since this failure forces it to, and its
        own read is the better evidence either way.
        """
        if settings_coordinator.polls:
            return
        if settings_coordinator.last_update_success != coordinator.last_update_success:
            settings_coordinator.last_update_success = coordinator.last_update_success
            settings_coordinator.last_exception = coordinator.last_exception
            settings_coordinator.async_update_listeners()

    entry.async_on_unload(
        coordinator.async_add_internal_listener(_propagate_connectivity_to_settings)
    )

    @callback
    def _propagate_settings_failure_to_general() -> None:
        """Reflect a confirmed settings connectivity failure into the status one.

        Only while the status coordinator has no poll of its own. One that
        polls reads on its next interval even with Telnet healthy, since this
        failure forces it to. Failure only; recovery is that coordinator's own
        to confirm.
        """
        if not settings_coordinator.last_update_success and not coordinator.polls:
            err = settings_coordinator.last_exception
            assert isinstance(err, Exception)
            mark_unavailable(coordinator, err)

    entry.async_on_unload(
        settings_coordinator.async_add_internal_listener(
            _propagate_settings_failure_to_general
        )
    )

    # Nothing else populates these values: status queries skip them and Telnet
    # only pushes on a change. Forced, and after the listener above so a
    # failure reaches the status coordinator instead of failing setup.
    await settings_coordinator.async_refresh_forced()

    @callback
    def _refresh_settings_on_change() -> None:
        """Re-read the settings after an input source or sound mode change.

        The receiver stores the audio delay and some Audyssey settings per
        source, and denonavr forgets the delay on a change. Whether the LFE
        level can be set follows the sound mode and the stream: the raw mode,
        since the matched one is the same for streams with and without an LFE
        channel. Without the periodic poll nothing would read them again.
        Settled, because the receiver takes a few seconds to switch.
        """
        nonlocal requested_for
        watched = _watched()
        if watched in (read_under, requested_for):
            return
        requested_for = watched
        # Not forced, so a no-op while Telnet is healthy.
        settings_coordinator.async_request_settled_refresh()

    entry.async_on_unload(
        coordinator.async_add_internal_listener(_refresh_settings_on_change)
    )

    @callback
    def _telnet_notify_settings(zone: str, event: str, parameter: str) -> None:
        """Feed Telnet activity into the settings coordinator.

        On the receiver rather than on an entity, whose callback would
        only run while that entity is enabled.

        A push is the receiver answering, so it clears an earlier
        connectivity failure: without "Update audio settings periodically" there
        is no poll to clear one. It keeps the refresh a Dynamic EQ change queued,
        which brings the other zones' copies in step: Telnet never pushes those.
        """
        mark_available(settings_coordinator)

    receiver.register_callback(SETTINGS_TELNET_EVENT, _telnet_notify_settings)
    entry.async_on_unload(
        lambda: receiver.unregister_callback(
            SETTINGS_TELNET_EVENT, _telnet_notify_settings
        )
    )

    @callback
    def _telnet_notify_status(zone: str, event: str, parameter: str) -> None:
        """Feed Telnet activity into the status coordinator.

        Its poll skips while Telnet is healthy, so nothing else would follow
        a push.

        A push is the receiver answering, so it clears a failure.
        """
        # A now-playing or HD Radio change arrives as one event per field:
        # notifying on each would publish a new title with the old artist.
        if (event == "NSE" and not parameter.startswith("4")) or (
            event == "HD" and not parameter.startswith("ALBUM")
        ):
            return
        mark_available(coordinator)

    receiver.register_callback(ALL_TELNET_EVENTS, _telnet_notify_status)
    entry.async_on_unload(
        lambda: receiver.unregister_callback(ALL_TELNET_EVENTS, _telnet_notify_status)
    )

    entry.runtime_data = DenonAvrData(
        receiver=receiver,
        coordinator=coordinator,
        settings_coordinator=settings_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_disconnect(event: Event) -> None:
        """Disconnect from Telnet."""
        if use_telnet:
            await receiver.async_telnet_disconnect()

    if use_telnet:
        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_disconnect)
        )

    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: DenonavrConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if config_entry.options.get(CONF_USE_TELNET, DEFAULT_USE_TELNET):
        receiver = config_entry.runtime_data.receiver
        await receiver.async_telnet_disconnect()

    # Remove zone2 and zone3 entities if needed
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, config_entry.entry_id)
    unique_id = config_entry.unique_id or config_entry.entry_id
    zone2_id = f"{unique_id}-Zone2"
    zone3_id = f"{unique_id}-Zone3"
    for entry in entries:
        if entry.unique_id == zone2_id and not config_entry.options.get(CONF_ZONE2):
            entity_registry.async_remove(entry.entity_id)
            _LOGGER.debug("Removing zone2 from DenonAvr")
        if entry.unique_id == zone3_id and not config_entry.options.get(CONF_ZONE3):
            entity_registry.async_remove(entry.entity_id)
            _LOGGER.debug("Removing zone3 from DenonAvr")

    return unload_ok
