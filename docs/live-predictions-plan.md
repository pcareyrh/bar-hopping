# Live Predictions — Updating Run Times Throughout the Day

How to make Bar Hopping's predicted run times **self-correcting on the day** by
tracking **when each (ring, class, height) event starts and finishes**, then
re-anchoring the schedule from those measured event boundaries.

> Investigated against the live page for trial 1307 (2026 Agility Nationals
> Queensland) at `https://www.topdogevents.com.au/trials/1307/live`. All live
> surfaces documented below were reachable **anonymously**; no TopDog
> credentials were required for the ring-status board or the live-results menu.

---

## 1. Why predictions drift (current behaviour)

Today every predicted time is computed **once** and is purely a paper estimate.
The two entry points in `app/engine/predictor.py` are:

```
predict_run:            first_run_start = scheduled_start + setup + walk
                        predicted_start = first_run_start + (position-1) × tpd
predict_run_from_block: predicted_start = block_first_run + (position-1) × tpd
```

and `app/routers/schedule.py::_compute_catalogue_blocks()` lays out the whole
day by assuming:

- every ring starts at `trial.start_time` (or `09:00`),
- a fixed `setup + walk` before each *event* changeover,
- a constant `tpd` (`session.tpd_for(height, event)`, default 90 s) for **every**
  dog in a height block,
- a single optional lunch break per (day, ring).

None of these track reality. On the day the schedule routinely drifts because:

- rings run **ahead or behind** the paper start (judging starts late, briefings
  run long, a ring waits for a shared judge),
- **height-change and course-build gaps** vary a lot between clubs,
- some classes burn through faster or slower than the default `tpd` implies,
- lunch actually starts when a ring *reaches* a break, not at a fixed clock time.

The data to correct this is published live by TopDog and is free to read.

---

## 2. Design principle — event boundaries, not individual runs

**Do not** anchor predictions on individual dog completions from the live board.
The board's `#last_run` / `last_runs[]` feed is useful for spectators but
**unreliable for timing**:

- runs can be entered **out of order** (WD/DQ/backfill lag),
- the "most recent" cat# may not reflect true queue position,
- `runs left` counts can jump when scratches are applied,
- per-run wall-clock timestamps are **not published** anyway.

Instead, track **event-level boundaries**: when a (ring, class, height) segment
**starts** and **finishes**. Within a started event, keep using the catalogue
`run_position` and user-configured `tpd` — that is pre-known running order, not
live per-dog tracking.

```
Measured:  event_started_at, event_finished_at, event_duration
Predict:   event_started_at + (run_position - 1) × tpd     # event already running
           chain prior measured durations + changeover      # event not yet started
```

This matches how handlers actually think about the day ("Novice Agility 400 is
running now, Masters is probably 45 min away") rather than "dog 465 just ran".

---

## 3. The live data source (verified)

### 3.1 `GET /trials/{trial_id}/live` — "Ring Status" board (public, primary source)

Server-renders the **current state of every ring**, then keeps it live over a
WebSocket. Per ring:

```html
<div class="card mb-3 ring-card" id="ring_351" data-status="Running">
  Ring 1
  <span id="class_name">Excellent Gamblers (400)</span>
  <span id="status">Running</span>
  <span id="updated" data-timestamp="2026-06-25T04:04:26Z"></span>
</div>
```

**Fields we use** (event-level only):

| Field | Source | Example | Use |
|---|---|---|---|
| `ring_id` | `id="ring_351"` | `351` | stable ring key |
| ring label | `.live-item-name` | `Ring 1` | → `CatalogueEntry.ring_number` (`"1"`) |
| current class+height | `#class_name` | `Excellent Gamblers (400)` | which event segment is live |
| status | `data-status` / `#status` | `Running` / `Complete` / `Height Change` / `Not Running` | boundary detection |
| updated | `#updated@data-timestamp` | `2026-06-25T04:04:26Z` (UTC) | timestamp for transitions |

**Fields we ignore for prediction** (display / debugging only):

| Field | Why ignored |
|---|---|
| `#last_run` / `last_runs[]` | out-of-order, no wall-clock time |
| `#class_runs_left` | scratch-sensitive, not needed when we have event duration |
| trial `#live_run_count` / `#live_runs_left` | trial-wide progress UI only |

Status vocabulary: **Running, Complete, Height Change, Not Running**.

**Event boundary signals** (detected by comparing consecutive polls or WS
messages for the same `ring_id`):

| Transition | Meaning | Record |
|---|---|---|
| `class_name` changes (any status) | previous segment ended, new one began | `finished_at` on old key; `started_at` on new key |
| `status` → `Complete` (same `class_name`) | segment finished | `finished_at` |
| `status` → `Running` (new `class_name` vs prior poll) | segment started | `started_at` |
| `status` → `Height Change` (same `class_name`) | mid-segment pause (judge change, walk) | pause interval — do **not** count toward event duration |
| `status` → `Not Running` | ring idle (lunch, waiting for judge) | gap between events |

Use the ring card's `updated` timestamp as the boundary clock when a transition
is observed; on the first poll of a new day, if a ring is already `Running`,
set `started_at = updated` (best available; flag `start_confidence=low`).

### 3.2 WebSocket: `wss://www.topdogevents.com.au/cable` (Rails ActionCable)

```js
App.cable.subscriptions.create(
  { channel: "ResultsFeedChannel", trial_id: trialId },
  { received(data) { /* patches ring cards */ } }
)
```

Relevant `data` fields for **event tracking only**:

```
ring_id
class_name             // "Novice Jumping (600)"
status                 // "Running" | "Complete" | "Height Change" | "Not Running"
updated                // ISO-8601 UTC — use as transition timestamp
note                   // optional ring note (lunch, walk open, etc.)
```

Ignore `last_run`, `last_runs`, `class_runs_left` for prediction. WS gives
lower latency on boundary detection; polling alone is sufficient for v1.

### 3.3 `GET /trials/{trial_id}/results/live` → `.../view` (optional, display only)

Per-class live view shows completed runs with times/Q/DQ. Use **only** to show
"your dog has run" and the actual result on the schedule card after the event
finishes — **not** to anchor or pace future predictions.

### 3.4 What is NOT available publicly

- `/trials/{id}/live.json` → `404`; `/trials/{id}/rings.json` → `401`. Ingest
  parses HTML and/or consumes the WebSocket.
- No explicit `started_at` / `finished_at` fields in the markup — **we derive
  them from status/class transitions** and the ring `updated` timestamp.

---

## 4. Architecture fit & constraints

These mirror the constraints in `PLAN.md` §0:

- **Web image can't scrape.** Live fetching lives in the **worker image**,
  lazy-imported inside RQ jobs (`app/worker.py` pattern). Web tier reads DB only.
- **No scheduler exists.** Use a periodic Nomad batch (like
  `barhopping-results-cron.nomad.hcl`) plus a self-rescheduling poll loop while
  a trial is live.
- **Worker is single-process.** One poll per active trial at ~45–60 s is enough;
  boundary detection is not latency-sensitive.
- **SQLite (dev) + Postgres (prod).** New tables via `create_all`; columns via
  `_migrate()` in `app/main.py`.
- **Timezone.** `updated` is UTC; convert to trial-local time on ingest.

---

## 5. The core idea — measured event timeline per ring

Build a **timeline of (ring, class, height) segments** with measured start and
finish times, then project the rest of the day by chaining durations.

### 5.1 Event segment identity

A segment key is:

```
(ring_number, event_name, height_group, day)
```

`event_name` and `height_group` come from parsing `#class_name`
(`"Excellent Gamblers (400)"` → `("Excellent Gamblers", 400)`), joined to
`CatalogueEntry` / `ClassSchedule` via the same normalisation as
`_match_class_schedule()`.

### 5.2 Recording start and finish

On each poll (or WS message), for each ring compare against the previous
snapshot:

```python
def apply_ring_transition(prev, curr, observed_at):
    key = (curr.ring_number, curr.event_name, curr.height_group)

    if prev is None or prev.class_name != curr.class_name:
        # class changed → close previous segment, open new one
        if prev:
            close_segment(prev_key, at=curr.updated or observed_at)
        open_segment(key, at=curr.updated or observed_at, status=curr.status)

    elif prev.status != "Complete" and curr.status == "Complete":
        close_segment(key, at=curr.updated or observed_at)

    elif prev.status in ("Not Running", "Height Change") and curr.status == "Running":
        # resumed after pause — extend segment, don't open a new one
        resume_segment(key, at=curr.updated or observed_at)

    update_segment_status(key, curr.status)
```

`close_segment` sets `finished_at` and `duration_s = finished_at - started_at`,
subtracting any accumulated **pause** intervals (`Height Change`, `Not Running`
while same `class_name`).

Store one row per segment in `EventLiveTiming` (see §6.2). Segments with
`finished_at IS NULL` are **in progress**.

### 5.3 Measured event duration

For completed segments:

```
duration_s = finished_at - started_at - pause_s
```

Maintain a rolling statistic per `(event_name, height_group)` across all rings
that have finished that segment today (and optionally prior trials for the same
club):

```
typical_duration_s = median(recent duration_s)   # prefer same-ring, else global
```

Fallback when no measurement yet: catalogue estimate

```
estimated_duration_s = height_group_total × session.tpd_for(height, event)
```

This replaces per-dog pace sampling. A whole Novice Agility 400 block that took
52 min teaches us that block takes ~52 min, regardless of which individual dogs
were WD or DQ.

### 5.4 Re-anchored prediction

Walk the per-ring ordered block list from `_compute_catalogue_blocks()` (unchanged
catalogue order), but seed each ring's cursor from the **measured timeline**:

| Segment state | Prediction for a dog in this segment |
|---|---|
| **Finished** (`finished_at` set) | Dog has run — optional `/view` lookup for actual result; no future time. |
| **Running** (`started_at` set, no `finished_at`) | `started_at + (run_position - 1) × tpd` |
| **Not started** (no row yet) | `cursor`, where `cursor` chains from the previous segment's measured or estimated `finished_at` + `changeover_s` |
| No live data for ring | Fall back to paper `predict_run` / `predict_run_from_block` |

**Changeover** between events on the same ring: measured gap between consecutive
finished→started pairs on that ring today; default to `session.default_setup_mins
+ session.default_walk_mins` until we have ≥1 sample.

**Ring offset (display):** for the currently-running segment,
`offset = paper_block_first_run − measured_started_at` → "Ring 1 · 18 min behind".

Within a running event, `run_position` still comes from the **catalogue** (or
user `position_override`). We are **not** trying to infer position from live
results — only the event's wall-clock start is live.

### 5.5 Confidence & staleness

- `start_confidence`: `high` (saw Running transition) / `low` (inferred mid-event
  on first poll).
- If a ring's `updated` is older than ~15 min while status is Running, mark
  **stale** and widen the prediction band / fall back toward paper.
- Tag predictions `source ∈ {event_live, scheduled}`; finished runs optionally
  `source=actual` when `/view` confirms a result.

---

## 6. Implementation

### 6.1 Scraper — `app/scraper/live.py` (worker image only)

Plain `httpx` + `BeautifulSoup`. Lazy-imported in worker jobs only.

```python
BASE = "https://www.topdogevents.com.au"

async def fetch_ring_status(trial_external_id: str) -> dict:
    """GET /trials/{id}/live → event-level ring snapshot:
       {
         "trial_external_id": "1307",
         "observed_at": datetime,
         "rings": [
            {ring_id:"351", ring_number:"1",
             class_name:"Excellent Gamblers", height_group:400,
             status:"Running", updated: datetime},
            ...
         ],
       }"""
    # Does NOT parse #last_run or #class_runs_left for prediction.

def parse_class_name(text: str) -> tuple[str, int | None]:
    """'Excellent Gamblers (400)' -> ('Excellent Gamblers', 400)."""

async def fetch_live_class_view(...) -> dict:
    """Optional. GET .../view → completed runs for display after event ends.
       Reuse my_day.parse_my_day_detail. Not used for prediction anchoring."""
```

Recommend **poll first** (~45–60 s); optional WS client later for faster boundary
detection.

### 6.2 Data model — `app/models.py` (+ `_migrate()`)

```python
class EventLiveTiming(Base):
    """One row per (trial, ring, class, height, day) segment."""
    __tablename__ = "event_live_timings"
    id
    trial_id          # FK, index
    day               # int, default 1
    ring_id           # TopDog internal e.g. "351"
    ring_number       # bare "1"
    event_name
    height_group
    status            # latest: Running/Complete/Height Change/Not Running
    started_at        # datetime, nullable until observed
    finished_at       # datetime, nullable while in progress
    pause_s           # accumulated non-Running time within segment
    duration_s        # computed on close; nullable while running
    start_confidence  # high | low
    observed_at       # our last poll that touched this row
    __table_args__ = (
        UniqueConstraint("trial_id", "day", "ring_number", "event_name", "height_group"),
    )

class EventDurationStat(Base):
    """Rolling measured duration per (event_name, height_group) for estimation."""
    __tablename__ = "event_duration_stats"
    id
    trial_id          # FK — scoped to trial day; optional global rollup later
    event_name
    height_group
    sample_count
    median_duration_s
    last_duration_s
    updated_at
```

`Trial` gets `live_status` (`idle|live|done`) and `live_synced_at`.

**Removed vs earlier draft:** no `RingPaceSample`, no `last_run_cat`, no per-dog
pace EWMA. `RingLiveState` is folded into `EventLiveTiming` + a lightweight
per-ring "current segment" pointer if needed for fast reads.

### 6.3 Worker jobs — `app/worker.py`

```
poll_live_trial_job(trial_id)
  1. fetch_ring_status(trial.external_id)
  2. for each ring: apply_ring_transition(prev_snapshot, curr, observed_at)
     → upsert EventLiveTiming; update EventDurationStat on close
  3. set trial.live_synced_at; flip live_status
  4. self-reschedule (~45 s) while live_status == "live"

start_live_tracking_job(trial_id)
sweep_live_trials_job()          # cron: active trials today → ensure poller
```

Hook `start_live_tracking_job` into `sync_session_job`. Politeness: one board
GET per poll; no per-class `/view` fetches unless the UI requests a result
lookup for a finished run.

### 6.4 Prediction engine — `app/engine/predictor.py` + `app/routers/schedule.py`

```python
def predict_run_from_event(
    *,
    event_started_at: datetime,
    run_position: int,
    avg_time_per_dog: int,
    position_override: int | None = None,
    time_per_dog_override: int | None = None,
) -> dict:
    """Same shape as predict_run_from_block; anchor is measured event start."""
    ...
```

In `_build_predictions()`:

1. Load `EventLiveTiming` for the trial (+ `EventDurationStat` for estimates).
2. Build per-ring timeline; for each user run pick §5.4 branch.
3. `_compute_catalogue_blocks()` gains optional `event_timings` arg: seed each
   ring's `cursor` from measured finishes + changeover instead of paper
   `base_start`.
4. Manual overrides (`position_override`, `time_per_dog_override`) still win.
5. `flag_conflicts`, lunch handling, multi-day fan-out unchanged.

### 6.5 Routes & UI

- Schedule page: per-run badge `event_live` vs `scheduled`; ring offset chip;
  "event started HH:MM" for running segments.
- HTMX `hx-trigger="every 30s"` refresh while trial is live.
- After a segment is **Complete**, optionally fetch `/view` once to show the
  user's actual time/result (display only).
- Remove "N dogs to go" live counter — replace with "event ~X min remaining"
  (`typical_duration_s − (now − started_at)`), which is event-level.

---

## 7. Matching segments to catalogue rows

Join by `(ring_number, event_name, height_group, day)` — no cat# required for
prediction:

- `#class_name` → `parse_class_name()` → match `CatalogueEntry.event_name` via
  `_match_class_schedule()` containment rules.
- Ring via `_bare_ring()`.
- User runs already linked through `SessionEntry` → `CatalogueEntry`.

Cat# is only needed for the optional post-event result display from `/view`.

---

## 8. Edge cases & risks

- **Mid-event first poll.** Ring already Running when tracking starts →
  `started_at = updated`, `start_confidence=low`; prediction still usable but
  flagged. Improves once the *next* event boundary is observed.
- **Height Change / Not Running pauses.** Accumulate into `pause_s`; don't close
  the segment unless `class_name` changes or status → Complete.
- **class_name encodes height** `(400)` — one board card = one height segment.
  Multi-height classes appear as separate sequential segments on the ring; each
  gets its own `EventLiveTiming` row and measured duration.
- **Shared judge / ring idle.** `Not Running` between events is changeover, not
  pause inside a segment (different `class_name`). Measured inter-event gap
  updates `changeover_s` for that ring.
- **Sub-trial / day ids.** Board is keyed by event id (`1307`); `day` comes from
  catalogue/`ClassSchedule.day` when fanning out multi-day trials.
- **Out-of-order runs.** Explicitly out of scope — we never read `#last_run` for
  timing, so mis-ordered result entry cannot skew predictions.
- **HTML drift.** Golden fixture (`tests/fixtures/live_1307.html`, capture +
  anonymise) + parser test on `ring-card` / `#class_name` / `#status` / `#updated`.
- **No live data.** Degrades to current static predictions.

---

## 9. Rollout

1. **Scraper commit.** `app/scraper/live.py` (event-level fields only) + fixture +
   parser test.
2. **Model + migration.** `EventLiveTiming`, `EventDurationStat`, `Trial` live
   columns; `_migrate()`.
3. **Worker commit.** `poll_live_trial_job` with transition logic; sweep/start
   hooks; Nomad cron trigger.
4. **Engine commit.** `predict_run_from_event` + timeline branch in
   `_build_predictions` / `_compute_catalogue_blocks`; `LIVE_PREDICTIONS_ENABLED`
   flag.
5. **UI commit.** Event-level badges, ring offset, optional post-event results.
6. **Verify on a live trial day** — compare measured `started_at` / `finished_at`
   to eScribe chat timestamps and paper schedule for a few known events.

### File-level change list

```
new   app/scraper/live.py                  # ring board parser (event fields only)
edit  app/models.py                        # +EventLiveTiming, +EventDurationStat, +Trial cols
edit  app/main.py                          # _migrate()
edit  app/worker.py                        # poll/sweep/start + transition logic
edit  app/engine/predictor.py             # +predict_run_from_event
edit  app/routers/schedule.py             # event-timeline branch in predictions/blocks
edit  app/templates/schedule.html         # event-live badges + HTMX refresh
edit  app/templates/partials/run_card.html
new   barhopping-live-cron.nomad.hcl
new   tests/fixtures/live_1307.html        # capture + anonymise
new   tests/test_live_parser.py
new   tests/test_live_predictor.py         # event boundary + chaining maths
```

Optional later: `app/scraper/live_socket.py` for push boundaries; `/view` fetch
for result display only.

No new runtime deps for v1. Web image unchanged.
