# Past Trial Results — Implementation Plan

Add a "Past Trial Results" section that backfills 3 years of NSW Agility results into the local database, then lets a logged-in user (session) see their dogs' historical runs.

> **Decisions (confirmed):**
> 1. Dog identity → `Dog` table populated from existing `SessionEntry.dog_name`s. **No** registration-number scraping.
> 2. Storage scope → store **every** NSW agility run.
> 3. Refresh cadence → one-shot backfill + scheduled refresh for **new results only** (never re-scrape a trial already at `results_status="ok"`).
> 4. Window → rolling last 3 years from today; no prune in v1.
> 5. UI scope → list view **and** stats (where derivable from the public data).
> 6. Scraping → public endpoints only, `httpx` (no Playwright, no auth).

---

## 0. Architecture context (verified against the repo, not memory)

The deployment is **not** monolithic SQLite — that's only the dev fallback in `app/database.py`. The actual Nomad topology (`barhopping.nomad.hcl` / `barhopping-public.nomad.hcl`):

```
job bar-hopping
├── group db       — postgres:16-alpine, CSI/NFS volume "barhoppingdata", port 5432
├── group redis    — redis:7-alpine, port 6379
└── group app
    ├── task web    — Dockerfile.web (slim python:3.12, NO scraping deps), uvicorn :8000
    └── task worker — Dockerfile (ubuntu + Playwright/Chromium), rq worker default
```

Implications for this plan:

- **Database is Postgres** in prod (`postgresql+psycopg2://…`). SQLite still works locally because `app/database.py` is `DATABASE_URL`-driven and toggles `check_same_thread`. All new SQL must work on both.
- **Web image cannot scrape.** `requirements.web.txt` excludes `httpx`, `playwright`, `beautifulsoup4`, `openpyxl`, `pdfplumber`. Anything in `app/scraper/*` must stay **lazy-imported inside RQ job functions** — the existing pattern in `app/worker.py` (`async def _run(): from app.scraper.X import …`). If a web-tier module ever does a top-level `from app.scraper...`, the slim image dies at startup.
- **No scheduler exists today.** The Nomad job is `type = "service"` only — no `periodic` stanza, no `rq-scheduler`, no APScheduler. The weekly refresh has to be added as a new Nomad batch+periodic job (see §7.5), not slipped into the existing one.
- **Worker is single-process, single-allocation** (`count = 1`). RQ concurrency is just one worker dequeuing serially. Backfill enqueues hundreds of jobs but only one runs at a time — that's already a natural rate limit on TopDog. The async `Semaphore(4)` inside a single job handles per-trial sub-fetches.
- **Postgres connection pool**: SQLAlchemy default of 5 is plenty (one process per task, sequential RQ dequeue). No pool tuning needed.

---

## 1. Scope

**In scope**
- Backfill all trials on TopDog matching `discipline=Agility` (id `1`) and `state=NSW` whose `start_date` ≥ `today − 3 years` and that have published results.
- Persist trial → sub-trial → class → height-group → run rows.
- Match runs to user dogs via normalised `(dog_name, handler_name)`.
- Add `/results` UI section in the app showing a session's dogs' past runs.
- Weekly job to pick up new trials and back-fill results for trials that didn't have them yet.

**Explicitly out of scope (v1)**
- Other states / disciplines (config-driven so easy to extend later).
- Live results, ActionCable, websocket streaming.
- Authenticated scraping of `/dogs.json` for registration numbers.
- Re-scraping trials already marked `results_status="ok"` (corrections published after the fact won't be picked up).
- Pruning of trials older than the rolling window.
- Cross-dog leaderboards / club-level rollups.

---

## 2. Data model changes

New tables, additive only — existing schema untouched. The current pattern in `app/main.py:_migrate_per_height_tpd()` (`inspect(engine).get_columns(table)` → conditional `ALTER TABLE … ADD COLUMN`) works on both SQLite and Postgres, so generalise it into `_migrate()` and reuse.

**Postgres-vs-SQLite parity notes:**
- `Base.metadata.create_all(bind=engine)` handles all the *new* tables (`dogs`, `trial_results`) on both backends. Indexes declared via `index=True` / `Index(...)` are created with the table.
- New **columns on existing tables** (`Trial.discipline`, `Trial.state`, `Trial.results_synced_at`, `Trial.results_status`, `Session.last_results_view_at`) are added via `_migrate()` — works on both.
- New **indexes on existing tables** are not created by `create_all`. Add explicit `CREATE INDEX IF NOT EXISTS` statements in `_migrate()` for any index we add to a column on `trials` or `sessions`. (The Postgres `IF NOT EXISTS` form is supported on SQLite too — both treat it as idempotent.)
- Composite unique constraint with a nullable column: both Postgres and SQLite treat `NULL` values as **distinct** in unique indexes, so `(name_normalised, handler_normalised=NULL)` rows could pile up. We mitigate by always normalising handler to a non-empty string or the literal `'-'` placeholder before insert, so the unique key is always fully populated.
- `Boolean`, `Float`, `Integer`, `String`, `Date`, `DateTime` map cleanly to both. `JSON` is not used, so no jsonb gotchas.
- `autoincrement=True` PK → `SERIAL`/`IDENTITY` on Postgres, `INTEGER PRIMARY KEY` on SQLite. SQLAlchemy handles the dialect difference.
- Bulk insert: use `Session.execute(insert(TrialResult), [...])` with a list-of-dicts — efficient on both. Avoid per-row `db.add()` for the ~70k initial backfill.

### 2.1 `dogs`
```python
class Dog(Base):
    __tablename__ = "dogs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    name_normalised = Column(String, nullable=False, index=True)  # lower, ws-collapsed, "(AI)" stripped
    handler_name = Column(String, nullable=True)
    handler_normalised = Column(String, nullable=True, index=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("name_normalised", "handler_normalised"),)
```

Backfill on first migration: `INSERT INTO dogs(name, name_normalised, handler_name, handler_normalised) SELECT DISTINCT … FROM session_entries`.

### 2.2 `trial_results`
One row per `(trial, sub_trial, class, height, dog)` run. Trial reuses the existing `trials.id` keyed by `external_id` (the TopDog event_id) — past trials get inserted as new `Trial` rows during backfill.

```python
class TrialResult(Base):
    __tablename__ = "trial_results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False, index=True)
    sub_trial_external_id = Column(String, nullable=False, index=True)  # e.g. "487"
    sub_trial_label = Column(String, nullable=True)                     # "Jumping 1", "Qualifying Heat 1"
    class_slug = Column(String, nullable=False, index=True)             # novice_agility, etc.
    class_label = Column(String, nullable=False)                        # "Novice Agility"
    height_group = Column(Integer, nullable=False, index=True)          # 200/300/400/500/600
    sct_seconds = Column(Float, nullable=True)
    course_length_m = Column(Integer, nullable=True)
    judge_name = Column(String, nullable=True)
    dog_id = Column(Integer, ForeignKey("dogs.id"), nullable=True, index=True)
    dog_name_raw = Column(String, nullable=False)        # as printed
    handler_name_raw = Column(String, nullable=True)
    time_seconds = Column(Float, nullable=True)          # null if DQ/absent
    total_faults = Column(Float, nullable=True)
    status = Column(String, nullable=True)               # "Q" | "DQ" | "ABS" | None
    nfc = Column(Boolean, default=False)
    row_index = Column(Integer, nullable=False)          # position within this height group as printed
    scraped_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("trial_id", "sub_trial_external_id", "class_slug", "height_group", "row_index"),)
```

`dog_id` is nullable so we can ingest results even for dogs we haven't created yet, then back-link in a separate matching pass.

### 2.3 `Trial` extensions
Add three nullable columns (idempotent migration):
- `discipline` `INTEGER` — TopDog discipline id (1 = Agility).
- `state` `STRING` — already declared in the model but not populated; backfill from `/results.json`.
- `results_synced_at` `DATETIME` — null = never tried; non-null = last successful results scrape.
- `results_status` `STRING` — `ok` | `none` (no results page yet) | `error:<short>` for retries.

### 2.4 `Session` extension
- `last_results_view_at` `DATETIME` — to drive a "new since last visit" highlight (trivially cheap to add now).

---

## 3. Scraper module — `app/scraper/results.py`

Plain `httpx.AsyncClient` + `BeautifulSoup`. No Playwright, no cookies.

**Lives only in the worker image.** Must never be imported at module-load time from anywhere under `app/routers/`, `app/main.py`, or `app/templates/`. Imports stay inside the inner `async def _run()` of the worker job functions, exactly like `app/worker.py:sync_session_job()` already does. (`httpx` and `beautifulsoup4` are present in `requirements.txt` for the worker but absent from `requirements.web.txt` — a stray top-level import would crash uvicorn at startup.)

```python
BASE = "https://www.topdogevents.com.au"

async def list_nsw_agility_trials(client) -> list[dict]:
    """Paginate /results.json?discipline=1&state=NSW until a short page comes back.
    Returns [{external_id, name, start_date, club_name, state}]."""

async def fetch_event_subtrials(client, event_id: str) -> list[tuple[str, str]]:
    """GET /results/{event_id}, parse <select id="trial_selection"> → [(sub_trial_id, label)]."""

async def fetch_subtrial_results(client, event_id: str, sub_trial_id: str) -> list[dict]:
    """GET /results/{event_id}/trial/{sub_trial_id}, parse cards → flat list of run dicts:
       {sub_trial_external_id, sub_trial_label, class_slug, class_label,
        height_group, sct_seconds, course_length_m, judge_name,
        dog_name_raw, handler_name_raw, time_seconds, total_faults, status, nfc, row_index}"""
```

**Parsing notes** (from `topdog_results_api.md`):
- Each class is a `<div class="card" id="d_{class_slug}">`.
- Inside, `<table>` rows alternate between height-group headers (`<td class="bg-nfc" colspan="5"><strong>Novice Agility - 300</strong>…SCT…Length…Judge…</strong>`) and run rows.
- Run row columns: `[place(blank), "Dog Name - Handler Name", blank, time, total_faults]`.
- Disqualified rows render `Disqualified` colspan=3 → set `status="DQ"`, `time_seconds=None`.
- A blank time + blank faults row is "absent" → `status="ABS"`.
- Otherwise `status="Q"` if `total_faults <= 5` else `None`. (Confirm Q rule with user; safest is to leave `status=None` and compute in the UI from `total_faults` and `sct_seconds`.)
- Split `"Dog Name - Handler Name"` on the **last** ` - ` to handle dog names that contain hyphens.

**Politeness**: shared `httpx.AsyncClient` with `timeout=30`, `limits=httpx.Limits(max_connections=4)`, an `asyncio.Semaphore(4)` around the per-trial fetches, and a small `await asyncio.sleep(0.1)` between requests. No auth, so no cookie management.

**Robustness**: each sub-trial fetch wrapped in try/except that records `results_status="error:<msg>"` on the parent trial and moves on; the weekly job retries them.

---

## 4. Worker jobs — `app/worker.py`

### 4.1 `backfill_results_job(years: int = 3)`
One-shot. Steps:
1. `trials = await list_nsw_agility_trials(client)` filtered to `start_date >= today - 3y`.
2. For each, upsert `Trial` (set `discipline=1`, `state="New South Wales"`, `external_id=str(id)`, name, start_date).
3. Enqueue one `scrape_trial_results_job(trial_id)` per trial, capped via RQ concurrency.
4. Update redis `set_sync_status("backfill", …)` so an admin progress page works.

### 4.2 `scrape_trial_results_job(trial_id)`
1. Load `Trial`. **Skip if `results_status == "ok"`** (idempotent — never re-scrape).
2. `subtrials = await fetch_event_subtrials(client, trial.external_id)`.
3. If empty → `results_status="none"`, return.
4. For each sub-trial: `runs = await fetch_subtrial_results(...)`.
5. Inside one transaction: delete any existing `TrialResult` rows for this `trial_id` (defensive — handles a partial prior run), insert fresh.
6. `results_synced_at = utcnow()`, `results_status = "ok"`.
7. Enqueue `match_results_to_dogs_job(trial_id)`.

### 4.3 `match_results_to_dogs_job(trial_id | None)`
1. For each `TrialResult` in scope (single trial, or all where `dog_id IS NULL` if `trial_id` is None):
   - Compute `(name_normalised, handler_normalised)` from `dog_name_raw` / `handler_name_raw`.
   - Look up `Dog` by `(name_normalised, handler_normalised)`, falling back to `name_normalised` only if handler is null.
   - If found, set `dog_id`. If not, **do not** auto-create — only user-known dogs get rows in `dogs`.

### 4.4 `link_session_dogs_job(session_uuid)`
Triggered at the end of the existing `sync_session_job`. For each distinct `SessionEntry.dog_name + handler` for this session, upsert a `Dog` row, then enqueue `match_results_to_dogs_job(None)` to back-link any historical results.

### 4.5 Weekly cron — new results only
Add `weekly_results_refresh_job()` (Nomad periodic — see §7). The job is **append-only**: it never re-fetches a trial whose `results_status` is `ok`.

1. Re-run `list_nsw_agility_trials()`. Upsert any new `Trial` rows. Drop trials whose `start_date < today - 3y` from consideration (rolling window).
2. Enqueue `scrape_trial_results_job` for trials where:
   - `results_status IS NULL` (never scraped), **or**
   - `results_status IN ('none', 'error:%')` **and** `start_date >= today - 60 days` (give late publications a 60-day grace — wider than the weekly cadence; older "none" trials almost never get results).
3. Trials with `results_status = 'ok'` are skipped — even if results were corrected later, we accept the staleness.

`scrape_trial_results_job` itself has the same guard at the top so a manual re-enqueue is also idempotent.

---

## 5. Routes & UI — `app/routers/results.py` + `app/templates/`

New router mounted at `/results` (rename only if it conflicts with anything — `app/routers/sessions.py` doesn't currently own it).

**Web-tier only.** Imports are restricted to `app.models`, `app.database`, `fastapi`, `sqlalchemy`, `jinja2`. Stats are computed via SQL aggregates (Postgres handles `AVG`, `COUNT FILTER (WHERE …)`, `MIN`, `MAX` natively; SQLite has the same except `FILTER` — write the conditional aggregates as `SUM(CASE WHEN … THEN 1 ELSE 0 END)` to stay portable). No scraping, no httpx, no bs4 — those aren't in the slim image.

### 5.1 Routes
- `GET /s/{uuid}/results` → "Past Results" page for this session.
  - Two panes: a **stats summary** at the top (one card per dog), then a filterable **runs table** below.
  - Joins `SessionEntry → Dog → TrialResult → Trial` for the session's dogs.
  - Filters: dog (default = all), class (`novice_agility`/etc.), height_group, date range, status (DQ/ABS/clean/sub-SCT).
  - Sort: date desc by default.
  - Pagination via HTMX.
- `GET /s/{uuid}/results/dog/{dog_id}` → single-dog detail with the per-dog stats expanded (per class+height breakdown) plus the run list scoped to that dog.
- `POST /admin/results/backfill` → admin-only trigger for `backfill_results_job` (gate behind a simple env-var token; no real auth in this app).
- `GET /admin/results/status` → progress (per-trial last sync time, counts, error breakdown).

### 5.2 Templates
- `templates/results.html` — extends `base.html`. Top: stats cards per dog. Below: filter bar + runs table.
- `templates/partials/results_row.html` — per-row partial used by HTMX pagination.
- `templates/partials/dog_stats_card.html` — the per-dog summary card (also reused on the single-dog page).
- Add a "Past Results" link to `base.html` nav, gated to sessions that have at least one matched run.

### 5.3 Run-row display
For each row: `Date · Trial name · Class · Height · Time · Faults · Status · Δ vs SCT` (`time - sct` shown as `±N.Ns`, blank if no time). Group visually by date.

### 5.4 Stats (computed in SQL, derivable from public data)

Per-dog summary card shows:
- **Run count** total + per class + per height group.
- **Clean runs** (`total_faults = 0`) and **clean-run rate** as `clean / completed` where `completed = total - dq - abs`.
- **Sub-SCT runs** (`time_seconds <= sct_seconds`) and **sub-SCT rate**.
- **DQ rate** (`dq / total`).
- **Fastest time per (class, height)** — link through to the row.
- **Average time delta vs SCT** per (class, height) — `avg(time_seconds - sct_seconds)` over completed runs only.
- **First / last seen** dates and **trial count**.
- **Trend over the 3-year window**: a tiny inline sparkline of `time_seconds - sct_seconds` per run, ordered by date, per class+height. (CSS bars, no chart library — matches the existing minimal Tailwind+HTMX style.)

Stats deliberately excluded from v1:
- A blanket "Q/qualifying" rate. ANKC qualification rules vary by class (Novice = 0 faults; Excellent/Masters typically ≤ 5 with within-SCT) and the public table doesn't expose a definitive Q flag, so we surface the underlying numbers (clean, sub-SCT, faults) and let the handler interpret.
- Title progression / points tracking — needs class-by-class qualification rules and ANKC title definitions; out of scope.

---

## 6. Matching nuances

The public HTML gives only `"<Dog Name> - <Handler Name>"`. Normalisation rules:

```python
def normalise(s: str | None) -> str | None:
    if not s: return None
    s = s.lower()
    s = re.sub(r"\(ai\)", "", s)            # artificial insemination suffix
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None
```

Splitting `cell_text` on the **last** ` - ` (with surrounding spaces) so dogs whose names contain hyphens (`Foo - Bar Baz - Handler`) parse correctly. If split fails, dog = whole text, handler = None.

Match priority:
1. `(name_normalised, handler_normalised)` exact.
2. `name_normalised` only (one Dog row matches).
3. `name_normalised` only with multiple matches → leave `dog_id=null`, surface in admin "ambiguous" report later.

---

## 7. Rollout

1. **Migration commit.** Add `Dog`, `TrialResult`, `Trial` columns, `Session.last_results_view_at`. Generalise `_migrate_per_height_tpd()` into a `_migrate()` helper. Backfill `dogs` from `session_entries`.
2. **Scraper commit.** Add `app/scraper/results.py` + unit-style smoke test against trial 256 (cached HTML in `tests/fixtures/`) so parsing is verifiable without network.
3. **Worker commit.** Add the four new jobs in `app/worker.py`. Hook `link_session_dogs_job` into the existing `sync_session_job`.
4. **One-shot backfill.** Run `backfill_results_job(3)` from a Nomad one-off task or a `nomad job dispatch`. Estimate volume: NSW agility ≈ 150 trials/year × 3 = ~450 trials × ~150 runs avg ≈ ~70k `trial_results` rows. SQLite handles this easily.
5. **Schedule weekly.** Add a **new Nomad job** `barhopping-results-cron.nomad.hcl` (separate from `barhopping.nomad.hcl`) of `type = "batch"` with a `periodic` stanza. Runs Mon 03:00 Sydney time:

   ```hcl
   job "bar-hopping-results-cron" {
     type = "batch"
     periodic {
       cron             = "0 3 * * 1"
       time_zone        = "Australia/Sydney"
       prohibit_overlap = true
     }
     group "enqueue" {
       task "trigger" {
         driver = "docker"
         config {
           image      = "ghcr.io/pcareyrh/bar-hopping:main"  # the worker image — has python + redis + rq + app code
           force_pull = true
           command    = "python3"
           args       = ["-c", "from app.queue import get_queue; get_queue().enqueue('app.worker.weekly_results_refresh_job', job_timeout=3600)"]
         }
         template {
           data = <<EOH
   {{ range service "bar-hopping-redis" -}}
   REDIS_URL="redis://{{ .Address }}:{{ .Port }}"
   {{ end -}}
   EOH
           destination = "secrets/cron.env"
           env         = true
         }
       }
     }
   }
   ```

   This is the cleanest fit for the existing Nomad-based setup — no new long-running scheduler container, no in-app cron, no new dependency. The trigger task just enqueues; the existing worker dequeues and does the work, naturally serialised behind any in-flight scrape jobs. Job is append-only — it will not touch any trial already at `results_status="ok"`.
6. **UI commit.** Add the router + templates + nav link. Ship behind a simple feature flag (`RESULTS_ENABLED=1`) so it can be rolled out independently.
7. **Verify.** Spot-check a known dog's known trial against the live TopDog page to confirm parsing.

---

## 8. Risks / open issues

- **Site HTML drift.** Parsing is brittle. Mitigation: a single golden fixture in `tests/fixtures/results_256.html` + a parser test that fails loudly when the column count or class container ids change.
- **Duplicate dog names across handlers.** Rare for ANKC-registered names but possible. The `(name, handler)` composite key handles it; the fallback name-only match is best-effort and surfaced in admin.
- **A dog with multiple handlers.** A single dog can be run by different handlers across trials. Composite-key matching will treat them as separate `Dog` rows. v1 accepts that; v2 could merge by registration number once we scrape `/dogs.json`.
- **State filter accuracy.** `/results.json?state=NSW` filters by **club** state, which is almost always the venue state but not guaranteed. Acceptable for v1.
- **TopDog rate limiting.** Unknown. Conservative concurrency (4) + 100ms gap should be safe; surface 429s with backoff if they appear.
- **Backfill cost.** ~450 page fetches for trial discovery + sub-trials, plus ~1–4 sub-trial pages each → ~1,500 HTTP requests. At the chosen pace, ≈ 5–10 minutes. Fine.
- **No registration numbers** means we cannot disambiguate two dogs with identical name+handler (effectively zero in practice). Fix later via authenticated scraping if it becomes a real problem.
- **Migration management.** The `inspect-and-ALTER` pattern is fine for a handful of additive columns but doesn't track dropped columns, type changes, or out-of-order deploys across the web/worker images. If this subsystem grows, switching to **Alembic** becomes worthwhile. Out of scope for v1.
- **Web/worker image drift.** A future contributor importing `app.scraper.X` from `app/routers/...` would crash the web image without affecting the worker — the failure surfaces only at uvicorn startup. Mitigation: a lint check or the parser-test could `import app.routers.results` under the slim `requirements.web.txt` to catch it in CI. Worth a follow-up, not a blocker.
- **Cron triggering vs running.** The Nomad periodic job *enqueues*; the worker *runs*. If the worker is busy with a long sync at 03:00, the refresh runs after — which is fine because the job is idempotent and `prohibit_overlap = true` guards re-entry of the trigger itself. If the worker is **down** when the cron fires, the enqueue still succeeds (Redis stores the job) and runs when the worker comes back. The only real failure mode is Redis being down at trigger time — acceptable given Redis sits in the same Nomad job.

---

## 9. File-level change list

```
new   app/scraper/results.py                       # parsers + httpx fetchers (worker-image only)
edit  app/models.py                                # +Dog, +TrialResult, +Trial cols, +Session col
edit  app/main.py                                  # generalise _migrate, run new migrations + dog seed
edit  app/worker.py                                # +backfill, +scrape_trial, +match, +weekly, hook link_session_dogs
new   app/routers/results.py                       # /s/{uuid}/results + admin endpoints (web-image, no scraper imports)
new   app/templates/results.html                   # main page
new   app/templates/partials/results_row.html      # htmx row partial
new   app/templates/partials/dog_stats_card.html   # per-dog stats card
edit  app/templates/base.html                      # nav link (gated)
new   barhopping-results-cron.nomad.hcl            # NEW periodic batch job — weekly cron trigger
new   tests/fixtures/results_256.html              # parser fixture
new   tests/test_results_parser.py                 # parser smoke test
```

**Untouched:** `app/database.py` (already env-driven), `app/queue.py`, `app/crypto.py`, `app/scraper/auth.py`, `app/scraper/catalogue.py`, `app/scraper/schedule.py`, `app/scraper/trials.py`, `Dockerfile`, `Dockerfile.web`, `requirements.txt`, `requirements.web.txt`, `barhopping.nomad.hcl`, `barhopping-public.nomad.hcl`.

No new runtime dependencies needed — the worker image already has `httpx` and `beautifulsoup4`. The web image needs nothing new.
