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

# How long a change keeps moving other values. Measured on an AVR-X1700H:
# everything a change moved had settled within ~4.3 s of the command.
SETTLED_REFRESH_DELAY = 5

# The event groups the settings coordinator's own settings arrive in: "PS"
# for the Audyssey values and the audio delay, "SP" for the speaker preset.
# __init__.py notifies it on them, whether or not media_player is enabled.
# Auto lip sync arrives on OP or SS instead, so its switch follows the
# status coordinator.
SETTINGS_TELNET_EVENTS = ("PS", "SP")

# The receiver's dB display is its 0-98 step scale minus 80, so MV00 is -80.0.
VOLUME_MIN = -80.0

# The zone number in the name of a secondary zone's entities. The word
# "Zone" stays in the translated string; the main zone's entities have no number.
ZONE_NUMBERS = {ZONE2: "2", ZONE3: "3"}
