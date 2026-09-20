"""
Setup for Tuya pet feeder meal-plan calendar entities.

Presents the feeding schedule stored in a base64 ``mealplan`` dps as a
read-only calendar: each enabled meal becomes a short event on the days it
fires. Editing (create/update/delete) is planned as a follow-up.
"""

import logging
from datetime import datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .device import TuyaLocalDevice
from .entity import TuyaLocalEntity
from .helpers import meal_plan
from .helpers.config import async_tuya_setup_platform
from .helpers.device_config import TuyaEntityConfig

_LOGGER = logging.getLogger(__name__)

# Feeders dispense near-instantly, so events are short markers.
EVENT_DURATION = timedelta(minutes=1)
# How far ahead to search when reporting the next event.
LOOKAHEAD = timedelta(days=8)


async def async_setup_entry(hass, config_entry, async_add_entities):
    config = {**config_entry.data, **config_entry.options}
    await async_tuya_setup_platform(
        hass,
        async_add_entities,
        config,
        "calendar",
        TuyaLocalCalendar,
    )


class TuyaLocalCalendar(TuyaLocalEntity, CalendarEntity):
    """Representation of a feeder meal plan as a calendar."""

    def __init__(self, device: TuyaLocalDevice, config: TuyaEntityConfig):
        """
        Initialise the calendar.
        Args:
            device (TuyaLocalDevice): the device API instance.
            config (TuyaEntityConfig): the configuration for this entity.
        """
        super().__init__()
        dps_map = self._init_begin(device, config)
        self._schedule_dps = dps_map.pop("schedule", None)
        if self._schedule_dps is None:
            raise AttributeError(f"{config.config_id} is missing a schedule dps")
        self._init_end(dps_map)

    def _plan(self) -> meal_plan.MealPlan:
        return meal_plan.decode_base64(self._schedule_dps.get_value(self._device))

    def _make_event(self, slot: meal_plan.MealSlot, begin: datetime) -> CalendarEvent:
        return CalendarEvent(
            start=begin,
            end=begin + EVENT_DURATION,
            summary=f"Feed {slot.portions}",
            description=f"{slot.portions} portion(s)",
            # Times are unique within a plan, so hhmm is a stable id.
            uid=f"{slot.hour:02d}{slot.minute:02d}",
        )

    def _events_between(
        self, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Expand the enabled meals into concrete events within the window."""
        start = dt_util.as_local(start_date)
        end = dt_util.as_local(end_date)
        events: list[CalendarEvent] = []
        for slot in self._plan().slots:
            if not slot.enabled or not slot.isoweekdays:
                continue
            slot_time = time(slot.hour, slot.minute)
            day = start.date()
            while day <= end.date():
                if day.isoweekday() in slot.isoweekdays:
                    begin = datetime.combine(day, slot_time).replace(
                        tzinfo=dt_util.DEFAULT_TIME_ZONE
                    )
                    if start <= begin <= end:
                        events.append(self._make_event(slot, begin))
                day += timedelta(days=1)
        events.sort(key=lambda e: e.start)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming feeding event."""
        now = dt_util.now()
        upcoming = self._events_between(now, now + LOOKAHEAD)
        return upcoming[0] if upcoming else None

    async def async_get_events(
        self, hass, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return all feeding events within the requested window."""
        return self._events_between(start_date, end_date)
