"""
Tests for the pet feeder meal-plan codec.

These are pure-Python and do not require Home Assistant.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from custom_components.tuya_local.helpers import meal_plan

# Reference payloads captured from a real Catit Pixi Smart Feeder (43752).
# First sample: seven meals, all days, all disabled.
SAMPLE_1 = "fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA="
# Second sample: an eighth meal added at 13:13, Mon-Sat, the only enabled one.
SAMPLE_2 = "fwY6AQB/CAAEAH8NAAIAfg0NAQF/Dx4BAH8QDwIAfxEeAQB/Ex4CAA=="

TZ = ZoneInfo("UTC")


def test_decode_sample_1_layout():
    plan = meal_plan.decode_base64(SAMPLE_1)
    assert len(plan.slots) == 7
    times = [(s.hour, s.minute, s.portions) for s in plan.slots]
    assert times == [
        (6, 58, 1),
        (8, 0, 4),
        (13, 0, 2),
        (15, 30, 1),
        (16, 15, 2),
        (17, 30, 1),
        (19, 30, 2),
    ]
    # Every slot is every-day and disabled in this sample.
    for slot in plan.slots:
        assert slot.days_mask == meal_plan.ALL_DAYS
        assert slot.days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        assert slot.enabled is False


def test_decode_sample_2_enable_and_days():
    plan = meal_plan.decode_base64(SAMPLE_2)
    assert len(plan.slots) == 8
    # The 13:13 slot is the only enabled one, and excludes Sunday.
    enabled = [s for s in plan.slots if s.enabled]
    assert len(enabled) == 1
    slot = enabled[0]
    assert (slot.hour, slot.minute) == (13, 13)
    assert slot.days_mask == 0x7E
    assert "sun" not in slot.days
    assert slot.days == ["mon", "tue", "wed", "thu", "fri", "sat"]


def test_roundtrip_both_samples():
    for sample in (SAMPLE_1, SAMPLE_2):
        plan = meal_plan.decode_base64(sample)
        assert meal_plan.encode_base64(plan) == sample


def test_encode_sorts_by_time():
    plan = meal_plan.MealPlan(
        [
            meal_plan.MealSlot(meal_plan.ALL_DAYS, 19, 30, 2, True),
            meal_plan.MealSlot(meal_plan.ALL_DAYS, 6, 0, 1, True),
        ]
    )
    raw = meal_plan.encode(plan)
    # First record's hour byte should be the earlier meal (06:00).
    assert raw[1] == 6
    assert raw[6] == 19


def test_day_bit_order_matches_documentation():
    # Documented example: Monday, Wednesday, Sunday -> 0b01010001
    mask = meal_plan.mask_from_isoweekdays([1, 3, 7])
    assert mask == 0b01010001


def test_next_feed_picks_earliest_enabled():
    plan = meal_plan.decode_base64(SAMPLE_2)
    # A Monday at 00:00; only 13:13 (enabled, Mon-Sat) should fire.
    now = datetime(2026, 9, 21, 0, 0, tzinfo=TZ)  # 2026-09-21 is a Monday
    nxt = plan.next_feed(now)
    assert nxt == datetime(2026, 9, 21, 13, 13, tzinfo=TZ)


def test_next_feed_none_when_all_disabled():
    plan = meal_plan.decode_base64(SAMPLE_1)  # all disabled
    now = datetime(2026, 9, 21, 0, 0, tzinfo=TZ)
    assert plan.next_feed(now) is None


def test_next_occurrence_skips_excluded_day():
    # 13:13 Mon-Sat: on a Sunday it should roll to Monday.
    slot = meal_plan.MealSlot(0x7E, 13, 13, 1, True)
    sunday = datetime(2026, 9, 20, 0, 0, tzinfo=TZ)  # 2026-09-20 is a Sunday
    nxt = slot.next_occurrence(sunday)
    assert nxt == datetime(2026, 9, 21, 13, 13, tzinfo=TZ)


def test_next_occurrence_rolls_past_time_today():
    slot = meal_plan.MealSlot(meal_plan.ALL_DAYS, 8, 0, 1, True)
    now = datetime(2026, 9, 21, 9, 0, tzinfo=TZ)  # after 08:00
    nxt = slot.next_occurrence(now)
    assert nxt == datetime(2026, 9, 22, 8, 0, tzinfo=TZ)


def test_decode_empty_and_none():
    assert meal_plan.decode_base64(None).slots == []
    assert meal_plan.decode_base64("").slots == []
    assert meal_plan.decode(b"").slots == []


def test_decode_bad_base64_is_safe():
    assert meal_plan.decode_base64("not valid base64!!!").slots == []


def test_decode_truncates_partial_record():
    # 7 bytes = one full record + 2 stray bytes; stray bytes are dropped.
    plan = meal_plan.decode(bytes([0x7F, 6, 58, 1, 0, 0x7F, 8]))
    assert len(plan.slots) == 1
    assert (plan.slots[0].hour, plan.slots[0].minute) == (6, 58)


def test_parse_portions():
    assert meal_plan.parse_portions("Feed 3") == 3
    assert meal_plan.parse_portions("3") == 3
    assert meal_plan.parse_portions("x2 portions") == 2
    assert meal_plan.parse_portions("Breakfast") is None
    assert meal_plan.parse_portions("") is None
    assert meal_plan.parse_portions(None) is None


def test_mask_from_rrule():
    assert meal_plan.mask_from_rrule(None) == meal_plan.ALL_DAYS
    assert meal_plan.mask_from_rrule("FREQ=WEEKLY") == meal_plan.ALL_DAYS
    # Mon, Wed, Sun -> documented 0b01010001
    assert meal_plan.mask_from_rrule("FREQ=WEEKLY;BYDAY=MO,WE,SU") == 0b01010001
    # Mon-Sat (no Sunday) -> 0x7e
    assert meal_plan.mask_from_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA") == 0x7E


def test_rrule_from_mask_roundtrips():
    assert meal_plan.rrule_from_mask(meal_plan.ALL_DAYS) is None
    rrule = meal_plan.rrule_from_mask(0x7E)
    assert rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA"
    assert meal_plan.mask_from_rrule(rrule) == 0x7E


def test_slot_uid():
    assert meal_plan.MealSlot(meal_plan.ALL_DAYS, 6, 58, 1).uid == "0658"
    assert meal_plan.MealSlot(meal_plan.ALL_DAYS, 13, 13, 1).uid == "1313"


def test_as_attributes_shape():
    plan = meal_plan.decode_base64(SAMPLE_2)
    attrs = plan.as_attributes()
    assert attrs["meal_count"] == 8
    assert attrs["enabled_count"] == 1
    assert attrs["meals"][3] == {
        "time": "13:13",
        "portions": 1,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat"],
        "enabled": True,
    }
