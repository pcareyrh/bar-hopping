# Event-Type × Height Timing Grid

**Date:** 2026-05-05
**Status:** Approved

## Problem

The schedule predictor uses one time-per-dog (TPD) value per jump height (200–600 mm). Agility and Jumping courses run at different speeds, so a single per-height value produces inaccurate predictions for whichever discipline it's miscalibrated for.

## Goal

Let the user set independent TPD values for **Agility** and **Jumping** at each of the five jump heights, forming a 5×2 grid of 10 values.

## Approach

Treat the existing `tpd_200`…`tpd_600` columns as the **Agility** row (no rename, no data loss). Add five new columns for Jumping. Classification reuses the existing `_ring_of()` function which already returns `"Jumping"` or `"Agility"` based on whether the event name contains "jumping".

## Data Model

**New columns on `sessions`** (all `Integer`, default 90):

```
tpd_jumping_200
tpd_jumping_300
tpd_jumping_400
tpd_jumping_500
tpd_jumping_600
```

**Migration:** backfill each new column from the matching existing `tpd_*` value so existing users' Jumping row starts equal to their Agility row.

**`Session.tpd_for` signature change:**

```python
# Before
def tpd_for(self, height_group: int | None) -> int

# After
def tpd_for(self, height_group: int | None, event_name: str | None = None) -> int
```

Implementation: if `event_name` contains "jumping" (case-insensitive), read from `tpd_jumping_*`; otherwise read from `tpd_*`. Falls back to `avg_time_per_dog` (90) if the column is null.

## Schedule Computation

Two call sites updated to pass `event_name`:

1. **`_compute_catalogue_blocks`** — the `tpd_for_height` callback changes from `(height_group) -> int` to `(height_group, event_name) -> int`. The lambda/reference passed from `schedule_view` and `_build_predictions` is updated accordingly. Inside the loop: `tpd_for_height(b["height_group"], b["event_name"])`.

2. **`_build_predictions`** — `session.tpd_for(ce.height_group)` → `session.tpd_for(ce.height_group, ce.event_name)`.

No other call sites exist.

## Settings UI

The settings form changes from one 5-column row to a **2-row grid**:

| | 200mm | 300mm | 400mm | 500mm | 600mm |
|-----------|-------|-------|-------|-------|-------|
| Agility | tpd_200 | tpd_300 | tpd_400 | tpd_500 | tpd_600 |
| Jumping | tpd_jumping_200 | … | … | … | tpd_jumping_600 |

Row labels appear as a left-hand column. Field names, validation (`min=15 max=300`), and POST handler logic follow the same pattern as today. The handler saves all 10 values.

## Schedule Footer

The footer on `schedule.html` currently shows one line of 5 values. It becomes two lines:

```
Agility: 200mm 90s · 300mm 90s · …
Jumping: 200mm 60s · 300mm 60s · …
```

The "Change defaults" link is unchanged.

## Migration

One Alembic migration (or inline `add_column` via the existing startup migration helper):
- Add the 5 new columns with `default=90`
- Backfill: `UPDATE sessions SET tpd_jumping_X = tpd_X WHERE tpd_jumping_X IS NULL` for each height

## Out of Scope

- Per-event-type setup/walk minutes (not requested)
- Gamblers, Snooker, or other discipline types beyond Agility/Jumping
- Per-run overrides (already handled separately via `time_per_dog_override` on `SessionEntry`)
