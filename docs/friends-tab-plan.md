# Plan: "Friends" tab on the Schedule page

**Status:** Proposal / design plan
**Scope:** Add a new **Friends** tab to the predicted-schedule page so a user can look up other competitors (by **CAT#** or **Handler Name**) and see *their* predicted events and run times — supporting **multiple friends**, each with **multiple dogs**.

---

## 1. Goal

Today the schedule page only predicts runs for the dogs belonging to the logged-in planning session (the user's own `SessionEntry` rows). Handlers at a trial frequently want to know when their friends run — to crew, cheer, video, or coordinate leaving. This feature lets the user pin a set of "friends" for a given trial and view their predicted schedule using the exact same prediction engine.

**Acceptance criteria**

- On the schedule page, a tab bar lets the user switch between **Mine** and **Friends** (and optionally keep **Full Day** as a third tab).
- In the **Friends** tab the user can add a friend by typing either a **CAT#** or a **Handler Name**.
- A handler with several dogs shows *all* of that handler's dogs and runs.
- Multiple friends can be added; each persists for that trial and that planning session.
- Friend runs are predicted with the same math as the user's own runs (scheduled start + setup + walk + position × sec/dog).
- A friend can be removed.
- Works on multi-day and Nationals-style coded-round trials (same fan-out behaviour as own entries).

---

## 2. Why this is very achievable (the key insight)

**The data for every competitor is already in the database.** The prediction engine does not need the logged-in user at all — it only needs a `CatalogueEntry` plus the session's timing defaults.

- `CatalogueEntry` (`app/models.py:75`) already stores **every** entry in a trial's catalogue: `dog_name`, **`handler_name`**, `cat_number`, `event_name`, `height_group`, `run_position`, `height_group_total`, `nfc`, `ring_number`, `day`.
- This table is populated trial-wide whenever the catalogue / `my_day` data is fetched (`refresh_trial_docs_job` in `app/worker.py`), and it is explicitly "shared with everyone entered in it."
- The prediction core, `predict_run` / `predict_run_from_block` (`app/engine/predictor.py`), takes a scheduled start (or block start), `run_position`, and `avg_time_per_dog` — none of which is user-specific.
- `_predict_for_ce()` (nested in `_build_predictions`, `app/routers/schedule.py:460`) already turns a `CatalogueEntry` into a prediction card dict. The only user-specific inputs it pulls from `SessionEntry` are the two optional overrides (`position_override`, `time_per_dog_override`) and `dog_name`/`ring_number`.

So **no new scraping is required**. Friends are a *read* over data that already exists, gated only on the catalogue being present for that trial. The work is mostly: persistence for the friends list, a lookup/resolve step, a small refactor of the predictor orchestration to accept a non-`SessionEntry` source, and UI (tabs + add/remove form).

### Important caveat (must be surfaced in the UI)

Friends lookup depends entirely on which **catalogue source** has populated `CatalogueEntry` for the trial — and the sources differ a lot in coverage. `handler_name` and individual run rows are only present in the **full catalogue (xlsx/PDF)** and **`my_day`** sources; the user's own `/entries` sync carries no handler names, and the HTML `/entries` **summary** fallback has **no individual rows at all** (only per-class counts via sentinel cat#s like `~Sat~400`). So Friends is only meaningful once a real catalogue or `my_day` fetch has happened, and the UI must make the current data state obvious rather than showing a confusing empty list.

This is important enough that it gets its own section: see **[§5 Data sources & availability](#5-data-sources--availability)** for the full source matrix and the exact UI states, and **[§6 Day-of data collection button](#6-day-of-data-collection-button)** for how the user refreshes/collects friend data on the trial day.

---

## 3. Current-state recap (anchored to code)

| Concern | Where | Notes |
|---|---|---|
| Schedule page route | `schedule_view`, `app/routers/schedule.py:25` | Renders `schedule.html`; no tabs today (single stacked page: "Predicted Schedule" + "Full Day Schedule"). |
| Prediction pipeline | `_build_predictions`, `app/routers/schedule.py:412` | Iterates **only** `SessionEntry` for the session+trial. |
| Per-run prediction | `_predict_for_ce`, `app/routers/schedule.py:460` | `CatalogueEntry` → prediction dict. |
| Fan-out (multi-day / rounds) | `cat_by_key` grouping, `app/routers/schedule.py:455-559` | Groups by `(_strip_event_code(event_name), cat_number)`; one card per run. |
| Time math | `app/engine/predictor.py` | `predict_run`, `predict_run_from_block`, `flag_conflicts`. |
| Run card UI | `app/templates/partials/run_card.html` | Shows dog, event, time, height, ring, position; inline HTMX "Adjust" form. **Does not show handler name.** |
| Data model | `app/models.py` | `Session`, `Trial`, `CatalogueEntry`, `ClassSchedule`, `SessionEntry`, `TrialLunchBreak`. No `Handler` / `Friend` entity. |
| Catalogue source | `app/worker.py`, `app/scraper/my_day.py`, `app/scraper/catalogue.py` | `handler_name` comes from `my_day` detail pages and FINAL xlsx/PDF. |
| Migrations | `_migrate()` in `app/main.py` | Additive, auto-run on startup for SQLite + Postgres. |

---

## 4. Design

### 4.1 Data model — new `SessionFriend` table

Persist the user's chosen friends per planning session and trial. CAT# is the most reliable join key within a trial, but a handler can own several cat#s (one per dog/class), so we model a **friend** as a handler-level pin and resolve their dogs at render time.

```python
class SessionFriend(Base):
    __tablename__ = "session_friends"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_uuid = Column(String, ForeignKey("sessions.uuid"), nullable=False)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    # What the user typed / how we resolved them:
    handler_name = Column(String, nullable=True)   # normalized handler name (preferred key)
    cat_number = Column(String, nullable=True)      # the cat# the user entered, if any
    label = Column(String, nullable=True)           # display name (defaults to handler_name)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("session_uuid", "trial_id", "handler_name", "cat_number"),)
```

**Resolution rules**

- **Added by CAT#:** look up the `CatalogueEntry` for `(trial_id, cat_number)`. Store its `handler_name` (if present) so the pin survives catalogue refreshes and expands to *all* the handler's dogs. If the catalogue has no handler name (older/partial data), fall back to pinning by cat# only (single dog).
- **Added by Handler Name:** normalize and match against distinct `CatalogueEntry.handler_name` for the trial. Store the canonical handler string.

> **Decision:** model friends at the **handler** level (so "multiple dogs per handler" is automatic), with cat# as a fallback when no handler name is available. This matches the user's phrasing ("that handler's events").

### 4.2 Backend — friend resolution + prediction

**a) Refactor the predictor orchestration to be source-agnostic.**

`_predict_for_ce(entry, ce)` currently reads `entry.id`, `entry.dog_name`, `entry.ring_number`, `entry.position_override`, `entry.time_per_dog_override`. Refactor so it can build a prediction dict from a `CatalogueEntry` alone, with overrides optional:

- Extract a helper `predict_catalogue_entry(ce, *, session, schedules, block_starts, cat_by_key, dog_name=None, overrides=None, owner="friend")` that does not require a `SessionEntry`.
- For friends, `dog_name` comes straight from `CatalogueEntry.dog_name`; `overrides` are `None` (friend runs are **read-only** in v1); add an `owner`/`is_friend` flag and a `handler_name` field to the dict.
- Keep `_build_predictions` for "Mine" using the same helper (pass the `SessionEntry` overrides + `entry.id`), so both paths share one code path and one set of tests.

**b) New `_build_friend_predictions(session, trial, db)`** that:

1. Loads `SessionFriend` rows for `(session_uuid, trial_id)`.
2. For each friend, collects their `CatalogueEntry` rows: by `handler_name` (preferred) else by `cat_number`.
3. Fans out per the existing `(_strip_event_code(event_name), cat_number)` grouping so multi-day / coded rounds each get a card (reuse the same `cat_by_key` logic — factor it out so it isn't duplicated).
4. Produces prediction dicts grouped by friend (and by dog within a friend) for display.
5. Optionally calls `flag_conflicts()` across the combined Mine + Friends set so the user can spot when a friend's run clashes with their own (powerful for crewing).

**c) Lookup endpoint(s).** Two practical options:

- **Server-rendered (simplest, matches stack):** an "Add friend" form that POSTs `query` (cat# or name). The handler decides: if `query` matches `^\d{2,4}(NFC)?$` treat as cat#, else treat as handler-name search. On ambiguous/multiple name matches, re-render with a small disambiguation list to pick from.
- **HTMX live search (nicer UX):** `GET /s/{uuid}/trials/{id}/friends/search?q=...` returning an HTMX fragment of matching handlers (distinct `handler_name` + dog count). Click adds. This fits the existing HTMX-only frontend (no SPA).

> **Recommendation:** ship the server-rendered form first (smallest, fully covered by tests), then layer HTMX autocomplete as a follow-up.

### 4.3 Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/s/{uuid}/trials/{id}/schedule?tab=friends` | Same page; `tab` selects which section renders (default `mine`). |
| `GET` | `/s/{uuid}/trials/{id}/friends/search?q=` | (Optional, HTMX) handler/cat# autocomplete fragment. |
| `POST` | `/s/{uuid}/trials/{id}/friends` | Add a friend (`query` or explicit `handler_name`/`cat_number`). Returns the friends fragment (HTMX) or redirects. |
| `POST` | `/s/{uuid}/trials/{id}/friends/{friend_id}/delete` | Remove a friend. |
| `POST` | `/s/{uuid}/trials/{id}/friends/refresh` | Day-of "Find/refresh friends' runs" — enqueue a `my_day`-first, non-destructive collection job (see §6). |

Keep everything under the existing `/s/{uuid}/...` shareable-link scheme and reuse `_get_session` / `_get_trial`.

### 4.4 UI

**Tab bar** in `schedule.html` (above the content), styled like the existing day pills:

```
[ Mine ]  [ Friends ]   ( [ Full Day ] )
```

- Server-side: `tab` query param drives which block renders; the active pill highlights with `bg-brand text-white`. This avoids client JS and keeps deep links shareable. (Optional progressive enhancement: HTMX `hx-get` swap for instant switching without full reload.)
- Consider moving the existing "Full Day Schedule" into its own tab to declutter, or leave it stacked under "Mine" — **recommend** making it a third tab for a cleaner mobile page.

**Friends tab content**

1. **Add-friend form** at top: single text input ("CAT# or Handler name") + Add button; helper text. Disambiguation list appears when a name matches several handlers.
2. **Friend groups:** one collapsible section per friend (handler), showing the handler name/label, a remove (×) button, and that handler's predicted run cards (grouped by dog, sorted by predicted time).
3. **Data-status line + refresh button** (see §5.3 and §6): a compact line showing source/freshness and a **"Find friends' runs" / "Refresh friend data"** button that drives the day-of collection job.
4. **Empty states** (driven by `friend_data_state`, §5.2): (a) `available` + no friends added → prompt to add; (b) `none`/`summary_only` → "Friend lookup needs the trial running order" + refresh button; (c) `partial` → show ready days, note pending days.

**Run card reuse**

- Reuse `partials/run_card.html` with a new flag (e.g. `p.is_friend`) that:
  - **Hides the "Adjust" form** (friends are read-only in v1).
  - **Shows the handler name** (currently never displayed). Add `handler_name` to the prediction dict and render it under the dog name when `p.is_friend` (or always, behind a conditional).
- Use a distinct accent (e.g. neutral/blue border) vs the brand-green "You" styling so friend cards are visually separable.

---

## 5. Data sources & availability

The Friends tab is a read over `CatalogueEntry`, so what works depends entirely on **which source last populated that table for the trial**. The sources are not equal — only some carry the per-dog rows and handler names Friends needs. The pipeline (`refresh_trial_docs_job` in `app/worker.py`) tries them in a priority order and merges results.

### 5.1 Source matrix

| # | Source | How / where | Individual dog rows? | Handler names? | Run order (position)? | Multi-day coverage | Supports Friends? |
|---|--------|-------------|----------------------|----------------|------------------------|--------------------|-------------------|
| 1 | **User `/entries` sync** | `sync_user_entries` (`app/scraper/auth.py`); populates `SessionEntry` | Only the user's own dogs | **No** | No | n/a | **No** — your dogs only |
| 2 | **`my_day` (authenticated)** | `fetch_my_day` (`app/scraper/my_day.py`) → `/trials/{id}/my_day` + class-detail pages | **Yes — all competitors** | **Yes** (`app/scraper/my_day.py:233,338`) | **Yes** (in-page order) | Often **current/next day only** until later days approach; supplemented by the catalogue PDF in `refresh_trial_docs_job` | **Yes — best on the day** (reflects scratches) |
| 3 | **FINAL xlsx catalogue** | `parse_catalogue_xlsx` (`app/scraper/catalogue.py`), public download | **Yes — all competitors** | **Yes** (`catalogue.py:20`) | **Yes** (row order) | **All days** | **Yes** |
| 4 | **PDF catalogue** | `parse_catalogue_pdf*` / AI extraction (`app/scraper/openrouter_catalogue.py:38`) | **Yes — all competitors** | **Yes** | **Yes** | **All days** | **Yes** |
| 5 | **Manual upload** | `upload_catalogue_job` (`app/worker.py:438`) parses uploaded xlsx/PDF | **Yes** | **Yes** | **Yes** | **All days** | **Yes** |
| 6 | **HTML `/entries` summary (fallback)** | `download_and_parse_catalogue_entries` (`app/scraper/catalogue.py:526`) | **No** — one sentinel row per `(day, event, height)` (`cat_number='~Sat~400'`, `run_position=0`) | **No** (`dog_name`/`handler_name` are `None`) | No | Per-day counts only | **No** — counts only, nothing to look up |

**Takeaways**

- **Friends works only when source 2, 3, 4, or 5 has run for the trial** (i.e. real per-dog catalogue rows exist).
- **`my_day` is the day-of source.** It reflects post-scratch running order, so on the trial day it's the freshest/most accurate for friend times — but it may only carry the current/next day until later days come into range.
- **Source 6 is a trap:** the trial may *have* catalogue rows, yet they're summary sentinels with no handlers. Friends must detect this (e.g. all `cat_number` start with `~`, or `handler_name`/`dog_name` all null) and treat it as "not available," **not** as "no friends found."

### 5.2 Detecting the current state (server-side)

A small helper, e.g. `friend_data_state(trial, db)`, classifies the trial into one of:

- `none` — no `CatalogueEntry` rows at all.
- `summary_only` — rows exist but are sentinel/no-handler (source 6). Friends unavailable.
- `available` — real per-dog rows with handler names exist (sources 2–5).
- `partial` — `available` for some days but missing/`summary_only` for others (common mid-event multi-day). Surface which days are ready.

Implementation: count `CatalogueEntry` rows where `handler_name IS NOT NULL` (and/or `cat_number NOT LIKE '~%'`), grouped by `day`. Reuse `Trial.scraped_at` for a "last updated" timestamp.

### 5.3 Making it clear to the user

The Friends tab should always show a compact **data-status line** (and matching empty states) so the dependency is never a mystery:

- **`available`** → subtle line: *"Friend data from the catalogue · updated {scraped_at}."* On the trial day, prefer *"Live from my_day · updated {time}"* when the last source was `my_day`.
- **`summary_only`** → callout: *"Only a summary of this trial is available, so individual competitors can't be looked up yet. Tap **Find friends' runs** to pull the full running order."* (button → §6).
- **`none`** → callout: *"The trial catalogue hasn't been published/collected yet. Friend lookup needs it. Tap **Find friends' runs** to check now."*
- **`partial`** → show available friends/days and a per-day note: *"Day 2 running order isn't available yet."* plus the refresh button.
- **Per-card freshness on the day:** when source is `my_day`, optionally badge friend cards as "live order" so users trust scratched/re-ordered times.
- **Disambiguation vs. unavailable:** never conflate "handler not found in an available catalogue" (→ *"No competitor matched 'Smith'."*) with "catalogue unavailable" (→ refresh prompt). They are different states and different copy.

> Reuse the existing `has_catalogue` signal already computed on the trial detail page (`app/routers/trials.py:71`) but **upgrade it** to the richer `friend_data_state` above, since `has_catalogue` is true even for the summary-only trap.

---

## 6. Day-of data collection button

> Requested: *"If the data is available on the day through my_day, add a button to the tab that will support collecting data for friends on the trial day, or add new friend details when available."*

### 6.1 What already exists

- `POST /s/{uuid}/trials/{trial_id}/refresh` (`app/routers/trials.py:87`) enqueues `refresh_trial_docs_job`, which **prefers `my_day`** and falls back to xlsx/PDF.
- `refresh_trial_docs_job` merges `my_day` results **per day** via `_merge_catalogue_entries` (`app/worker.py:235`) — it only replaces the days present in the payload, leaving other days intact, then re-resolves links. This makes a `my_day` refresh **safe to run repeatedly on the day**.
- Auth is resolved from the session's encrypted TopDog creds, falling back to `TOPDOG_USER`/`TOPDOG_PW` env (`_resolve_auth_cookies`, `app/worker.py:205`).
- Progress is already modelled for the initial sync via `set_sync_status` + `/s/{uuid}/syncing` + the `/s/{uuid}/sync-status` HTMX poll (`app/routers/sessions.py:63-104`).

### 6.2 The two gotchas to design around

1. **Overwrite guard.** The existing refresh route **blocks** when a catalogue already exists unless `overwrite=1` (`app/routers/trials.py:102`) — deliberately, because regenerating an AI-parsed PDF is expensive. For Friends we want a *day-of* refresh that is cheap and non-destructive. The `my_day` merge is per-day and safe, so the Friends refresh should be **allowed even when a catalogue exists**, but it must **not** trigger the destructive HTML-summary fallback.
2. **Don't let the summary fallback clobber real rows.** If `my_day` is unavailable (no auth / 404) the job can fall back to source 6, which would overwrite good per-dog rows with sentinels. Guard against this in the Friends path.

### 6.3 Proposed design

Add a dedicated, idempotent endpoint and button rather than reusing the guarded generic refresh:

- **Route:** `POST /s/{uuid}/trials/{id}/friends/refresh` → enqueue a `my_day`-first refresh.
- **Worker:** either pass a flag to `refresh_trial_docs_job` (e.g. `prefer_my_day=True, allow_summary_fallback=False`) or add a thin `collect_friend_data_job` that:
  1. Resolves auth cookies; if none, return a clear "connect your TopDog account to collect live data" state (env creds may still work).
  2. Fetches `my_day`; on success, **per-day merge** (non-destructive) and re-resolve links.
  3. Supplements later days from the catalogue PDF (the job already does this, `app/worker.py:369-392`).
  4. **Skips** the summary-only fallback so existing real rows are never downgraded.
- **Button placement:** in the Friends tab header — label **"Find friends' runs"** (state `none`/`summary_only`) or **"Refresh friend data"** (state `available`/on the day). Show `scraped_at` next to it.
- **Progress:** reuse the `set_sync_status` + HTMX poll pattern (or a lightweight `hx-post` + spinner that swaps in the refreshed friends fragment). On completion, re-render the Friends tab.
- **"Add new friend details when available":** because friends are pinned at the **handler level** and **resolved at render time**, a successful refresh automatically:
  - enriches existing friends with newly-present dogs/runs (e.g. a later day, or a class added after first sync),
  - reflects day-of scratches/re-ordering in predicted times,
  - makes newly-present handlers searchable so the user can add more friends.
  No friend rows are cached, so nothing needs migrating on refresh.

### 6.4 Caveats to surface

- **Auth required for `my_day`.** If the session has no TopDog creds and no env fallback, the button should explain that live collection needs a connected account; the public xlsx/PDF may still be fetchable.
- **Cost/rate.** `my_day` fetches every class-detail page (capped at 4 concurrent). Debounce the button (disable while a job is queued/running) to avoid hammering TopDog.
- **Eventual consistency.** It's a background job; the tab shows "collecting…" then refreshes. Don't block the request thread.

---

## 7. Multi-day support

Multi-day (and Nationals multi-round) trials are already first-class in the schema and engine; Friends inherits this with little new work.

### 7.1 What the model/engine already give us

- **`CatalogueEntry.day`** (`app/models.py:80`) tags every row with its day; the unique key is `(trial_id, event_name, cat_number, day)`.
- **Fan-out across days/rounds** is handled by grouping on `(_strip_event_code(event_name), cat_number)` (`app/routers/schedule.py:455-559`): one card per run, including the same dog on multiple days and Nationals coded rounds (ADM1/ADM2, ADO1/2/3). **Friends reuse this exact grouping**, so a friend with runs across Sat/Sun gets a card per day automatically.
- **Per-day prediction anchoring:** each prediction is anchored to its own calendar date (`day_date = trial.start_date + (day-1)`, `app/routers/schedule.py:466`), and `_match_class_schedule` is **day-aware** (`app/routers/schedule.py:565`), so the same class on different days picks up its own start time. `block_starts` is keyed by `(event, height, day)`.
- **Cross-day conflicts are already avoided** because `flag_conflicts` compares datetimes anchored per day — so a friend's Sunday run won't false-clash with your Saturday run.

### 7.2 Friends-specific multi-day behaviour

- **Pins are day-agnostic (handler-level).** A `SessionFriend` is not tied to a day, so one pin covers all of that handler's runs across every day — no per-day re-adding.
- **UI mirrors "Mine" day grouping.** The Friends tab reuses the `Day N` header pattern from `schedule.html:20-32` (and/or the existing `?day=N` filter from the Full Day section, `schedule.html:39-46`). Within a friend, group by dog, then list runs with day labels in time order.
- **Combined day filter.** If the schedule page adopts a single day selector, apply it across Mine + Friends + Full Day so "show me everything on Day 2" works consistently.

### 7.3 Multi-day availability (ties to §5 + §6)

- **`my_day` often covers only the current/next day** before the weekend, while the catalogue PDF/xlsx carries all days. `refresh_trial_docs_job` already **merges** them: `my_day` for the live day(s), PDF for the rest (`app/worker.py:369-392`).
- This means a friend can be **`available` for Day 1 but `partial`/pending for Day 2** mid-event. The Friends tab should:
  - show ready days' runs immediately,
  - mark not-yet-available days (*"Day 2 running order not available yet — tap Refresh friend data"*),
  - re-resolve and fill those days when §6's refresh succeeds.
- **Day-of accuracy:** on each trial day, the §6 refresh pulls that day's `my_day` order (post-scratch) and per-day-merges it without disturbing other days — so friend times stay accurate as the event progresses.

### 7.4 Testing additions for multi-day

- Friend with runs on 2+ days → one card per day, correct day labels, sorted within day.
- Day-aware times: same class different days picks up each day's `ClassSchedule.scheduled_start`.
- No cross-day false conflicts in the combined Mine+Friends conflict pass.
- `partial` state: Day 1 `available`, Day 2 `summary_only`/missing → correct per-day messaging; after a simulated `my_day` merge for Day 2, runs appear.

---

## 8. Edge cases & risks

| Case | Handling |
|---|---|
| **Catalogue not yet fetched / summary-only** | Classify via `friend_data_state` (§5.2); show the right empty state + day-of refresh button (§6). Don't mistake the summary-only trap (source 6) for "no friends." |
| **Handler with multiple dogs** | Native: query all `CatalogueEntry` for the handler → all dogs/classes appear. |
| **Same handler name, different people** | Name matching is fuzzy. Provide disambiguation (show dog names under each match). Storing both `handler_name` + an example `cat_number` helps. |
| **Joint handlers / `·` prefixes** | `my_day` handler strings can have leading `·`/whitespace (already stripped in `app/scraper/my_day.py:233`). Normalize consistently (trim, collapse whitespace, case-insensitive compare) in a shared helper. |
| **CAT# with NFC suffix** | Accept `\d{2,4}(NFC)?`; the engine already flags `nfc`. |
| **Multi-day & Nationals coded rounds** | Reuse existing `_strip_event_code` + `cat_by_key` fan-out so each run gets a card. |
| **Friend not found** | Re-render form with "No competitor found for '…'." |
| **Catalogue refresh changes cat#/handler** | Because we pin by `handler_name` primarily, friends survive re-import; resolve at render time (don't cache prediction rows). |
| **Privacy** | Catalogue is public trial data already shown trial-wide, so no new exposure. Still: friends are read-only (no overrides written for others). |
| **Performance** | One extra query per friend (or a single `IN` query for all pinned handlers). Negligible at trial scale; can batch into one `CatalogueEntry` query filtered by the set of pinned handler names/cat#s. |

---

## 9. Feedback & ideas (beyond the core ask)

These are optional enhancements — flagged so the team can decide scope:

1. **Conflict awareness across Mine + Friends.** Run `flag_conflicts()` over the union and badge a friend's run when it clashes with one of *your* runs. Hugely useful for "can I crew for them?" — arguably the most valuable add-on and cheap to build.
2. **"Crew view" sort.** Offer a combined timeline (your runs + friends' runs interleaved by time) so the user sees their whole day, not separate lists. Could be the Friends tab's default view.
3. **Quick-add from Full Day Schedule.** Let the user tap a block/competitor to pin as a friend, reducing typing.
4. **Autocomplete (HTMX).** Live handler/dog search-as-you-type; much nicer than guessing exact spelling. Recommended fast-follow.
5. **Add by dog name too.** The ask says CAT#/Handler, but dog name is the most memorable identifier for many users. Cheap to support (same table, `dog_name ILIKE`). Worth considering.
6. **"Notify me N runs before."** Since we predict friend times, a simple "X runs out / ~Y min away" indicator per friend dog is high-value on the day.
7. **Persist friends across trials.** v1 pins per trial. A later "favourites" concept could remember handlers you follow and auto-pin them at each new trial (match by handler name). Note: handler identity is not stable cross-trial today, so this needs a normalization/identity strategy.
8. **Share a friend bundle.** Since sessions are already shareable by UUID, friends pinned in a session are automatically shareable — worth calling out as a feature.
9. **Read-only is the right v1 default.** Don't let users override *other people's* positions/timing. If demand exists, allow *local-only* overrides stored on `SessionFriend` later.
10. **Empty-catalogue UX.** Covered in §5.3/§6 — tie the Friends empty state to the day-of "Find friends' runs" affordance so users understand and can resolve the dependency.

---

## 10. Testing plan

The repo's prediction/parsing logic is well covered by pytest (`tests/test_schedule_fanout.py`, `tests/test_my_day_parser.py`, etc.) and runs on in-memory SQLite with no network. Mirror that.

**Unit / integration (pytest):**

- `SessionFriend` model + migration: table is created by `_migrate()` on a fresh and on an existing DB (additive).
- Resolve-by-cat#: seed `CatalogueEntry`, add friend by cat#, assert handler + all their dogs resolve.
- Resolve-by-handler-name: case-insensitive, whitespace/`·`-tolerant matching; multi-dog handler returns all dogs.
- Disambiguation: two distinct handlers, same name → both surfaced.
- Fan-out parity: a friend with multi-day and coded-round entries yields the same card count as the equivalent own-entry test in `test_schedule_fanout.py`.
- Prediction parity: friend prediction times equal own-entry predictions for the same `CatalogueEntry` + session defaults (proves the shared code path).
- Read-only: no `position_override`/`time_per_dog_override` written for friends.
- Empty states: no catalogue → friends empty state; cat# not found → friendly error.
- Remove friend.
- **Data-state classifier (`friend_data_state`):** `none` (no rows), `summary_only` (sentinel `~%`/null-handler rows), `available` (real handler rows), `partial` (mixed by day) each classify correctly.
- **Day-of refresh job:** a `my_day`-style payload per-day-merges without wiping other days; the summary-only fallback is skipped so existing real rows aren't downgraded; friends re-resolve and pick up newly-added dogs/handlers afterward.
- **Multi-day:** see §7.4 (per-day cards, day-aware times, no cross-day false conflicts, `partial`→filled after refresh).

**Manual (GUI via computerUse + screen recording):**

- Seed a session + a trial with catalogue data (or use an existing fixture path), open the schedule page, switch to **Friends**, add a friend by **CAT#** and by **Handler Name**, confirm multiple dogs render with predicted times, remove a friend. Capture a demo video + before/after screenshots for the walkthrough.

**Lint/syntax:** `.venv/bin/python -m py_compile $(find app scripts migrations -name '*.py')` and `.venv/bin/python -m pytest -q`.

---

## 11. Phased implementation

1. **Model + migration:** add `SessionFriend`; extend `_migrate()`. Tests for table creation.
2. **Refactor predictor orchestration:** extract source-agnostic `predict_catalogue_entry` + shared fan-out helper; keep "Mine" behaviour identical (regression tests green).
3. **Friend resolution + `_build_friend_predictions`:** lookup by cat#/handler; tests.
4. **Routes:** add/remove friend + `tab` param on schedule view.
5. **Data-state classifier:** `friend_data_state(trial, db)` + per-day availability; tests.
6. **Day-of collection (§6):** `friends/refresh` route + `my_day`-first non-destructive job (skip summary fallback); progress UX; re-resolve friends.
7. **UI:** tab bar in `schedule.html`; friends section; data-status line + refresh button; add/remove form; run-card `is_friend` flag (hide Adjust, show handler); multi-day day grouping.
8. **Empty/partial states + disambiguation.**
9. **Manual test + walkthrough artifacts.**
10. **(Fast-follow) HTMX autocomplete, conflict-aware crew view.**

---

## 12. Open questions

- Should **Full Day Schedule** become its own third tab, or stay stacked under **Mine**? (Recommend: third tab.)
- Should we also allow **add by dog name** in v1? (Recommend: yes, low cost, high memorability.)
- Do we want **conflict badges between your runs and friends'** in v1, or fast-follow? (Recommend: include — it's the headline value-add.)
- Cross-trial "followed handlers" — in scope later? (Needs a handler-identity strategy; defer.)

---

## 13. Touch list (files likely to change)

- `app/models.py` — new `SessionFriend`.
- `app/main.py` — `_migrate()` additive table create.
- `app/routers/schedule.py` — refactor predictor helper; `tab` param; friend prediction builder; `friend_data_state` (or a new `app/routers/friends.py`).
- `app/routers/friends.py` (or extend `trials.py`) — add/remove/refresh routes.
- `app/worker.py` — `my_day`-first non-destructive collection (flag on `refresh_trial_docs_job` or new `collect_friend_data_job`; skip summary fallback).
- `app/templates/schedule.html` — tab bar + friends section + data-status line + refresh button + multi-day day grouping.
- `app/templates/partials/run_card.html` — `is_friend` flag (hide Adjust, show `handler_name`).
- (Optional) `app/templates/partials/friends_*.html` — add form, search fragment, refresh progress.
- `tests/test_friends.py` (new) — resolution, fan-out parity, prediction parity, data-state classifier, refresh merge, multi-day, empty/partial states.
