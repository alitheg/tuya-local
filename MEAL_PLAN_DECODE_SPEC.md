# Design spec: smart decode for the pet-feeder meal plan

**Status:** Draft / design proposal (working document — not the final PR content)
**Device:** `custom_components/tuya_local/devices/welltobe_cat_feeder.yaml`
(WellToBe WB S36D, product `qqzpxhisd6zs8zyq`) and other Tuya feeders that
use the same `meal_plan` DP encoding.
**Goal (agreed):** decode the raw base64 meal-plan DP into a readable
schedule, expose it as **both** an enriched sensor **and** an editable
**calendar** entity, with full **round-trip** editing from Home Assistant,
targeting an **upstream PR** to `make-all/tuya-local`.

---

## 1. Current state

DP `1` (`meal_plan`) is currently exposed as a *hidden* `text` entity:

```yaml
  - entity: text
    translation_key: meal_plan
    hidden: true
    category: config
    dps:
      - id: 1
        type: base64
        optional: true
        name: value
```

So Home Assistant receives the raw base64 string
(`fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA=`) and does nothing with
it. The user sees an opaque blob.

## 2. Decoded payload format

The base64 decodes to **35 bytes = 7 fixed-width records of 5 bytes** each
(35 / 5 = 7 exactly; 35 is not divisible by 6, so the 5-byte width is
firmly established). Each record is:

```
byte 0: day-of-week bitmask   (0x7f in every record = all 7 days)
byte 1: hour        (0-23)
byte 2: minute      (0-59)
byte 3: portions    (1-6, matches the manual_feed number range on this device)
byte 4: flag byte   (0x00 in this sample — see open questions)
```

Sample decoded:

| # | mask | hour | min | portions | flag | reading                |
|---|------|------|-----|----------|------|------------------------|
| 1 | 0x7f | 06   | 58  | 1        | 0    | daily 06:58, 1 portion |
| 2 | 0x7f | 08   | 00  | 4        | 0    | daily 08:00, 4 portions|
| 3 | 0x7f | 13   | 00  | 2        | 0    | daily 13:00, 2 portions|
| 4 | 0x7f | 15   | 30  | 1        | 0    | daily 15:30, 1 portion |
| 5 | 0x7f | 16   | 15  | 2        | 0    | daily 16:15, 2 portions|
| 6 | 0x7f | 17   | 30  | 1        | 0    | daily 17:30, 1 portion |
| 7 | 0x7f | 19   | 30  | 2        | 0    | daily 19:30, 2 portions|

Confidence is high on the record width and on the hour/minute/portions
fields (hours are monotonically increasing, minutes are all valid 0-59,
portions are all within the device's advertised 1-6 range). The **day
bitmask** and the **flag byte** need one confirming sample — see §9.

Reference decoder (validated against the sample above):

```python
import base64

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]  # bit order TBC (§9)

def decode_meal_plan(b64: str) -> list[dict]:
    raw = base64.b64decode(b64)
    if len(raw) % 5 != 0:
        raise ValueError(f"unexpected length {len(raw)}")
    meals = []
    for i in range(0, len(raw), 5):
        mask, hour, minute, portions, flag = raw[i : i + 5]
        meals.append(
            {
                "days_mask": mask,
                "days": [d for b, d in enumerate(DAYS) if mask & (1 << b)],
                "hour": hour,
                "minute": minute,
                "portions": portions,
                "flag": flag,
            }
        )
    return meals

def encode_meal_plan(meals: list[dict]) -> str:
    out = bytearray()
    for m in sorted(meals, key=lambda m: (m["hour"], m["minute"])):
        out += bytes(
            [m["days_mask"], m["hour"], m["minute"], m["portions"], m.get("flag", 0)]
        )
    return base64.b64encode(bytes(out)).decode()
```

## 3. Why the existing YAML mechanisms are not enough

- The `format:` decoder in `helpers/device_config.py` handles a **single
  fixed struct** of named fields and is **only consumed by `light.py`**
  (RGBHSV colour). It cannot express a **variable-length array of records**,
  and it does not drive sensor/text display. So it is not reusable here as-is.
- There is **no `calendar` platform** in the integration today. The schema
  `entity` enum (`device_config_schema.json`) does not list `calendar`, and
  there is no `calendar.py`. Platforms are enumerated dynamically from the
  entity types present in configs (`__init__.py` builds the `entities` set
  and calls `async_forward_entry_setups`), so once `calendar.py` exists and
  the schema allows it, wiring is automatic.

Therefore this needs **new Python**, not just a YAML tweak. That is
acceptable upstream, but should be built as **generic, reusable
machinery**, not a device-specific hack (see §8).

## 4. Proposed design

Three layers, built bottom-up. Layers A + B are shippable independently of C.

### A. A reusable meal-plan codec (core)

Add a small, well-tested helper (e.g.
`helpers/meal_plan.py`) that converts between the raw bytes and a normalised
Python structure, parameterised so other feeders with the same shape can
reuse it:

- `decode(raw: bytes, *, record_len=5, fields=[...]) -> list[MealSlot]`
- `encode(slots: list[MealSlot], *, ...) -> bytes`

`MealSlot` = dataclass `{days_mask, hour, minute, portions, flag}`.

The existing DPS pipeline already base64-decodes/encodes (`decode_value`
/ `encode_value` for `rawtype == "base64"`), so the codec works on **bytes**
and stays independent of the transport encoding.

### B. Enriched sensor (phase 1 — low risk, ships first)

Repurpose/augment DP 1 as a **read-only `sensor`** whose:

- **state** = next scheduled feeding as a `timestamp` (device_class
  `timestamp`), computed from the parsed slots + current time, or the count
  of active meals if a timestamp state is judged too surprising.
- **attributes** = the full parsed schedule: a list of
  `{time: "06:58", portions: 1, days: [...]}` plus `meal_count`,
  `portions_per_day`, and the next-feed details.

This gives immediate dashboard value with no new platform. The current
hidden `text` entity can stay (raw value, for power users) or be replaced.

tuya-local sensors already support extra attributes via additional named
DPS / `attributes` — the parsed list would be produced by a small
`sensor.py` code path that recognises the `meal_plan` translation_key, or
(cleaner) by a new DPS `type`/formatter. **Decision needed** (§9): attribute
production via a new DPS `format` variant vs. a targeted `sensor.py` branch.

### C. Editable calendar entity (phase 2 — the "set from HA" goal)

Add a new **`calendar` platform** (`calendar.py`) that presents each meal
slot as a recurring calendar event and supports editing.

- Base class `TuyaLocalCalendar(TuyaLocalEntity, CalendarEntity)`, following
  the pattern of `datetime.py` / `time.py` (`async_setup_entry` →
  `async_tuya_setup_platform(..., "calendar", TuyaLocalCalendar)`).
- **Read:** `async_get_events(start, end)` expands each slot across the
  requested window using its day bitmask; `event` property returns the next
  upcoming feeding. Each event: summary = `"Feed N portion(s)"`, start =
  that day's HH:MM, short duration, `rrule` = weekly on the masked days.
- **Write (round-trip):** implement `CalendarEntityFeature.CREATE_EVENT`,
  `DELETE_EVENT`, `UPDATE_EVENT`:
  - CREATE → add a `MealSlot` (map event time→hour/minute, a portions field
    from the summary or a default, days from rrule/byday), re-encode the
    whole array, write DP 1.
  - DELETE → drop the matching slot, re-encode, write.
  - UPDATE → replace the slot, re-encode, write.
  - Event identity: encode a stable id from `(hour, minute)` (times are
    unique per plan) so HA can map an edited event back to a slot.

Because the device stores the **entire plan in one DP**, every edit is a
read-modify-write of the full array. The codec's `encode()` re-sorts by time
for a stable payload.

### Round-trip / write path notes

- Writes go through the normal `async_set_value` → `get_values_to_set` →
  `encode_value` (base64) path. The calendar entity builds the new byte
  array and hands it to the DP as bytes; `encode_value` base64-encodes it.
- **Portions** aren't naturally part of a calendar event. Options: encode
  portions in the event summary/description (parse on write), or pair the
  calendar with the existing `manual_feed`/a per-event number. Recommend:
  parse portions from summary (e.g. `"Feed 2"`), default 1, and document it.

## 5. Config (YAML) changes

`welltobe_cat_feeder.yaml` — replace the opaque `text` with a sensor (+ keep
raw hidden text optionally), and add the calendar once phase C lands:

```yaml
  - entity: sensor
    translation_key: meal_plan          # new/generic key; timestamp of next feed
    class: timestamp
    dps:
      - id: 1
        type: base64
        optional: true
        name: sensor
        # + attribute-producing mechanism (see §9 decision)

  - entity: calendar                     # phase C
    translation_key: meal_plan
    dps:
      - id: 1
        type: base64
        optional: true
        name: schedule
```

Keep DP 1 `optional: true` (it was optional before; not all variants report it).

## 6. Schema / translations / icons

- `device_config_schema.json`: add `"calendar"` to the `entity` enum.
- `translations/*.json`: add generic `meal_plan` sensor/calendar keys
  (per AGENTS.md, translation-only additions are best as a **separate PR**).
- `icons.json`: add an mdi icon for the new translation_key(s)
  (e.g. `mdi:food` / `mdi:calendar-clock`).
- Keep names generic/unbranded per the naming guidelines.

## 7. Testing plan

- `helpers/meal_plan.py` — unit tests: decode the reference payload → the
  table in §2; `encode(decode(x)) == x` round-trip; malformed lengths;
  empty plan.
- `calendar.py` — tests mirroring `tests/test_datetime.py`: event
  expansion across a week, next-event selection, and CREATE/UPDATE/DELETE
  producing the expected re-encoded DP write (assert the base64 the device
  would receive).
- `tests/test_device_config.py` covers the YAML change automatically; add
  the new entity to the device's test coverage config so the config test
  passes.
- Follow AGENTS.md: `uv run ruff check`, `uv run ruff format`,
  `uv run yamllint`, `uv run pytest`.

## 8. Upstream strategy & phasing

A brand-new **calendar platform** is a precedent-setting change for this
integration. Recommend:

1. **Talk to the maintainer first** (issue/discussion) before the calendar
   PR — confirm they want a calendar platform and agree the generic codec
   shape. The sensor phase is uncontroversial and can go first.
2. **PR 1 — codec + enriched sensor** (`helpers/meal_plan.py`, sensor wiring,
   YAML sensor swap, tests). Self-contained, immediately useful.
3. **PR 2 — calendar platform** (`calendar.py`, schema enum, tests) — read
   support first.
4. **PR 3 — editable calendar** (CREATE/UPDATE/DELETE round-trip).
5. **PR (separate) — translations/icons** as AGENTS.md advises.

Keep every piece **generic** (parameterised codec, translation keys, no
`welltobe`-specific branching) so other feeders adopt it by config alone.

## 9. Open questions — need a second data sample / confirmation

These don't block phase A/B but must be nailed before shipping editing:

1. **Day bitmask bit order.** Every record is `0x7f` (all days) in the
   sample, so bit→weekday order is unproven. Confirm by setting **one meal
   to specific weekdays** in the vendor app and re-reading DP 1. (Common
   Tuya order is bit0 = Sunday, but verify.)
2. **Flag byte (byte 4).** All `0x00` here. Candidates: per-slot
   enable/disable, "already fed today", or reserved. Confirm by
   disabling a single meal and re-reading.
3. **Max slots / ordering.** Does the device cap the number of meals? Must
   the array be time-sorted on write? (Codec sorts defensively.)
4. **Portions in calendar events** — chosen representation (summary-parse
   vs. paired number). See §4C.
5. **Sensor state choice** — next-feed `timestamp` vs. active-meal count.
6. **Attribute mechanism** — new generic DPS formatter vs. targeted
   `sensor.py` branch (prefer the reusable formatter if it stays simple).
7. **DP 3 relationship.** `manual_feed` (DP 3, portions 1-6) confirms the
   portions range; check it isn't also involved in scheduled feeds.

## 10. Risks

- Writing a malformed payload to DP 1 could disrupt real feeding. Mitigate:
  validate ranges on encode, round-trip tests, and a config option / initial
  read-only calendar before enabling writes.
- New platform increases integration surface; keep it minimal and mirror
  existing platform modules closely.
- Bitmask/flag assumptions unverified — gate editing on the §9 confirmations.

---

### Validation snippet

Reproduce the decode of the reference payload:

```python
>>> decode_meal_plan("fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA=")
# 7 slots: 06:58×1, 08:00×4, 13:00×2, 15:30×1, 16:15×2, 17:30×1, 19:30×2
```
