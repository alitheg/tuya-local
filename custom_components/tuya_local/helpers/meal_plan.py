"""
Decode and encode the meal-plan schedule used by some Tuya pet feeders.

The schedule is stored in a single base64-encoded dps as a variable-length
array of fixed length records, kept sorted by time of day. Each record is:

    byte 0: day-of-week bitmask (MSB padded with 0; bit6..bit0 = Mon..Sun)
    byte 1: hour     (0-23)
    byte 2: minute   (0-59)
    byte 3: portions / feed size (device dependent range)
    byte 4: enable flag (1 = enabled, 0 = disabled)

The layout is documented in the Catit Pixi feeder configs; see
catit_pixi_6meal_feeder.yaml and catit_pixi_smart_feeder.yaml.
"""

from __future__ import annotations

import logging
import re
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

RECORD_LEN = 5

# RRULE BYDAY two-letter codes <-> ISO weekday (Mon=1 .. Sun=7).
_BYDAY_ISO = {"MO": 1, "TU": 2, "WE": 3, "TH": 4, "FR": 5, "SA": 6, "SU": 7}
_ISO_BYDAY = {iso: code for code, iso in _BYDAY_ISO.items()}

_PORTIONS_RE = re.compile(r"\d+")

# bit index within the day mask -> ISO weekday number (Mon=1 .. Sun=7), per
# the order documented in catit_pixi_6meal_feeder.yaml
# (Ex: Monday, Wednesday, Sunday -> 0b01010001).
_BIT_ISO = {6: 1, 5: 2, 4: 3, 3: 4, 2: 5, 1: 6, 0: 7}
_ISO_BIT = {iso: bit for bit, iso in _BIT_ISO.items()}
_ISO_ABBR = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}

ALL_DAYS = 0x7F


@dataclass
class MealSlot:
    """A single scheduled feeding."""

    days_mask: int
    hour: int
    minute: int
    portions: int
    enabled: bool = True

    @property
    def isoweekdays(self) -> list[int]:
        """ISO weekdays (Mon=1..Sun=7) this slot fires on, sorted."""
        return sorted(
            iso for bit, iso in _BIT_ISO.items() if self.days_mask & (1 << bit)
        )

    @property
    def days(self) -> list[str]:
        """Lower-case three letter day abbreviations this slot fires on."""
        return [_ISO_ABBR[iso] for iso in self.isoweekdays]

    @property
    def time_str(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def uid(self) -> str:
        """Stable id for this slot; times are unique within a plan."""
        return f"{self.hour:02d}{self.minute:02d}"

    def as_dict(self) -> dict:
        return {
            "time": self.time_str,
            "portions": self.portions,
            "days": self.days,
            "enabled": self.enabled,
        }

    def next_occurrence(self, now: datetime) -> datetime | None:
        """
        Return the next datetime at or after now when this slot fires.

        Disabled slots, and slots with no days selected, never fire.
        """
        if not self.enabled or not self.isoweekdays:
            return None
        for offset in range(8):
            day = now + timedelta(days=offset)
            if day.isoweekday() in self.isoweekdays:
                candidate = day.replace(
                    hour=self.hour,
                    minute=self.minute,
                    second=0,
                    microsecond=0,
                )
                if candidate >= now:
                    return candidate
        return None


@dataclass
class MealPlan:
    """A feeder's full schedule, as a list of slots."""

    slots: list[MealSlot] = field(default_factory=list)

    def next_feed(self, now: datetime) -> datetime | None:
        """Return the next enabled feeding at or after now, if any."""
        times = [occ for occ in (s.next_occurrence(now) for s in self.slots) if occ]
        return min(times) if times else None

    def as_attributes(self) -> dict:
        """Return a JSON-friendly summary suitable for entity attributes."""
        enabled = [s for s in self.slots if s.enabled]
        return {
            "meals": [s.as_dict() for s in self.slots],
            "meal_count": len(self.slots),
            "enabled_count": len(enabled),
        }


def decode(raw: bytes | bytearray | None) -> MealPlan:
    """Decode raw schedule bytes into a MealPlan."""
    if not raw:
        return MealPlan()
    remainder = len(raw) % RECORD_LEN
    if remainder:
        _LOGGER.warning(
            "meal plan payload length %d is not a multiple of %d; ignoring "
            "trailing %d byte(s)",
            len(raw),
            RECORD_LEN,
            remainder,
        )
        raw = raw[: len(raw) - remainder]
    slots = []
    for i in range(0, len(raw), RECORD_LEN):
        mask, hour, minute, portions, flag = raw[i : i + RECORD_LEN]
        slots.append(MealSlot(mask, hour, minute, portions, flag == 1))
    return MealPlan(slots)


def decode_base64(value: str | bytes | bytearray | None) -> MealPlan:
    """Decode a base64 string (or raw bytes) into a MealPlan."""
    if value is None:
        return MealPlan()
    if isinstance(value, str):
        try:
            # binascii.Error (raised by b64decode) subclasses ValueError.
            value = b64decode(value)
        except ValueError:
            _LOGGER.warning("could not decode base64 meal plan %r", value)
            return MealPlan()
    return decode(value)


def encode(plan: MealPlan) -> bytes:
    """Encode a MealPlan back into raw schedule bytes, sorted by time."""
    out = bytearray()
    for s in sorted(plan.slots, key=lambda s: (s.hour, s.minute)):
        out += bytes(
            [
                s.days_mask & 0xFF,
                s.hour & 0xFF,
                s.minute & 0xFF,
                s.portions & 0xFF,
                1 if s.enabled else 0,
            ]
        )
    return bytes(out)


def encode_base64(plan: MealPlan) -> str:
    """Encode a MealPlan back into a base64 string for the device."""
    return b64encode(encode(plan)).decode("utf-8")


def mask_from_isoweekdays(isoweekdays) -> int:
    """Build a day bitmask from an iterable of ISO weekdays (Mon=1..Sun=7)."""
    mask = 0
    for iso in isoweekdays:
        if iso in _ISO_BIT:
            mask |= 1 << _ISO_BIT[iso]
    return mask


def parse_portions(summary: str | None) -> int | None:
    """Pull a feed size out of a calendar event summary (e.g. "Feed 3")."""
    if not summary:
        return None
    match = _PORTIONS_RE.search(summary)
    return int(match.group()) if match else None


def mask_from_rrule(rrule: str | None) -> int:
    """
    Derive a day bitmask from an RRULE string's BYDAY part.

    A missing rrule or a weekly rule with no BYDAY means every day.
    """
    if not rrule:
        return ALL_DAYS
    for part in rrule.split(";"):
        key, _, value = part.partition("=")
        if key.strip().upper() == "BYDAY" and value:
            isodays = []
            for code in value.split(","):
                # Strip any leading ordinal (e.g. "2MO") - unusual here.
                two = code.strip()[-2:].upper()
                if two in _BYDAY_ISO:
                    isodays.append(_BYDAY_ISO[two])
            if isodays:
                return mask_from_isoweekdays(isodays)
    return ALL_DAYS


def rrule_from_mask(mask: int) -> str | None:
    """Return a weekly RRULE for the given day mask, or None if every day."""
    if mask & ALL_DAYS == ALL_DAYS:
        return None
    isodays = sorted(iso for bit, iso in _BIT_ISO.items() if mask & (1 << bit))
    if not isodays:
        return None
    byday = ",".join(_ISO_BYDAY[iso] for iso in isodays)
    return f"FREQ=WEEKLY;BYDAY={byday}"
