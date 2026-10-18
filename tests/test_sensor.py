"""Tests for the sensor entity."""

from unittest.mock import AsyncMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local.const import (
    CONF_DEVICE_ID,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)
from custom_components.tuya_local.helpers.device_config import TuyaEntityConfig
from custom_components.tuya_local.sensor import TuyaLocalSensor, async_setup_entry


@pytest.mark.asyncio
async def test_init_entry(hass):
    """Test the initialisation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: "goldair_dehumidifier",
            CONF_DEVICE_ID: "dummy",
            CONF_PROTOCOL_VERSION: "auto",
        },
    )
    m_add_entities = Mock()
    m_device = AsyncMock()

    hass.data[DOMAIN] = {
        "dummy": {"device": m_device},
    }

    await async_setup_entry(hass, entry, m_add_entities)
    assert type(hass.data[DOMAIN]["dummy"]["sensor_temperature"]) is TuyaLocalSensor
    m_add_entities.assert_called_once()


@pytest.mark.asyncio
async def test_init_entry_fails_if_device_has_no_sensor(hass):
    """Test initialisation when device has no matching entity"""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: "mirabella_genio_usb",
            CONF_DEVICE_ID: "dummy",
            CONF_PROTOCOL_VERSION: "auto",
        },
    )
    m_add_entities = Mock()
    m_device = AsyncMock()

    hass.data[DOMAIN] = {
        "dummy": {"device": m_device},
    }
    try:
        await async_setup_entry(hass, entry, m_add_entities)
        assert False
    except ValueError:
        pass
    m_add_entities.assert_not_called()


@pytest.mark.asyncio
async def test_init_entry_fails_if_config_is_missing(hass):
    """Test initialisation when device has no matching entity"""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: "non_existing",
            CONF_DEVICE_ID: "dummy",
            CONF_PROTOCOL_VERSION: "auto",
        },
    )
    m_add_entities = Mock()
    m_device = AsyncMock()

    hass.data[DOMAIN] = {
        "dummy": {"device": m_device},
    }
    try:
        await async_setup_entry(hass, entry, m_add_entities)
        assert False
    except ValueError:
        pass
    m_add_entities.assert_not_called()


def test_sensor_suggested_display_precision():
    mock_device = Mock()
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "sensor",
            "dps": [
                {
                    "id": 1,
                    "name": "sensor",
                    "type": "integer",
                    "precision": 1,
                }
            ],
        },
    )
    sensor = TuyaLocalSensor(mock_device, config)
    assert sensor.suggested_display_precision == 1
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "sensor",
            "dps": [{"id": 1, "name": "sensor", "type": "integer"}],
        },
    )
    sensor = TuyaLocalSensor(mock_device, config)
    assert sensor.suggested_display_precision == 0
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "sensor",
            "dps": [
                {
                    "id": 1,
                    "name": "sensor",
                    "type": "integer",
                    "mapping": [{"scale": 10}],
                },
            ],
        },
    )
    sensor = TuyaLocalSensor(mock_device, config)
    assert sensor.suggested_display_precision == 1


# Reference payloads captured from a real Catit Pixi Smart Feeder (43752).
# SAMPLE_MEALPLAN: 8 meals; the 13:13 slot (Mon-Sat) is the only enabled one.
SAMPLE_MEALPLAN = "fwY6AQB/CAAEAH8NAAIAfg0NAQF/Dx4BAH8QDwIAfxEeAQB/Ex4CAA=="
# SAMPLE_MEALPLAN_DISABLED: 7 meals, all every-day but all disabled.
SAMPLE_MEALPLAN_DISABLED = "fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA="


def _mealplan_sensor(payload):
    mock_device = Mock()
    mock_device.get_property.return_value = payload
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "sensor",
            "class": "timestamp",
            "dps": [{"id": 1, "name": "sensor", "type": "mealplan"}],
        },
    )
    return TuyaLocalSensor(mock_device, config)


def test_mealplan_sensor_exposes_schedule_attributes():
    sensor = _mealplan_sensor(SAMPLE_MEALPLAN)
    attr = sensor.extra_state_attributes
    assert attr["meal_count"] == 8
    assert attr["enabled_count"] == 1
    assert attr["meals"][3] == {
        "time": "13:13",
        "portions": 1,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
        "enabled": True,
    }


def test_mealplan_sensor_next_feed_is_future_datetime():
    from datetime import datetime

    sensor = _mealplan_sensor(SAMPLE_MEALPLAN)
    value = sensor.native_value
    assert isinstance(value, datetime)
    assert value.tzinfo is not None


def test_mealplan_sensor_next_feed_none_when_all_disabled():
    sensor = _mealplan_sensor(SAMPLE_MEALPLAN_DISABLED)
    assert sensor.native_value is None
    # Attributes still list the (disabled) meals.
    assert sensor.extra_state_attributes["meal_count"] == 7
    assert sensor.extra_state_attributes["enabled_count"] == 0
