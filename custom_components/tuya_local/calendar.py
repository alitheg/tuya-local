"""
Setup for Tuya pet feeder meal-plan calendar entities.

Presents the feeding schedule stored in a base64 ``mealplan`` dps as a
calendar: each enabled meal becomes a short event on the days it fires.
Create/update/delete are supported and re-encode the whole schedule back to
the device. Feed size rides in the event summary ("Feed N"), defaulting to
the device Meal size when unspecified; day selection comes from the event's
weekly RRULE.
"""

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
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
# Suffix used to render (and recognise) a disabled meal in an event summary.
DISABLED_SUFFIX = " (off)"
# Feed size bounds (matches the device Meal size / manual_feed range).
MIN_PORTIONS = 1
MAX_PORTIONS = 12
# Storage for named meal-plan presets saved in Home Assistant.
PLAN_STORAGE_VERSION = 1


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

    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT
        | CalendarEntityFeature.UPDATE_EVENT
        | CalendarEntityFeature.DELETE_EVENT
    )

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
        # Optional dps giving the default feed size for new/edited meals.
        self._meal_size_dps = dps_map.pop("meal_size", None)
        # Named meal-plan presets persisted in HA (name -> base64 payload).
        self._plan_storage = Store(
            device._hass,
            PLAN_STORAGE_VERSION,
            f"tuya_local_meal_plans_{device.unique_id}",
        )
        self._saved_plans: dict[str, str] = {}
        self._storage_loaded = False
        self._init_end(dps_map)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        await self._async_load_storage()

    async def _async_load_storage(self):
        """Load saved named plans from disk."""
        self._saved_plans = await self._plan_storage.async_load() or {}
        self._storage_loaded = True

    @property
    def extra_state_attributes(self):
        attr = super().extra_state_attributes
        return {**attr, "saved_plans": sorted(self._saved_plans)}

    def _plan(self) -> meal_plan.MealPlan:
        return meal_plan.decode_base64(self._schedule_dps.get_value(self._device))

    def _default_portions(self) -> int:
        """Feed size to use when an event does not specify one."""
        if self._meal_size_dps is not None:
            size = self._meal_size_dps.get_value(self._device)
            if isinstance(size, int) and MIN_PORTIONS <= size <= MAX_PORTIONS:
                return size
        return MIN_PORTIONS

    def _summary(self, slot: meal_plan.MealSlot) -> str:
        summary = f"Feed {slot.portions}"
        if not slot.enabled:
            summary += DISABLED_SUFFIX
        return summary

    def _make_event(self, slot: meal_plan.MealSlot, begin: datetime) -> CalendarEvent:
        return CalendarEvent(
            start=begin,
            end=begin + EVENT_DURATION,
            summary=self._summary(slot),
            description=f"{slot.portions} portion(s)",
            uid=slot.uid,
            rrule=meal_plan.rrule_from_mask(slot.days_mask),
        )

    async def _write(self, plan: meal_plan.MealPlan) -> None:
        """Persist the whole plan back to the device."""
        payload = meal_plan.encode_base64(plan)
        settings = self._schedule_dps.get_values_to_set(self._device, payload)
        await self._device.async_set_properties(settings)

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

    # --- Editing -----------------------------------------------------------

    def _slot_from_fields(
        self,
        dtstart: Any,
        summary: str | None,
        rrule: str | None,
        default_mask: int,
    ) -> meal_plan.MealSlot:
        """Build a MealSlot from calendar event fields."""
        if not isinstance(dtstart, datetime):
            raise HomeAssistantError("A feeding must have a start time")
        begin = dt_util.as_local(dtstart)
        portions = meal_plan.parse_portions(summary)
        if portions is None:
            portions = self._default_portions()
        portions = max(MIN_PORTIONS, min(MAX_PORTIONS, portions))
        enabled = not (summary or "").endswith(DISABLED_SUFFIX)
        mask = meal_plan.mask_from_rrule(rrule) if rrule else default_mask
        return meal_plan.MealSlot(mask, begin.hour, begin.minute, portions, enabled)

    @staticmethod
    def _upsert(
        plan: meal_plan.MealPlan,
        slot: meal_plan.MealSlot,
        replace_uid: str | None = None,
    ) -> None:
        """Add slot to plan, replacing any slot with the same or old uid."""
        drop = {slot.uid}
        if replace_uid:
            drop.add(replace_uid)
        plan.slots = [s for s in plan.slots if s.uid not in drop]
        plan.slots.append(slot)

    async def async_create_event(self, **kwargs: Any) -> None:
        """Add a new meal to the schedule."""
        slot = self._slot_from_fields(
            kwargs.get("dtstart"),
            kwargs.get("summary"),
            kwargs.get("rrule"),
            meal_plan.ALL_DAYS,
        )
        plan = self._plan()
        self._upsert(plan, slot)
        await self._write(plan)

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Remove a meal from the schedule."""
        plan = self._plan()
        plan.slots = [s for s in plan.slots if s.uid != uid]
        await self._write(plan)

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Change an existing meal. Applies to the whole series."""
        plan = self._plan()
        existing = next((s for s in plan.slots if s.uid == uid), None)
        default_mask = existing.days_mask if existing else meal_plan.ALL_DAYS
        slot = self._slot_from_fields(
            event.get("dtstart"),
            event.get("summary"),
            event.get("rrule"),
            default_mask,
        )
        self._upsert(plan, slot, replace_uid=uid)
        await self._write(plan)

    async def async_set_meal(
        self,
        meal_time: time,
        portions: int | None = None,
        days=None,
        enabled: bool = True,
    ) -> None:
        """Add or replace a meal by explicit fields (service-driven)."""
        if portions is None:
            portions = self._default_portions()
        portions = max(MIN_PORTIONS, min(MAX_PORTIONS, portions))
        mask = meal_plan.mask_from_day_names(days) if days else meal_plan.ALL_DAYS
        slot = meal_plan.MealSlot(
            mask, meal_time.hour, meal_time.minute, portions, enabled
        )
        plan = self._plan()
        self._upsert(plan, slot)
        await self._write(plan)

    # --- Named presets -----------------------------------------------------

    async def async_save_plan(self, name: str, force: bool = False) -> None:
        """Snapshot the device's current schedule under a name."""
        if not self._storage_loaded:
            await self._async_load_storage()
        if name in self._saved_plans and not force:
            raise HomeAssistantError(
                f"A meal plan named '{name}' already exists; pass force to overwrite"
            )
        payload = self._schedule_dps.get_value(self._device)
        if not payload:
            raise HomeAssistantError("No meal plan is currently available to save")
        self._saved_plans[name] = payload
        await self._plan_storage.async_save(self._saved_plans)
        self.async_write_ha_state()

    async def async_load_plan(self, name: str) -> None:
        """Replace the device's schedule with a saved named plan."""
        if not self._storage_loaded:
            await self._async_load_storage()
        payload = self._saved_plans.get(name)
        if payload is None:
            raise HomeAssistantError(f"No saved meal plan named '{name}'")
        settings = self._schedule_dps.get_values_to_set(self._device, payload)
        await self._device.async_set_properties(settings)

    async def async_delete_plan(self, name: str) -> None:
        """Delete a saved named plan."""
        if not self._storage_loaded:
            await self._async_load_storage()
        if name not in self._saved_plans:
            raise HomeAssistantError(f"No saved meal plan named '{name}'")
        del self._saved_plans[name]
        await self._plan_storage.async_save(self._saved_plans)
        self.async_write_ha_state()
