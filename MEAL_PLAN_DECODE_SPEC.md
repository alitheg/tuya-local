# Design spec: smart decode for the pet-feeder meal plan

**Status:** Draft / design proposal (working document — not the final PR content)
**Device:** `custom_components/tuya_local/devices/catit_pixi_smart_feeder.yaml`
(Catit Pixi Smart Feeder, model 43752, product `s3rvixmeqx62vud5`) and other
Tuya feeders that use the same `meal_plan` DP encoding — including
`catit_pixi_6meal_feeder.yaml`, whose config already documents this exact
byte layout, and `welltobe_cat_feeder.yaml`.

> **Config-comment correction:** `catit_pixi_smart_feeder.yaml` currently
> annotates DP 1 with *"Not thought to be used with Catit 43752"*. A real
> 43752 (the requester's) returns a populated base64 meal plan on DP 1, so
> that comment is wrong and should be removed/corrected in this work.
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
    category: config
    hidden: true
    dps:
      # Not thought to be used with Catit 43752   <-- INCORRECT (see above)
      - id: 1
        type: base64
        name: value
        optional: true
```

So Home Assistant receives the raw base64 string
(`fwY6AQB/CAAEAH8NAAIAfw8eAQB/EA8CAH8RHgEAfxMeAgA=`) and does nothing with
it. The user sees an opaque blob.

Portions on this device range **1-12** (cf. DP 101 "Meal size" and DP 3
`manual_feed`, both 1-12) — wider than the welltobe 1-6 — so the codec must
not hardcode a portions range.

## 2. Decoded payload format

The base64 decodes to **35 bytes = 7 fixed-width records of 5 bytes** each
(35 / 5 = 7 exactly; 35 is not divisible by 6, so the 5-byte width is
firmly established). Each record is:

```
byte 0: day-of-week bitmask   (MSB padded 0; bit6..bit0 = Mon,Tue,Wed,Thu,Fri,Sat,Sun; 0x7f = every day)
byte 1: hour        (0-23)
byte 2: minute      (0-59)
byte 3: portions / feed number (device-dependent range; 1-12 on the Smart Feeder)
byte 4: enable flag (per-meal enable: 1 = enabled, 0 = disabled — confirmed by owner, see §2)
```

The day-bitmask bit order is the one **documented in
`catit_pixi_6meal_feeder.yaml`** ("1 bit per day Monday → Sunday, padded
with 0 on the MSB. Ex: Monday, Wednesday, Sunday → 0b01010001"):

```
bit:   7    6    5    4    3    2    1    0
day:  pad  Mon  Tue  Wed  Thu  Fri  Sat  Sun
```

**Confirmed against a second sample.** Adding an 8th meal (13:13) grew the
payload from 35 → 40 bytes and the new record was inserted **time-sorted**
between the 13:00 and 15:30 records — so the payload is a variable-length,
chronologically-ordered array and `encode()` must sort by time. That new
meal was set to "not Sunday", producing mask `0x7e` (bit 0 cleared), which
matches **bit 0 = Sunday**. Byte 4 is a **per-meal enable flag**: the owner
confirms the 13:13 record (byte 4 = `1`) is the **only enabled** meal while
the other seven (byte 4 = `0`) are disabled, so **`1` = enabled, `0` =
disabled**. (In the first sample all records were `0`, i.e. the whole
schedule was disabled at that time.)

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

Confidence is now high on every field: record width, hour/minute/portions,
the day-bitmask bit order (from the 6-Meal config's documentation, cross-
checked against the Sunday-off sample), and byte 4 being a per-meal enable
flag with **1 = enabled, 0 = disabled** (owner-confirmed). No residual
unknowns in the field layout.

Reference decoder (validated against the sample above):

```python
import base64

# bit index -> weekday, per catit_pixi_6meal_feeder.yaml (MSB padding at bit 7)
BIT_DAY = {6: "Mon", 5: "Tue", 4: "Wed", 3: "Thu", 2: "Fri", 1: "Sat", 0: "Sun"}

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
                "days": [d for b, d in BIT_DAY.items() if mask & (1 << b)],
                "hour": hour,
                "minute": minute,
                "portions": portions,
                "enabled": flag == 1,  # byte 4: 1 = enabled, 0 = disabled
            }
        )
    return meals

def encode_meal_plan(meals: list[dict]) -> str:
    out = bytearray()
    for m in sorted(meals, key=lambda m: (m["hour"], m["minute"])):  # device keeps sorted
        flag = 1 if m.get("enabled", True) else 0
        out += bytes([m["days_mask"], m["hour"], m["minute"], m["portions"], flag])
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

`catit_pixi_smart_feeder.yaml` — fix the incorrect DP-1 comment, add a
sensor (optionally keep the raw hidden text for power users), and add the
calendar once phase C lands. The same additions apply to
`catit_pixi_6meal_feeder.yaml` and `welltobe_cat_feeder.yaml` since they
share the encoding:

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

Keep DP 1 `optional: true` (it already is; not all variants report it).

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

**Decision: we build the calendar entity (Phase 2), regardless of upstream
appetite.** A calendar is the right HA home for an editable recurring
schedule — its native recurring-event model and CREATE/UPDATE/DELETE
features map directly onto add/edit/remove a meal slot, and every
alternative (per-slot datetime/number/switch entities, or a services-only
editor) is clunkier and less discoverable. Since a `calendar` platform is
new to this integration, we still *offer* it upstream the collaborative way,
but we do not gate the work on acceptance:

- **Courtesy heads-up, not a gate.** Open an issue/discussion proposing the
  calendar platform so the maintainer can weigh in on shape early. Proceed
  with implementation in parallel.
- **Fallback if declined.** The calendar platform lives in this same
  `custom_components/tuya_local` tree, so if upstream doesn't want it, the
  requester runs the branch as their own `custom_components` override (or a
  fork) with no further changes. Nothing about the design depends on being
  merged.

Phasing:

1. **PR 1 — codec + enriched sensor** (`helpers/meal_plan.py`, sensor wiring,
   YAML sensor swap + the DP-1 comment fix, tests). Self-contained,
   immediately useful, uncontroversial — lands first.
2. **PR 2 — calendar platform, read** (`calendar.py`, schema enum, tests) —
   event expansion + next-event, built regardless of the upstream reply.
3. **PR 3 — editable calendar** (CREATE/UPDATE/DELETE round-trip).
4. **PR (separate) — translations/icons** as AGENTS.md advises.

If the maintainer prefers to take the sensor but not the calendar, PR 1
still merges cleanly on its own and PRs 2–3 simply live on the personal
branch/fork. Keep every piece **generic** (parameterised codec, translation
keys, no device-specific branching) so other feeders adopt it by config
alone and the personal-fork path stays a drop-in.

## 9. Open questions — need a second data sample / confirmation

Resolved by the second sample:

- ~~Day bitmask bit order~~ — **CONFIRMED bit0=Sunday**, bits 0..6 =
  Sun,Mon,Tue,Wed,Thu,Fri,Sat (deselecting Sunday gave mask `0x7e`).
- ~~Flag byte meaning + polarity~~ — **CONFIRMED per-meal enable flag,
  `1` = enabled, `0` = disabled** (owner: the 13:13 record, byte 4 = 1, is
  the only enabled meal).
- ~~Array ordering~~ — **CONFIRMED time-sorted**; the inserted 13:13 record
  landed between 13:00 and 15:30. `encode()` sorts by time.

Still open (do not block phase A/B):

1. **Max slots.** Does the device cap the number of meals? (Grew cleanly
   7→8; upper bound unknown — validate ranges/count defensively on encode.)
3. **Portions in calendar events** — chosen representation (summary-parse
   vs. paired number). See §4C.
4. **Sensor state choice** — next-feed `timestamp` (enabled meals only) vs.
   active-meal count.
5. **Attribute mechanism** — new generic DPS formatter vs. targeted
   `sensor.py` branch (prefer the reusable formatter if it stays simple).
6. **Related DPs on the Smart Feeder.** DP 3 `manual_feed` (1-12), DP 101
   "Meal size" (1-12), DP 15 feed report, DP 104 "Last meal details"
   (`R:.. C:.. T:..`). Confirm none of these overlap the scheduled-feed
   path and use DP 101/3 to bound the portions byte range.

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
