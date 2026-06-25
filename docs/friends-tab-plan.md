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

`handler_name` is **only** populated by the **full catalogue / `my_day`** sources. The user's own `/entries` sync alone does **not** carry handler names, and some fallback sources (HTML `/entries` summary with sentinel cat#s) have **no individual rows at all**. Therefore:

- Friends lookup is only meaningful once a real catalogue or `my_day` fetch has happened for the trial.
- If only the summary fallback exists, Friends should show an explicit "catalogue not available yet" empty state rather than failing silently.

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
3. **Empty states:** (a) no friends added yet → prompt; (b) catalogue not available for the trial yet → "Friend lookup needs the trial catalogue, which isn't available yet."

**Run card reuse**

- Reuse `partials/run_card.html` with a new flag (e.g. `p.is_friend`) that:
  - **Hides the "Adjust" form** (friends are read-only in v1).
  - **Shows the handler name** (currently never displayed). Add `handler_name` to the prediction dict and render it under the dog name when `p.is_friend` (or always, behind a conditional).
- Use a distinct accent (e.g. neutral/blue border) vs the brand-green "You" styling so friend cards are visually separable.

---

## 5. Edge cases & risks

| Case | Handling |
|---|---|
| **Catalogue not yet fetched** | `handler_name` absent → show explicit empty state; offer a "refresh catalogue" hint. Lookup by cat# may still work for single dog if rows exist. |
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

## 6. Feedback & ideas (beyond the core ask)

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
10. **Empty-catalogue UX.** Tie the Friends empty state to a "Sync/refresh catalogue" affordance so users understand the dependency.

---

## 7. Testing plan

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

**Manual (GUI via computerUse + screen recording):**

- Seed a session + a trial with catalogue data (or use an existing fixture path), open the schedule page, switch to **Friends**, add a friend by **CAT#** and by **Handler Name**, confirm multiple dogs render with predicted times, remove a friend. Capture a demo video + before/after screenshots for the walkthrough.

**Lint/syntax:** `.venv/bin/python -m py_compile $(find app scripts migrations -name '*.py')` and `.venv/bin/python -m pytest -q`.

---

## 8. Phased implementation

1. **Model + migration:** add `SessionFriend`; extend `_migrate()`. Tests for table creation.
2. **Refactor predictor orchestration:** extract source-agnostic `predict_catalogue_entry` + shared fan-out helper; keep "Mine" behaviour identical (regression tests green).
3. **Friend resolution + `_build_friend_predictions`:** lookup by cat#/handler; tests.
4. **Routes:** add/remove friend + `tab` param on schedule view.
5. **UI:** tab bar in `schedule.html`; friends section; add/remove form; run-card `is_friend` flag (hide Adjust, show handler).
6. **Empty states + disambiguation.**
7. **Manual test + walkthrough artifacts.**
8. **(Fast-follow) HTMX autocomplete, conflict-aware crew view.**

---

## 9. Open questions

- Should **Full Day Schedule** become its own third tab, or stay stacked under **Mine**? (Recommend: third tab.)
- Should we also allow **add by dog name** in v1? (Recommend: yes, low cost, high memorability.)
- Do we want **conflict badges between your runs and friends'** in v1, or fast-follow? (Recommend: include — it's the headline value-add.)
- Cross-trial "followed handlers" — in scope later? (Needs a handler-identity strategy; defer.)

---

## 10. Touch list (files likely to change)

- `app/models.py` — new `SessionFriend`.
- `app/main.py` — `_migrate()` additive table create.
- `app/routers/schedule.py` — refactor predictor helper; `tab` param; friend routes (or a new `app/routers/friends.py`).
- `app/templates/schedule.html` — tab bar + friends section.
- `app/templates/partials/run_card.html` — `is_friend` flag (hide Adjust, show `handler_name`).
- (Optional) `app/templates/partials/friends_*.html` — add form + search fragment.
- `tests/test_friends.py` (new) — resolution, fan-out parity, prediction parity, empty states.
