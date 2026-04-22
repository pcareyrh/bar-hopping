# bar-hopping: Implementation Plan

## Context

Build a mobile-friendly Docker-deployed web app for dog agility competitors attending NSW Agility trials managed by [topdogevents.com.au](https://www.topdogevents.com.au/trials). Users provide their TopDog login and the app authenticates as them to discover all their dogs' entries across upcoming trials. For each event the app generates a personalised time-predicted schedule that accounts for ring setup, course walk, and running order. Multiple independent users share the same deployment via UUID-based shareable session links.

---

## Stack

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Async, fast, great for scraping + API |
| Scraping | Playwright + BeautifulSoup4 | Playwright for authenticated TopDog sessions and JS-rendered pages; BS4 for static detail pages |
| Catalogue parsing | openpyxl | Parses the FINAL catalogue `.xlsx` file downloaded from TopDog |
| Schedule parsing | pdfplumber + BeautifulSoup4 | Handles schedule as PDF or HTML depending on trial |
| Encryption | cryptography (Fernet) | Encrypt stored TopDog credentials at rest |
| Database | SQLAlchemy + SQLite | Single-file DB, mounted as Docker volume |
| Frontend | Jinja2 templates + HTMX + Tailwind CSS (CDN) | No build step, mobile-first, inline editing without a JS framework |
| Container | Single Dockerfile + docker-compose.yml | Simple to deploy; volume for DB persistence |

---

## Project Structure

```
bar-hopping/
├── app/
│   ├── main.py                 # FastAPI app + route mounting
│   ├── database.py             # SQLAlchemy engine + session
│   ├── models.py               # ORM models
│   ├── crypto.py               # Fernet encrypt/decrypt helpers
│   ├── scraper/
│   │   ├── auth.py             # Playwright: log in to TopDog, fetch user's entries
│   │   ├── trials.py           # Playwright: scrape trials list (NSW, Agility filter)
│   │   ├── schedule.py         # Parse schedule doc (PDF or HTML)
│   │   └── catalogue.py        # Parse catalogue doc (PDF or HTML)
│   ├── engine/
│   │   └── predictor.py        # Time prediction logic
│   ├── routers/
│   │   ├── sessions.py         # POST /sessions, GET/PUT /s/{uuid}/settings
│   │   ├── trials.py           # GET /s/{uuid}/trials, /s/{uuid}/trials/{id}
│   │   └── schedule.py         # GET /s/{uuid}/trials/{id}/schedule + inline edits
│   └── templates/
│       ├── base.html           # Mobile layout, Tailwind, HTMX
│       ├── index.html          # Home — create session or resume via link
│       ├── setup.html          # TopDog login + sync entries
│       ├── trials.html         # NSW Agility trials list with user's entries highlighted
│       ├── trial_detail.html   # Trial overview: rings, events, user's dogs
│       ├── schedule.html       # Personalised predicted schedule (day view)
│       └── partials/           # HTMX fragments for inline edit responses
├── data/                       # SQLite DB (Docker volume mount)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Data Models (`app/models.py`)

```
Session
  uuid                  PK (UUID4)
  created_at
  topdog_email          encrypted (Fernet)
  topdog_password       encrypted (Fernet)
  topdog_synced_at      nullable — last time entries were fetched from TopDog
  avg_time_per_dog      int (seconds, default 90) — global default
  default_setup_mins    int (default 10) — ring setup time default
  default_walk_mins     int (default 10) — course walk time default

Trial
  id                    PK
  external_id           unique (from site URL, e.g. "1307")
  name
  start_date / end_date
  venue, state
  schedule_doc_url      nullable
  catalogue_doc_url     nullable
  scraped_at            — re-scrape if > 4 h old

CatalogueEntry                  — one row per dog per event, parsed from FINAL .xlsx
  id
  trial_id              FK Trial
  event_name            full name as in xlsx (e.g. "Agility Trial - Novice Agility (AD)")
  cat_number            string, e.g. "405" or "521NFC"
  height_group          int (200, 300, 400, 500, 600) — derived from cat_number prefix
  run_position          int — 1-based row order within height group for this event (IS the running order)
  height_group_total    int — total entries (including NFC) in this height group for this event
  nfc                   bool — True if cat_number ends with "NFC"
  dog_name              nullable (present in real catalogues, blank in some)
  handler_name          nullable

ClassSchedule                   — from parsed schedule
  id
  trial_id              FK Trial
  ring_number
  class_name
  scheduled_start       time of day
  ring_setup_mins       int (default from Session; user can override per event)
  walk_mins             int (default from Session; user can override per event)

SessionEntry                    — user's runs, populated from TopDog sync
  session_uuid          FK Session
  trial_id              FK Trial
  dog_name              as shown on TopDog
  height_group          int (200/300/400/500/600)
  event_name            matches CatalogueEntry.event_name
  cat_number            string — the user's catalogue number (e.g. "405")
  catalogue_entry_id    nullable FK CatalogueEntry — resolved once FINAL xlsx is parsed
  ring_number           nullable — resolved from schedule
  position_override     nullable int — user can adjust if queue has changed on the day
  time_per_dog_override nullable int (seconds)
```

---

## TopDog Authentication & Entry Sync (`scraper/auth.py`)

The TopDog entries page for each trial exposes exactly what we need per entry:
- **Dog name**
- **Dog height** (jump height group, e.g. 200 mm)
- **Event class** (e.g. "Masters Agility", "Novice Jumping")
- **Sequence number** — the dog's running order position within their height group in that class

This sequence number is used directly as the default position in the prediction formula. No catalogue parsing is required to determine the user's place in the queue.

**Sync process:**

1. User provides their topdogevents.com.au email + password on the setup page.
2. Credentials are encrypted with Fernet before being saved to the Session row.
3. On sync, Playwright:
   a. Navigates to `/users/sign_in` and submits the form.
   b. For each upcoming NSW Agility trial in our scraped list, navigates to that trial's entries page.
   c. Extracts the authenticated user's entries: dog name, height, class, sequence number, ring (if shown).
4. Each entry is saved as a `SessionEntry` with `sequence_number` populated directly from the site.
5. If ring/start-time data is not yet published, the entry is stored as unresolved with a "pending schedule" status.
6. Re-sync button available at any time; auto-re-syncs if `topdog_synced_at` > 1 hour old.

**FINAL Catalogue (xlsx):** Once a trial closes, TopDog publishes the FINAL catalogue as a `.xlsx` download. This file is the authoritative source for:
- The actual running order (row position within height group per event — NOT numerical Cat# order)
- Total entries per height group per event
- NFC entries (marked with "NFC" suffix on Cat#)

The Cat# from TopDog entries is matched against `CatalogueEntry.cat_number` to resolve `SessionEntry.catalogue_entry_id`. The `run_position` from the catalogue is the default position; the user can override it via `position_override` if the running order changes on the day.

**Credential security**:
- Fernet key loaded from `ENCRYPTION_KEY` env var (generated at deploy time, never committed).
- Credentials never appear in logs, API responses, or template output.
- Credentials used only server-side via Playwright; never forwarded to the browser.

---

## Scraping & Parsing Strategy

1. **Trials list** (`scraper/trials.py`): Playwright loads `/trials`, applies Discipline=Agility + State=NSW filters, extracts trial IDs and names.
2. **Trial detail** (`scraper/trials.py`): BeautifulSoup on `/trials/{id}` — venue, dates, and links to schedule and catalogue documents.
3. **FINAL Catalogue** (`scraper/catalogue.py`) — downloaded once per trial after entries close:
   - Download the `.xlsx` file; parse with `openpyxl` (no third-party install required beyond openpyxl)
   - Detect event sections: any row where column A contains `"Agility Trial"` is a new event header
   - Within each event, track current height group (changes when Cat# prefix changes)
   - Record each dog's `run_position` = 1-based row index within their height group section
   - Set `nfc = True` if Cat# ends with "NFC"; still record position but flag it
   - Compute `height_group_total` = count of non-NFC entries per height group per event
   - Store all as `CatalogueEntry` rows; link to `SessionEntry` by matching `cat_number`
4. **Schedule parser** (`scraper/schedule.py`) — ring assignments and start times:
   - Schedule may be PDF (parse with pdfplumber) or HTML (BeautifulSoup)
   - Extract: ring number, event name (must match `CatalogueEntry.event_name`), scheduled start time
   - Store as `ClassSchedule`; set default `ring_setup_mins` and `walk_mins` from session defaults
5. **Cache**: Skip re-scrape if `Trial.scraped_at` < 4 hours ago. Manual refresh button on trial page.

---

## Time Prediction Algorithm (`engine/predictor.py`)

For each `SessionEntry` with a resolved `catalogue_entry_id` and linked `ClassSchedule`:

```
1. Look up CatalogueEntry → run_position, height_group_total, nfc
   Look up ClassSchedule(trial_id, ring_number, event_name)
       → scheduled_start, ring_setup_mins, walk_mins

2. If nfc=True: include in position count and time calculations; display "NFC" badge but show predicted time

3. effective_position = position_override ?? catalogue_entry.run_position
   effective_tpd      = time_per_dog_override ?? session.avg_time_per_dog

4. first_run_start    = scheduled_start + ring_setup_mins + walk_mins
   predicted_start    = first_run_start + (effective_position - 1) × effective_tpd

5. Return: dog_name, event_name, ring_number, height_group,
           predicted_start, effective_position, height_group_total
           → displayed as "Run 4 of 12 in 400mm group"
```

**Pre-catalogue state** (trial not yet closed): `catalogue_entry_id` is null; show the user's entry with event/height but no predicted time. Display "Catalogue pending — check back after entries close."

---

## User Session Flow

```
/                          → home: "Start Planning" (new session) or resume via link
POST /sessions             → create session (UUID), redirect to /s/{uuid}/setup

/s/{uuid}/setup            → enter TopDog email + password; "Sync My Entries" button
POST /s/{uuid}/sync        → authenticate + fetch entries; redirect to /s/{uuid}/trials

/s/{uuid}/trials           → list of upcoming NSW Agility trials
                             user's trials highlighted / pinned at top

/s/{uuid}/trials/{id}      → trial detail:
                             - rings and event list
                             - user's dogs + their entries for this trial
                             - "pending catalogue" badge if not yet published

/s/{uuid}/trials/{id}/schedule → predicted schedule (day view):
                             - cards per run: dog, class, ring, predicted time, position
                             - inline edit: position override, time-per-dog, setup mins, walk mins
                             - HTMX: edits update only the affected card + summary

/s/{uuid}/settings         → adjust global defaults: avg time per dog, setup mins, walk mins
```

Shareable link is `/s/{uuid}` — bookmarkable, works on any device.

---

## Frontend (Mobile-first)

- **Tailwind CSS** via CDN — no build step, responsive, phone-optimised.
- **HTMX** — inline edits update only the affected card (no full reload).
- Key UI states:
  - **Setup**: clean login form with clear note that credentials are stored encrypted.
  - **Trials list**: cards with date, venue, entry count badge for user's dogs.
  - **Trial detail**: grouped by ring; each event shows user's dog(s) with "pending" or run number.
  - **Schedule view**: timeline-style cards sorted by predicted time. Colour-coded by proximity. Editable fields collapse/expand inline.
  - **Settings panel**: sliders or number inputs for global timing defaults.

---

## Docker

**Dockerfile** — single stage:
- `python:3.12-slim` base
- Install Playwright + Chromium (`playwright install --with-deps chromium`)
- Install Python deps from `requirements.txt`
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`

**docker-compose.yml**:
```yaml
services:
  web:
    build: .
    ports: ["8000:8000"]
    volumes:
      - ./data:/app/data
    environment:
      - DATABASE_URL=sqlite:////app/data/barhopping.db
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}   # generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

A `.env` file (gitignored) holds `ENCRYPTION_KEY`.

---

## Known Challenges

| Challenge | Mitigation |
|---|---|
| TopDog "My Entries" page structure unknown | Inspect with Playwright during dev; adapt selectors |
| xlsx row order ≠ Cat# order | Running order = row index within height group section, not numeric Cat# sort |
| NFC entries run the event but results don't count | Flagged in `CatalogueEntry.nfc`; included in position count and time calculations like any other competitor |
| Catalogue not yet published pre-close | `catalogue_entry_id` null; show "Catalogue pending" state; resolves on next refresh |
| Ring number absent from xlsx | Comes from schedule document; must be matched to `event_name` |
| Schedule document format varies (PDF / HTML) | pdfplumber for PDF, BS4 for HTML; detect by Content-Type |
| Multi-day trials use a single catalogue file | One xlsx covers all days; event sections within it cover all rings and days for the trial |
| Multiple dogs with overlapping ring times | Schedule view sorts all entries by predicted time; flag simultaneous conflicts |
| Ring setup / walk time vary by club and class | Per-event overrides on schedule page; session-level defaults as starting point |
| Daylight saving / timezone | Store all times as `datetime` in AEST (UTC+10) |

---

## Verification Checklist

- [ ] `docker compose up` starts cleanly; app reachable at `http://localhost:8000`
- [ ] Visiting `/` and clicking "Start Planning" creates a new session UUID
- [ ] Entering TopDog credentials and syncing fetches user's dogs and entries
- [ ] Credentials are stored encrypted; plaintext is never visible in DB or logs
- [ ] Trials list shows only NSW Agility events; user's trials pinned/highlighted
- [ ] Trial detail shows user's dogs with event name, cat#, height group
- [ ] Parsing the example xlsx creates correct `CatalogueEntry` rows: `run_position` matches row order, not Cat# numeric order
- [ ] Cat# "521NFC" is flagged `nfc=True` and shown as non-competing
- [ ] Schedule page shows predicted times: `scheduled_start + ring_setup + walk + (position-1) × tpd`
- [ ] "Run 4 of 12 in 400mm group" display matches xlsx data
- [ ] Pre-catalogue state shows "Catalogue pending" gracefully when xlsx not yet available
- [ ] Editing position override or timing values updates prediction inline via HTMX
- [ ] Sharing `/s/{uuid}` URL in a new browser restores full session state
- [ ] Docker volume persists data across container restarts
- [ ] Multi-dog scenario: two dogs in different events show correct independent predictions
