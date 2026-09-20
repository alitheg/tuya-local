"""Tests for the meal-plan calendar entity."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.exceptions import HomeAssistantError
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


# --- Editing ---------------------------------------------------------------


def _editable_calendar(payload, meal_size=3):
    mock_device = Mock()
    props = {"1": payload, "101": meal_size}
    mock_device.get_property.side_effect = lambda i: props.get(str(i))
    mock_device.async_set_properties = AsyncMock()
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "calendar",
            "dps": [
                {"id": 1, "name": "schedule", "type": "mealplan"},
                {
                    "id": 101,
                    "name": "meal_size",
                    "type": "integer",
                    "range": {"min": 1, "max": 12},
                },
            ],
        },
    )
    return TuyaLocalCalendar(mock_device, config), mock_device


def _written_plan(mock_device):
    from custom_components.tuya_local.helpers import meal_plan

    payload = mock_device.async_set_properties.call_args.args[0]["1"]
    return meal_plan.decode_base64(payload)


async def test_create_event_adds_meal(hass):
    cal, dev = _editable_calendar(SAMPLE_MEALPLAN)
    tz = dt_util.DEFAULT_TIME_ZONE
    await cal.async_create_event(
        dtstart=datetime(2026, 9, 21, 7, 0, tzinfo=tz),
        dtend=datetime(2026, 9, 21, 7, 1, tzinfo=tz),
        summary="Feed 2",
    )
    plan = _written_plan(dev)
    assert len(plan.slots) == 9
    new = next(s for s in plan.slots if s.uid == "0700")
    assert new.portions == 2
    assert new.enabled is True
    assert new.days_mask == 0x7F  # no rrule -> every day


async def test_create_event_defaults_portions_to_meal_size(hass):
    cal, dev = _editable_calendar(SAMPLE_MEALPLAN, meal_size=4)
    tz = dt_util.DEFAULT_TIME_ZONE
    await cal.async_create_event(
        dtstart=datetime(2026, 9, 21, 9, 0, tzinfo=tz),
        summary="breakfast",  # no number -> use device meal size
    )
    new = next(s for s in _written_plan(dev).slots if s.uid == "0900")
    assert new.portions == 4


async def test_delete_event_removes_meal(hass):
    cal, dev = _editable_calendar(SAMPLE_MEALPLAN)
    await cal.async_delete_event("1313")
    plan = _written_plan(dev)
    assert all(s.uid != "1313" for s in plan.slots)
    assert len(plan.slots) == 7


async def test_update_event_changes_time_size_and_days(hass):
    cal, dev = _editable_calendar(SAMPLE_MEALPLAN)
    tz = dt_util.DEFAULT_TIME_ZONE
    await cal.async_update_event(
        "0658",
        {
            "dtstart": datetime(2026, 9, 21, 6, 30, tzinfo=tz),
            "summary": "Feed 5",
            "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
        },
    )
    plan = _written_plan(dev)
    assert all(s.uid != "0658" for s in plan.slots)
    moved = next(s for s in plan.slots if s.uid == "0630")
    assert moved.portions == 5
    assert moved.days_mask == meal_plan_mask_mon_fri()


def meal_plan_mask_mon_fri():
    from custom_components.tuya_local.helpers import meal_plan

    return meal_plan.mask_from_isoweekdays([1, 2, 3, 4, 5])


# --- Named presets (save/load/delete) --------------------------------------


def _storage_calendar(hass, payload=SAMPLE_MEALPLAN):
    mock_device = Mock()
    props = {"1": payload, "101": 3}
    mock_device.get_property.side_effect = lambda i: props.get(str(i))
    mock_device._hass = hass
    mock_device.unique_id = "dummy-uid"
    mock_device.async_set_properties = AsyncMock()
    config = TuyaEntityConfig(
        mock_device,
        {
            "entity": "calendar",
            "dps": [
                {"id": 1, "name": "schedule", "type": "mealplan"},
                {
                    "id": 101,
                    "name": "meal_size",
                    "type": "integer",
                    "range": {"min": 1, "max": 12},
                },
            ],
        },
    )
    cal = TuyaLocalCalendar(mock_device, config)
    # async_write_ha_state needs a fully registered entity; stub it here.
    cal.async_write_ha_state = Mock()
    return cal, mock_device


async def test_save_plan_lists_in_attributes(hass):
    cal, _ = _storage_calendar(hass)
    await cal.async_save_plan("Weekday")
    assert cal.extra_state_attributes["saved_plans"] == ["Weekday"]


async def test_save_existing_requires_force(hass):
    cal, _ = _storage_calendar(hass)
    await cal.async_save_plan("Weekday")
    with pytest.raises(HomeAssistantError):
        await cal.async_save_plan("Weekday")
    # With force it succeeds.
    await cal.async_save_plan("Weekday", force=True)


async def test_load_plan_writes_saved_payload(hass):
    cal, dev = _storage_calendar(hass)
    await cal.async_save_plan("snapshot")
    dev.async_set_properties.reset_mock()
    await cal.async_load_plan("snapshot")
    written = dev.async_set_properties.call_args.args[0]["1"]
    assert written == SAMPLE_MEALPLAN


async def test_load_missing_plan_raises(hass):
    cal, _ = _storage_calendar(hass)
    with pytest.raises(HomeAssistantError):
        await cal.async_load_plan("does-not-exist")


async def test_delete_plan(hass):
    cal, _ = _storage_calendar(hass)
    await cal.async_save_plan("temp")
    await cal.async_delete_plan("temp")
    assert cal.extra_state_attributes["saved_plans"] == []
    with pytest.raises(HomeAssistantError):
        await cal.async_delete_plan("temp")
