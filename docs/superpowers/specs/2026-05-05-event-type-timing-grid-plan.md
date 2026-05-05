# Implementation Plan: Event-Type × Height Timing Grid

**Spec:** `2026-05-05-event-type-timing-grid-design.md`
**Branch:** main

---

## Step 1 — Migration (`app/main.py`)

In `_migrate()`, add 5 new columns to `sessions`, each backfilled from the matching existing `tpd_*` column:

```python
for h in (200, 300, 400, 500, 600):
    col = f"tpd_jumping_{h}"
    _add_column_if_missing(
        conn, "sessions", col, "INTEGER",
        backfill_sql=f"UPDATE sessions SET {col} = COALESCE(tpd_{h}, avg_time_per_dog, 90) WHERE {col} IS NULL",
    )
```

Insert after the existing `tpd_*` loop (currently lines 37–44).

---

## Step 2 — Model (`app/models.py`)

**Add 5 columns to `Session`** after the existing `tpd_600` column:

```python
tpd_jumping_200 = Column(Integer, default=90)
tpd_jumping_300 = Column(Integer, default=90)
tpd_jumping_400 = Column(Integer, default=90)
tpd_jumping_500 = Column(Integer, default=90)
tpd_jumping_600 = Column(Integer, default=90)
```

**Update `tpd_for` signature and logic:**

```python
def tpd_for(self, height_group: int | None, event_name: str | None = None) -> int:
    is_jumping = "jumping" in (event_name or "").lower()
    if height_group in HEIGHT_GROUPS:
        col = f"tpd_jumping_{height_group}" if is_jumping else f"tpd_{height_group}"
        value = getattr(self, col, None)
        if value is not None:
            return value
    return self.avg_time_per_dog or 90
```

---

## Step 3 — Schedule router (`app/routers/schedule.py`)

**`_compute_catalogue_blocks`** — update the inner loop to pass `event_name` to the callback:

```python
# line 214 — change:
cursor += timedelta(seconds=b["count"] * tpd_for_height(b["height_group"]))
# to:
cursor += timedelta(seconds=b["count"] * tpd_for_height(b["height_group"], b["event_name"]))
```

No change needed at the call sites (`schedule_view` and `_build_predictions`) because `session.tpd_for` is passed as the callback and its new signature is backwards-compatible (`event_name` defaults to `None`).

**`_build_predictions`** — pass `event_name` at both prediction call sites:

```python
# line 286 — change:
height_tpd = session.tpd_for(ce.height_group)
# to:
height_tpd = session.tpd_for(ce.height_group, ce.event_name)
```

---

## Step 4 — Settings route handler (`app/routers/sessions.py`)

In the POST handler for `/s/{uuid}/settings`, save the 5 new fields alongside the existing ones. Follow the same pattern as today:

```python
for h in (200, 300, 400, 500, 600):
    val = form.get(f"tpd_jumping_{h}", "").strip()
    if val:
        setattr(session, f"tpd_jumping_{h}", int(val))
```

---

## Step 5 — Settings template (`app/templates/settings.html`)

Replace the single 5-column row with a labelled 2-row grid. The height headers move to a `<thead>`-style row; "Agility" and "Jumping" become row labels in a left column.

Structure (5 columns + 1 label column = 6-column grid):

```
         | 200mm | 300mm | 400mm | 500mm | 600mm
Agility  | [tpd_200] ... existing fields ...
Jumping  | [tpd_jumping_200] ... new fields ...
```

- Field names, `min=15 max=300`, and CSS classes unchanged from existing inputs.
- Grid: `grid-cols-6` with the label column being `col-span-1` and each input `col-span-1`.

---

## Step 6 — Schedule footer (`app/templates/schedule.html`)

The footer currently shows one line:

```
Time per dog: 200mm 90s · 300mm 90s · …
```

Change to two lines:

```
Agility: 200mm Xs · 300mm Xs · …
Jumping: 200mm Xs · 300mm Xs · …
```

Template change (around line 70):

```html
<p>Agility: {% for h in [200, 300, 400, 500, 600] %}{{ h }}mm {{ session|attr('tpd_' ~ h) or session.avg_time_per_dog }}s{% if not loop.last %} · {% endif %}{% endfor %}</p>
<p>Jumping: {% for h in [200, 300, 400, 500, 600] %}{{ h }}mm {{ session|attr('tpd_jumping_' ~ h) or session.avg_time_per_dog }}s{% if not loop.last %} · {% endif %}{% endfor %}</p>
```

---

## Verification

1. Start dev server, open Settings — confirm 2-row grid renders with current values pre-filled in both rows.
2. Set Agility 200mm = 90s, Jumping 200mm = 60s, save.
3. Open a trial schedule — confirm the footer shows the two different values.
4. Confirm a Jumping block in the day schedule uses 60s/dog and an Agility block uses 90s/dog.
5. Confirm a trial with no Jumping entries is unaffected.
