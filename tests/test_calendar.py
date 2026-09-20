"""Tests for the meal-plan calendar entity."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_local.calendar import (
    TuyaLocalCalendar,
    async_setup_entry,
)
from custom_components.tuya_local.const import (
    CONF_DEVICE_ID,
    CONF_PROTOCOL_VERSION,
    CONF_TYPE,
    DOMAIN,
)
from custom_components.tuya_local.helpers.device_config import TuyaEntityConfig

# 8 meals; the 13:13 slot (Mon-Sat) is the only enabled one.
SAMPLE_MEALPLAN = "fwY6AQB/CAAEAH8NAAIAfg0NAQF/Dx4BAH8QDwIAfxEeAQB/Ex4CAA=="
# 7 meals, all every-day but all disabled.
SAMPLE_MEALPLAN_DISABLED = "fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA="


@pytest.mark.asyncio
async def test_init_entry(hass):
    """The calendar entity is created for the Catit Pixi Smart Feeder."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: "catit_pixi_smart_feeder",
            CONF_DEVICE_ID: "dummy",
            CONF_PROTOCOL_VERSION: "auto",
        },
    )
    m_add_entities = Mock()
    m_device = AsyncMock()
    hass.data[DOMAIN] = {"dummy": {"device": m_device}}

    await async_setup_entry(hass, entry, m_add_entities)
    added = [
        e
        for e in hass.data[DOMAIN]["dummy"].values()
        if isinstance(e, TuyaLocalCalendar)
    ]
    assert added
    m_add_entities.assert_called()


def _calendar(payload):
    mock_device = Mock()
    mock_device.get_property.return_value = payload
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "calendar",
            "dps": [{"id": 1, "name": "schedule", "type": "mealplan"}],
        },
    )
    return TuyaLocalCalendar(mock_device, config)


async def test_get_events_expands_only_enabled_meals(hass):
    cal = _calendar(SAMPLE_MEALPLAN)
    tz = dt_util.DEFAULT_TIME_ZONE
    start = datetime(2026, 9, 21, 0, 0, tzinfo=tz)  # a Monday
    end = start + timedelta(days=7)
    events = await cal.async_get_events(hass, start, end)
    # Only the enabled 13:13 Mon-Sat meal fires: Mon-Sat within the window,
    # so 6 events (Sunday excluded, and the next Monday is at the boundary).
    assert len(events) == 6
    assert all(isinstance(e, CalendarEvent) for e in events)
    assert all(e.start.hour == 13 and e.start.minute == 13 for e in events)
    assert events[0].summary == "Feed 1"
    # Events are sorted ascending.
    assert events == sorted(events, key=lambda e: e.start)


async def test_get_events_empty_when_all_disabled(hass):
    cal = _calendar(SAMPLE_MEALPLAN_DISABLED)
    tz = dt_util.DEFAULT_TIME_ZONE
    start = datetime(2026, 9, 21, 0, 0, tzinfo=tz)
    end = start + timedelta(days=7)
    assert await cal.async_get_events(hass, start, end) == []


def test_event_property_returns_upcoming(hass):
    cal = _calendar(SAMPLE_MEALPLAN)
    nxt = cal.event
    assert isinstance(nxt, CalendarEvent)
    assert nxt.start.hour == 13 and nxt.start.minute == 13


def test_event_property_none_when_all_disabled(hass):
    cal = _calendar(SAMPLE_MEALPLAN_DISABLED)
    assert cal.event is None
