"""Tests for the Denon AVR Network Receivers integration."""

from homeassistant.components.denonavr.const import DOMAIN
from homeassistant.helpers import entity_registry as er

TEST_HOST = "1.2.3.4"
TEST_NAME = "Test_Receiver"
TEST_MODEL = "model5"
TEST_SERIALNUMBER = "123456789"
TEST_MANUFACTURER = "Denon"
TEST_RECEIVER_TYPE = "avr-x"
TEST_ZONE = "Main"
TEST_UNIQUE_ID = f"{TEST_MODEL}-{TEST_SERIALNUMBER}"


def get_entity_id(entity_registry: er.EntityRegistry, domain: str, key: str) -> str:
    """Return the entity_id of the receiver-level entity with this key."""
    entity_id = entity_registry.async_get_entity_id(
        domain, DOMAIN, f"{TEST_UNIQUE_ID}-{key}"
    )
    assert entity_id is not None
    return entity_id
