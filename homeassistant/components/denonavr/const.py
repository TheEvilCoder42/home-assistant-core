"""Constants for Denon AVR."""

from denonavr.const import ZONE2, ZONE3

DOMAIN = "denonavr"

ATTR_DYNAMIC_EQ = "dynamic_eq"

CONF_SHOW_ALL_SOURCES = "show_all_sources"
CONF_ZONE2 = "zone2"
CONF_ZONE3 = "zone3"
CONF_MANUFACTURER = "manufacturer"
CONF_SERIAL_NUMBER = "serial_number"
CONF_UPDATE_AUDYSSEY = "update_audyssey"
CONF_USE_TELNET = "use_telnet"

DEFAULT_SHOW_SOURCES = False
DEFAULT_TIMEOUT = 5
DEFAULT_ZONE2 = False
DEFAULT_ZONE3 = False
DEFAULT_UPDATE_AUDYSSEY = False
DEFAULT_USE_TELNET = False

# Seconds an optimistic pending value is trusted over the receiver's own,
# comfortably above the ~10s a settings fetch can take.
PENDING_VALUE_TIMEOUT = 15

# Shared by both coordinators, at the rate media_player.py polled at.
COORDINATOR_UPDATE_INTERVAL = 10

# Delay action-triggered refreshes so the receiver can settle and coalesce changes.
ACTION_REFRESH_DEBOUNCE_COOLDOWN = 0.5

# The event group the Audyssey settings and the audio delay arrive in.
# __init__.py notifies the settings coordinator on it, whether or not
# media_player is enabled.
SETTINGS_TELNET_EVENT = "PS"

# The receiver's dB display is its 0-98 step scale minus 80, so MV00 is -80.0.
VOLUME_MIN = -80.0

# How a secondary zone is spelled in the name of an entity that has
# one per zone. The main zone's entities are named without it.
ZONE_NAMES = {ZONE2: "Zone 2", ZONE3: "Zone 3"}
