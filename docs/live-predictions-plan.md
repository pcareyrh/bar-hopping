# Live Predictions — Updating Run Times Throughout the Day

How to make Bar Hopping's predicted run times **self-correcting on the day** by
ingesting TopDog's live ring-status feed and re-anchoring each prediction to the
actual progress of the trial.

> Investigated against the live page for trial 1307 (2026 Agility Nationals
> Queensland) at `https://www.topdogevents.com.au/trials/1307/live`. All live
> surfaces documented below were reachable **anonymously** (the page renders a
> "Log In / Sign Up" sidebar); no TopDog credentials were required to read the
> ring-status board, the live-results menu, or a per-class live view. The only
> things still behind Devise auth are the JSON/admin variants
> (`/trials/{id}/rings.json` → `401`).

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
- **scratches / absentees / NFC** change the real dog count per height,
- **height-change and course-build gaps** vary a lot between clubs,
- a fast height (lots of DQs/short courses) burns the queue faster than 90 s/dog;
  a Masters Gamblers height with 60 s courses + walk-throughs runs much slower,
- lunch actually starts when a ring *reaches* a break, not at a fixed clock time.

The data to correct all of this is published live by TopDog and is free to read.

---

## 2. The live data source (verified)

### 2.1 `GET /trials/{trial_id}/live` — "Ring Status" board (public, primary source)

Server-renders the **current state of every ring**, then keeps it live over a
WebSocket. The initial HTML alone already contains everything we need for a
poll-based implementation. Per ring:

```html
<div class="card mb-3 ring-card" id="ring_351" data-status="Running">
  ...
  Ring 1
  <span id="class_name">Excellent Gamblers (400)</span>
  <span id="status">Running</span>
  <div id="last_run">
    <span class="live-run-cat">4031</span> - Morehill Scarlett In Red: 65.04sec: 31.0pts
    <span class="live-run-cat">4002</span> - Phoenix - WD
    <span class="live-run-cat">496</span>  - Flatout Sweet As (AI) - WD
  </div>
  <span id="class_runs_left"> 2 runs left, 2 in class</span>
  <span id="updated" data-timestamp="2026-06-25T04:04:26Z"></span>
</div>
```

Extractable fields **per ring**:

| Field | Source | Example | Use |
|---|---|---|---|
| `ring_id` | `id="ring_351"` | `351` | join key to `/view` URLs + WS deltas |
| ring label | `.live-item-name` text | `Ring 1` | map to `CatalogueEntry.ring_number` (`"1"`) |
| current class+height | `#class_name` | `Excellent Gamblers (400)` | which (event, height) block is live |
| status | `data-status` / `#status` | `Running` / `Complete` / `Height Change` / `Not Running` | gate the maths per state |
| last run cat# | `.live-run-cat` (first) | `4031` | anchor actual position via catalogue order |
| last run result | `#last_run` text | `65.04sec: 31.0pts`, `DQ`, `WD`, `CLEAR!!`, `F: 4` | pace timing + show real result |
| runs left / in class | `#class_runs_left` | ` 2 runs left, 2 in class` | dogs remaining in the live height segment |
| updated | `#updated@data-timestamp` | `2026-06-25T04:04:26Z` (UTC) | staleness / pace sampling clock |

Trial-wide (top of page):

| Field | Source | Example |
|---|---|---|
| total runs | `#live_run_count` | `4166` |
| runs left | `#live_runs_left` | `2196` |
| progress | `#progress-bar` width | `47.3%` |

Status vocabulary observed: **Running, Complete, Height Change, Not Running**
(the JS lowercases+hyphenates these into `status-running` etc.).

### 2.2 WebSocket: `wss://www.topdogevents.com.au/cable` (Rails ActionCable)

The page opts in via `<meta name="action-cable-url">` and a hidden
`<input id="trial_id" value="1307">`, then subscribes with the bundled
`results_feed_channel` JS:

```js
App.cable.subscriptions.create(
  { channel: "ResultsFeedChannel", trial_id: trialId },
  { received(data) { /* patches the DOM */ } }
)
```

Each broadcast `data` object (verified from the channel handler) may carry:

```
ring_id | class_id      // which card to update (ring_id is what /live uses)
class_name             // "Novice Jumping (600)"
status                 // "Running" | "Complete" | "Height Change" | "Not Running"
last_run | last_runs[] // recent run strings ("cat - name: 32.89sec - CLEAR!!")
class_runs_left        // " 4 runs left, 6 in class"
updated                // ISO-8601 UTC
note                   // free-text ring note
run_count | runs_left  // trial-wide counters
feed | feed_timestamp  // live feed line
chat_message           // eScribe / handler chat
```

This is a push version of exactly the fields in §2.1. Connecting needs the
standard ActionCable handshake (`{"command":"subscribe","identifier":"{\"channel\":\"ResultsFeedChannel\",\"trial_id\":\"1307\"}"}`)
over the `wss` URL — no cookie required for this public channel.

### 2.3 `GET /trials/{trial_id}/results/live` and `.../view` (public, secondary source)

The live-results menu lists per-class links:

```
/trials/{trial_id}/results/live/trial/{sub_trial_id}/ring/{ring_id}/class/{class_id}/view
```

Each `/view` page is **the same `my-day-entry-row` HTML the app already parses**
(`app/scraper/my_day.py::parse_my_day_detail`) plus a progress header:

```html
<div id="live-results-list" data-trial-id="1307" data-discipline-class-id="1">
  <strong>93</strong> of <strong>95</strong> runs complete · <strong>2</strong> runs remaining
  <div class="my-day-entry-row">
    <span class="badge badge-dark">401</span> <strong>Perrioak Litl Boy Gray</strong> · Claire Bird
    <span>36.15s</span> <span>5 faults</span>
  </div>
  <div class="my-day-entry-row">
    <span class="badge badge-dark">437</span> <strong>...</strong> <span class="badge badge-danger">DQ</span>
  </div>
  ...
</div>
```

This gives, for a specific class: exact **runs-complete / total**, plus each
completed run's cat#, time, and Q/DQ/faults. It is the authoritative source for
"has *my* dog run yet, and what did it get", and a precise per-class pace sample.
`ring_id` here matches the `ring_NNN` DOM id on the board; `sub_trial_id`
(e.g. `3063`) identifies the day/session.

### 2.4 What is NOT available publicly

- `/trials/{id}/live.json` → `404`; `/trials/{id}/rings.json` → `401`. There is
  **no public JSON**; ingest must parse the HTML board and/or consume the WS.
- No per-run wall-clock timestamps in the markup — only a per-ring `updated`
  time. **Pace must be measured by sampling** counts over time (see §4.2), not
  read directly.

---

## 3. Architecture fit & constraints

These mirror the constraints already documented in `PLAN.md` §0:

- **Web image can't scrape.** `requirements.web.txt` has no `httpx`/`bs4`. All
  live fetching/parsing lives in the **worker image**, lazy-imported inside RQ
  job functions (the existing `app/worker.py` pattern). The web tier only reads
  the new DB tables.
- **No scheduler exists.** The live poller needs a recurring trigger. Reuse the
  pattern added for results in `barhopping-results-cron.nomad.hcl` (a periodic
  Nomad batch task that just `enqueue`s), but at a **short cadence while a trial
  is live** (see §5.3). For native/dev there's `docker-compose`; a tiny
  rq-driven self-reschedule loop is the simplest portable option (§5.2).
- **Worker is single-process.** One poll job per active trial, serialised. With a
  handful of live trials and a ~30–60 s cadence this is trivial load.
- **SQLite (dev) + Postgres (prod) parity.** New tables via
  `Base.metadata.create_all`; new columns via the existing
  `_migrate()`/`_add_column_if_missing` helper in `app/main.py`.
- **Timezone.** Live timestamps are UTC (`...Z`); the predictor already works in
  naive local time with `AEST_OFFSET`. Convert `updated` → AEST on ingest and
  keep all prediction maths in trial-local time as today.

---

## 4. The core idea — re-anchor each ring's clock to reality

Replace the "paper" assumptions per ring/height with **measured** ones, computed
from the live board, and recompute downstream predictions from the live "now"
line instead of from `trial.start_time`.

For each ring we continuously learn three things:

1. **Where the ring actually is** — the current (event, height) segment, and the
   actual position reached within it.
2. **How fast it's actually going** — measured seconds-per-dog (rolling), per
   (class, height), replacing the static `tpd`.
3. **How far ahead/behind paper it is** — a single signed offset for display
   ("Ring 1 running 18 min behind").

### 4.1 Anchoring position (who is on the line *now*)

Two independent signals, used with a preference order:

**(a) cat#-anchored (preferred).** The board's first `.live-run-cat` is the most
recently completed dog. Map it to a `CatalogueEntry` (`trial_id`, `cat_number`)
→ that dog's `run_position` within its (event, height). Then for a user run in
the same segment:

```
dogs_ahead   = user.run_position - last_completed.run_position
eta          = now + dogs_ahead × measured_pace
```

**(b) count-anchored (fallback / cross-check).** Parse `#class_runs_left`
(` N runs left, M in class`) or the `/view` "X of Y complete":

```
position_reached = M - N            # dogs already run in this segment
dogs_ahead       = user.position_in_segment - position_reached
```

Use (a) when the cat# resolves to a catalogue row; fall back to (b) when it
doesn't (sentinel/`~` catalogues, or board lag). Both rely on data already in
`CatalogueEntry` (`run_position`, `height_group`, `ring_number`, `cat_number`).

### 4.2 Measuring pace (seconds per dog) without per-run timestamps

Sample `(ring_id, class_name, runs_left, observed_at)` on every poll/WS update
and store it. Pace over a window = work done ÷ wall-clock elapsed:

```
Δdogs = runs_left(t0) - runs_left(t1)           # dogs completed between samples
Δt    = observed_at(t1) - observed_at(t0)
pace  = Δt / Δdogs                              # sec/dog, when Δdogs > 0
```

- Maintain an **EWMA** per (class, height) so a couple of slow/fast dogs don't
  whipsaw the estimate; seed it with `session.tpd_for(...)` until ≥ ~3 dogs of
  evidence exist (confidence ramp).
- Ignore samples spanning a **status ≠ Running** interval (Height Change / paused
  / lunch) so changeover gaps don't pollute the per-dog rate — capture those as a
  separate **changeover overhead** estimate instead.
- Clamp to a sane band (e.g. 20–240 s/dog) to reject parsing glitches.

### 4.3 Re-anchored prediction

For a user's run, choose the formula by the live state of its ring+segment:

| Live state of the run's (ring, class, height) | Prediction |
|---|---|
| **Complete** and dog matched in `/view` | Show **actual** time/result; mark done. |
| **Running** this exact segment | `now + dogs_ahead × pace` (§4.1, §4.2). |
| **Running** an earlier segment in the same ring | Sum remaining dogs in intervening segments × their pace + changeover overheads, anchored at `now`. |
| Ring **behind** but segment not started | Shift the paper `block_first_run` by the ring's current **offset** (measured ring lateness), then apply `(position-1) × pace`. |
| Ring **Not Running** / no live data yet | Fall back to today's static `predict_run` / `predict_run_from_block` (current behaviour). |

The "remaining dogs in intervening segments" walk reuses the per-ring ordered
block list already produced by `_compute_catalogue_blocks()` — we only swap the
**start anchor** (now-line) and the **per-dog/changeover costs** (measured) into
the same structure, so the existing schedule layout, lunch handling, and
conflict detection (`flag_conflicts`) keep working unchanged.

**Ring offset for display:** `offset = predicted_block_first_run(paper) −
projected_block_first_run(live)` for the currently-running segment, surfaced as
"running N min ahead/behind".

### 4.4 Confidence & staleness

- Each live ring carries `updated`; if `now − updated` exceeds a threshold
  (e.g. 10–15 min) mark the ring **stale** and fall back toward the paper
  estimate, flagged in the UI ("live data paused").
- Tag each prediction with a `source ∈ {actual, live, scheduled}` and a coarse
  confidence so the UI can style it (solid vs. dimmed/"~").

---

## 5. Implementation

### 5.1 Scraper — `app/scraper/live.py` (worker image only)

Plain `httpx.AsyncClient` + `BeautifulSoup`, no auth, mirroring
`app/scraper/my_day.py`. Lazy-imported inside worker jobs only.

```python
BASE = "https://www.topdogevents.com.au"

async def fetch_ring_status(trial_external_id: str) -> dict:
    """GET /trials/{id}/live → parse the board. Returns:
       {
         "trial_external_id": "1307",
         "observed_at": datetime,            # parse-time (UTC→AEST)
         "run_count": 4166, "runs_left": 2196,
         "rings": [
            {ring_id:"351", ring_number:"1", class_name:"Excellent Gamblers",
             height_group:400, status:"Running",
             runs_left:2, runs_in_class:2,
             last_run_cat:"4031", last_run_raw:"...: 65.04sec: 31.0pts",
             updated: datetime},
            ...
         ],
       }"""

def parse_class_name(text: str) -> tuple[str, int | None]:
    """'Excellent Gamblers (400)' -> ('Excellent Gamblers', 400)."""

async def fetch_live_class_view(trial_external_id, sub_trial_id, ring_id, class_id) -> dict:
    """GET .../results/live/.../view → {runs_complete, runs_total, runs:[...]}.
       Reuse my_day.parse_my_day_detail for the entry rows."""

async def list_live_class_views(trial_external_id: str) -> list[dict]:
    """GET /trials/{id}/results/live → [{sub_trial_id, ring_id, class_id, href}]."""
```

Parsing notes (from §2): ring label via regex `Ring\s*(\d+)`; height from the
`(NNN)` suffix on `#class_name`; `#class_runs_left` via
`r"(\d+)\s+runs left,\s+(\d+)\s+in class"`; result class from `#last_run`
(`CLEAR!!`, `DQ`, `WD`, `F: N`, `: Xsec`, `: Ypts`). Normalise ring labels to the
bare `"1"` form already used by `_bare_ring()`.

Optional WS client (`app/scraper/live_socket.py`, `websockets` dep) for push;
not required for v1 — polling the board is simpler and matches the existing
httpx/RQ shape. Recommend **poll first, add WS later** if latency matters.

### 5.2 Data model — `app/models.py` (+ `_migrate()`)

New tables (additive; `create_all` + indexes via `_migrate`):

```python
class RingLiveState(Base):                 # latest snapshot per ring
    __tablename__ = "ring_live_states"
    id; trial_id (FK, index)
    ring_id            # TopDog internal "351"
    ring_number        # bare "1"
    class_name; height_group
    status             # Running/Complete/Height Change/Not Running
    runs_left; runs_in_class
    last_run_cat
    measured_pace_s    # current EWMA sec/dog for this (class,height)
    offset_seconds     # +behind / -ahead vs paper
    updated_at         # TopDog 'updated' (AEST)
    observed_at        # our last successful poll
    __table_args__ = (UniqueConstraint("trial_id", "ring_id"),)

class RingPaceSample(Base):                 # rolling pace evidence
    __tablename__ = "ring_pace_samples"
    id; trial_id (FK, index)
    ring_id; class_name; height_group
    runs_left; observed_at
    # pace derived from consecutive rows; prune > 1 day old
```

`Trial` gets `live_status` (`idle|live|done`) and `live_synced_at` columns so the
poller knows which trials to track and the UI can show a "LIVE" badge.

No change to `SessionEntry` / `CatalogueEntry` — they already hold the cat#,
run_position, height_group, and ring_number needed to join to live state.

### 5.3 Worker jobs — `app/worker.py`

```
poll_live_trial_job(trial_id)
  1. fetch_ring_status(trial.external_id)
  2. upsert RingLiveState per ring; append RingPaceSample
  3. recompute measured_pace_s (EWMA, status==Running only) + offset_seconds
  4. set trial.live_synced_at; flip live_status idle→live→done from progress/age
  5. if a user's run just went Complete, optionally fetch_live_class_view to
     capture the actual time/result
  6. self-reschedule (enqueue_in ~45 s) while live_status == "live"

start_live_tracking_job(trial_id)   # kick off polling for a trial
sweep_live_trials_job()             # cron: find active trials, ensure a poller
```

**Which trials to poll:** any `Trial` with `SessionEntry` rows whose date window
is "today" (`trial_dates.trial_model_active_on`). `sweep_live_trials_job` runs
from a periodic Nomad batch (every ~5 min, daytime AEST) and ensures one poller
loop per active trial; each loop self-reschedules at the fast cadence and exits
when the trial finishes or all rings are Complete. Hook
`start_live_tracking_job` into the end of `sync_session_job` so opening the app
on the day begins tracking immediately.

Politeness: one board GET + at most a few `/view` GETs per poll; ~45 s cadence;
`asyncio.Semaphore(4)`; back off on non-200.

### 5.4 Prediction engine — `app/engine/predictor.py` + `app/routers/schedule.py`

Add a live-aware layer without rewriting the paper layer:

```python
def predict_run_live(*, now, dogs_ahead, pace_s, changeover_s=0) -> dict:
    predicted_start = now + timedelta(seconds=dogs_ahead*pace_s + changeover_s)
    ...
```

In `_build_predictions()`:

1. Load `RingLiveState` for the trial (one query) into a `{(ring, class, height)}`
   map; skip the whole live path if none / trial not `live`.
2. For each user run, resolve its (ring, class, height) and pick the §4.3 branch.
   Use measured `pace_s` (fallback `session.tpd_for`) and the ring offset.
3. Keep `flag_conflicts`, lunch handling, multi-day fan-out, and overrides exactly
   as-is — they operate on the resulting `predicted_start` list.
4. Per-run **manual overrides still win** (`position_override`,
   `time_per_dog_override`) — live just changes the *default* anchor/pace.

`_compute_catalogue_blocks()` gains an optional `live_state` arg: when present,
seed each ring's `cursor` from the live now-line and use measured pace /
changeover per segment instead of constants; otherwise behave exactly as today.

### 5.5 Routes & UI

- `GET /s/{uuid}/trials/{id}/schedule` already renders predictions — add live
  badges: per-run `source` styling, a ring-offset chip ("Ring 1 · 18 min behind"),
  a trial progress bar (`run_count`/`runs_left`), and a "live as of HH:MM" stamp.
- HTMX `hx-trigger="every 30s"` polling on the schedule (and/or a small
  `GET /s/{uuid}/trials/{id}/schedule/live` partial) so the page self-refreshes
  while the trial runs — the predictions are recomputed from the latest
  `RingLiveState` on each request (cheap, read-only).
- "Up next" emphasis: highlight the user's nearest upcoming run and show
  `dogs_ahead` ("4 dogs to go").
- Mark completed runs with their **actual** time/result when matched in `/view`.

All web-tier code reads only the new tables — **no scraper imports in the web
image**.

---

## 6. Matching live rows to a user's runs

The join key is **cat#** within a trial (unique per dog per trial), already
stored on `CatalogueEntry.cat_number` / `SessionEntry.cat_number`:

- Board `last_run_cat` / `/view` row cat# → `CatalogueEntry(trial_id, cat_number)`
  → `run_position`, `height_group`, `ring_number`.
- The live `#class_name` ("Excellent Gamblers") maps to `event_name` via the same
  case-insensitive containment used by `_match_class_schedule()`; height comes
  from the `(NNN)` suffix. Ring maps via `_bare_ring()`.
- NFC dogs (`...NFC`) already counted in `height_group_total`; the board counts
  them in `in class` too, so count-anchoring stays consistent.

No registration numbers or names needed — cat# is sufficient and robust.

---

## 7. Edge cases & risks

- **Board lag vs. WS.** Polling can be up to one cadence stale; `updated`
  timestamps let us detect and discount stale rings (§4.4). WS removes the lag if
  we add it later.
- **Scratches / late entries** change `in class` mid-day; count-anchoring uses the
  live `in class`, so it self-corrects. cat#-anchoring is unaffected.
- **Height-change / shared-judge gaps**: handled as separate changeover overhead,
  not folded into per-dog pace (§4.2).
- **Sub-trial / day ids** (`3063`) differ from the event id (`1307`); the board is
  keyed by event id, `/view` needs the sub_trial id from the live-results menu.
- **Multiple rings, one judge** (rings pause waiting): status flips to
  Not Running/Height Change → we stop the pace clock and hold the offset.
- **HTML drift.** Brittle parsing; mitigate with a golden fixture
  (`tests/fixtures/live_1307.html` — capture from the live board at
  implementation time and **anonymise dog/handler names** to match the repo's
  existing `_ANONYMISED` convention) + a parser test that fails loudly if
  `ring-card` / `#class_runs_left` / `#class_name` shapes change.
- **Rate limiting.** Conservative cadence + low concurrency; back off on errors.
- **No live data (no creds / pre-publish).** Everything degrades gracefully to
  the current static predictions — live is strictly additive.
- **Timezone.** `updated` is UTC; convert on ingest; keep prediction maths in
  trial-local time (QLD = UTC+10, no DST; NSW has DST — store trial state/tz or
  keep using the existing AEST handling and the trial's own date).

---

## 8. Rollout

1. **Scraper commit.** `app/scraper/live.py` + golden fixture + parser test
   (offline, no network) against an anonymised trial-1307 board and a `/view`.
2. **Model + migration commit.** `RingLiveState`, `RingPaceSample`, `Trial`
   live columns; extend `_migrate()`.
3. **Worker commit.** `poll_live_trial_job` (+ sweep/start), hook into
   `sync_session_job`; add a periodic Nomad batch trigger (daytime AEST) modelled
   on `barhopping-results-cron.nomad.hcl`.
4. **Engine commit.** `predict_run_live` + live branch in `_build_predictions` /
   `_compute_catalogue_blocks`, behind a `LIVE_PREDICTIONS_ENABLED` flag.
5. **UI commit.** Live badges, ring-offset chips, progress bar, HTMX auto-refresh.
6. **Verify on a live trial day** (spot-check predictions vs. the TopDog board for
   a known dog) and tune the EWMA window, confidence ramp, and stale threshold.

### File-level change list

```
new   app/scraper/live.py                  # board + /view parsers (worker image only)
new   app/scraper/live_socket.py           # optional ActionCable client (later)
edit  app/models.py                        # +RingLiveState, +RingPaceSample, +Trial cols
edit  app/main.py                          # _migrate() for new tables/columns
edit  app/worker.py                        # poll/sweep/start jobs; hook sync_session_job
edit  app/engine/predictor.py             # +predict_run_live
edit  app/routers/schedule.py             # live branch in _build_predictions/_compute_catalogue_blocks
edit  app/templates/schedule.html         # live badges + HTMX auto-refresh
edit  app/templates/partials/run_card.html# source/confidence styling, dogs-ahead
new   barhopping-live-cron.nomad.hcl       # periodic sweep trigger (daytime AEST)
new   tests/fixtures/live_1307.html        # golden board fixture (capture + anonymise)
new   tests/fixtures/live_1307_class.html  # golden /view fixture (capture + anonymise)
new   tests/test_live_parser.py            # offline parser tests
new   tests/test_live_predictor.py         # re-anchoring maths tests
```

No new runtime deps for v1 (worker already has `httpx`/`bs4`); the optional WS
client would add `websockets`. The web image needs nothing new.
