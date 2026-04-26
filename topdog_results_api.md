# TopDog Events — APIs for Past Trial Results

Investigation of `https://www.topdogevents.com.au` (a Rails app, Devise auth, Bootstrap-Table on the front, Action Cable for live updates) focused on retrieving past trial results for a specific dog.

The site has **no per-dog history endpoint**. Results are organised by trial, not by dog. To get a dog's history you must (1) discover trials, (2) fetch each trial's results page, and (3) filter for that dog by name (public) or by registration number (authenticated only).

---

## TL;DR — recipe

**Public path (no login):**
1. `GET /results.json` — paginate over all trials with published results.
2. For each trial id, get its sub-trial ids from `GET /results/{event_id}` (parse the `<select id="trial_selection">` options).
3. For each sub-trial: `GET /results/{event_id}/trial/{sub_trial_id}` — server-rendered HTML with all classes/heights for that sub-trial.
4. Find rows whose dog cell text starts with the dog's display name (e.g. `Adalacia Comte Clement - Carolyne Fitzpatrick`).

Identifier available publicly: **dog display name** (`<dog_name> - <handler_name>`). No stable numeric dog ID is exposed.

**Authenticated path (Devise login):**
1. `GET /entries` (HTML) or `GET /entries.json` — user's own dogs and entries (this is what `app/scraper/auth.py` already uses).
2. `GET /dogs.json` — likely returns the user's dogs with their Dogs Australia registration numbers (returns `401` anonymously; not yet verified what the payload looks like).
3. Then either parse `/entries.json` for past entries, or follow the public path above for trials the dog has been in.

---

## Trial discovery

### `GET /results` (HTML index)
The public results index. Renders an empty table; rows are loaded via Bootstrap-Table from `/results` with `Accept: application/json`.

### `GET /results.json` (or `GET /results` with `Accept: application/json`)
Returns a JSON array of trials that have published results. Sorted by `start_date` descending.

Response item:
```json
{
  "id": 1501,
  "name": "Hawkesbury District Agricultural Association Agility Trial",
  "start_date": "2026-04-26",
  "club_name": "Hawkesbury District Agricultural Associaiation",
  "state": "New South Wales"
}
```

Query params (verified working):
- `discipline=<id>` — single id (Agility=1, Obedience=2, Trick Dog=3, Dances With Dogs=4, Scent Work=5, Tracking=6, Retrieving=7, Canine Hoopers=8, CASSA Scent Work=9, Mock Trial=10, Misc=11, SprintDog=12, Earthdog=13, Sled Sport=15, Lure Coursing=16, Track and Search=18). Repeat as `discipline[]=1&discipline[]=2` for multiple.
- `state=<code>` — `ACT|NSW|NT|QLD|SA|TAS|VIC|WA`. Repeat-array form supported.
- `limit=<n>` and `offset=<n>` — Bootstrap-Table pagination.

Query params that are *ignored* by the server (do not waste time on them): `search`, `sort`, `order`, `from`, `to`. Filtering by date or text has to be done client-side.

### `GET /trials/?f=past` (JSON when `Accept: application/json`)
A wider list — past trials by *closing date*, not by start date — including ones where results were never uploaded. Date strings are pre-formatted (`"Sat 25th Apr 2026"`), so `/results.json` is friendlier for parsing.

Response item:
```json
{
  "id": 1474,
  "start_date": "Sat 25th Apr 2026",
  "name": "GDCSA (SNXN, SWIN, SWCN, SWIE, SWCE) ",
  "club_name": "Gundog Club of S.A. Inc",
  "closing_date": "Thu 9th Apr 2026"
}
```

Same `discipline[]=` / `state[]=` filters. Use `f=upcoming` for the upcoming view (already handled by `app/scraper/auth.py` for the user's own trials via `/entries`).

---

## Trial detail / results pages

### `GET /trials/{trial_id}`
Public trial detail page. Already parsed by `app/scraper/trials.py` for venue, start date, schedule/catalogue document URLs. No results data here.

### `GET /trials/{trial_id}/catalogue/get`
xlsx download of the trial catalogue (this is the FINAL catalogue your `app/scraper/catalogue.py` parses). Public.

### `GET /trials/{trial_id}/schedule/get`
HTML schedule. Public.

### `GET /results/{event_id}`
The public, server-rendered "All Results" page for a trial. **Important quirk:** an "event" can contain several "sub-trials" (e.g. `Jumping 1`, `Jumping 2`, `Qualifying Heat 1`...). This URL renders only the **first** sub-trial. The other sub-trials are listed in:

```html
<select id="trial_selection" onchange="resultsGoToTrial(48, this.value)">
  <option selected="selected" value="203">Jumping 1</option>
  <option value="204">Jumping 2</option>
</select>
```

To get every result, parse those `<option value="...">` and follow:

### `GET /results/{event_id}/trial/{sub_trial_id}`
Same page, but for the chosen sub-trial. Use this to enumerate.

**HTML structure inside each sub-trial page:**

- One `<div class="card">` per class+discipline combination, e.g. `id="d_novice_agility"`, `id="d_excellent_jumping"`. Class options listed in `<select id="class_selection">` (e.g. `novice_agility`, `excellent_agility`, `masters_agility`, `novice_jumping`, ..., `open_agility`, `open_jumping`).
- Inside each class card, a `<table class="table table-xs">` with rows.
- Each height group starts with a header row:
  ```html
  <tr>
    <td class="bg-nfc" colspan="5">
      <strong>Novice Agility - 200</strong>
      Standard Course Time: 50 seconds.
      Course Length: 130m.
      Judge: Ms N Neethling (WA)
    </td>
  </tr>
  ```
  Use these to capture **height** (`200|300|400|500|600`), **SCT**, **course length**, **judge**.
- Following rows are entries for that height. Columns:

  | col 1 | col 2 | col 3 | col 4 (Time) | col 5 (Total Faults) |
  |---|---|---|---|---|
  | (blank — sometimes a placement marker for top finishers, often empty) | `Dog Name - Handler Name` | (blank) | `35.99` (or empty if DQ/absent) | `5` / `5.3` / blank |

  No "Place" column on the static results page. Live results have an extra `Faults` column and a status indicator. Disqualified runs render the time cell as `Disqualified` with `colspan=3`.

- **Stable identifiers exposed publicly:** none for the dog itself. The dog's "name" includes the handler, separated by ` - `. Cat# (catalogue number) is *not* in the static page (only on the live page); it is per-trial anyway, not per-dog.

### `GET /trials/{trial_id}/results/download.pdf`
PDF version of the same data. Returns `401` to anonymous requests in general, but the link is published on `/trials/{id}` for some trials and may be reachable when published. Prefer the HTML.

---

## Live results (real-time)

### `GET /trials/{trial_id}/results/live`
Public menu of "Qualifying Heat N → Ring R → Class C" links pointing at:

### `GET /trials/{trial_id}/results/live/trial/{sub_trial_id}/ring/{ring_id}/class/{class_id}/view`
Server-rendered table with **more columns than the static page**:

```
Cat# | Dog | Handler | Height | Time | Faults | Total Faults | (status)
```

Each `<tr id="cat_NNNN">` is the per-trial cat number. Disqualified rows show `Disqualified` (col-spanned across the three numeric columns).

### WebSocket: `wss://www.topdogevents.com.au/cable`
Rails Action Cable. The page opts in via `<meta name="action-cable-url">`. Live-result updates are presumably broadcast on a per-ring or per-class channel; the channel name is set in the page-specific JS (not in the global bundle) so identifying it requires loading the page in a browser and tapping into the WebSocket. Not needed for *past* results.

---

## Authenticated endpoints (probed via 401/redirect)

These all redirect to `/users/sign_in` when anonymous (HTML) or return `401 Unauthorized` (JSON):

| Path | Method | Purpose | Payload format |
|---|---|---|---|
| `/users/sign_in` | POST | Devise login. Form fields: `user[email]`, `user[password]`, `authenticity_token` (CSRF). | form-encoded |
| `/dashboard` | GET | User home. | HTML |
| `/entries` | GET | List of upcoming trials the user is entered in. Currently parsed by `app/scraper/auth.py`. | HTML |
| `/entries.json` | GET | JSON form of above. Schema not yet captured. | JSON |
| `/entries/{id}` | GET | Single entry detail. | HTML |
| `/entries/show?entry_id={id}` | GET | AJAX entry detail (used by JS bundle). | HTML/partial |
| `/entries/{entry_id}/get_details/` | GET | Per-entry details for editing. | JSON |
| `/entries/{trial_id}/select_items/{dog_id}` | GET | Items pickable for a dog at this trial. | HTML |
| `/entries/{trial_id}/enter_other_dog` | POST | Enter another dog. | form |
| `/dogs` | GET | User's dogs (HTML). | HTML |
| `/dogs.json` | GET | User's dogs (JSON) — likely the cleanest source of `{id, name, registration_number}` for the user's dogs. | JSON |
| `/dogs/new` | GET | Add-a-dog form. | HTML |
| `/users/edit` | GET | Profile edit. | HTML |
| `/trials/{id}/results` | GET | Authenticated results view. | HTML |
| `/trials/{id}/results.json` | GET | Authenticated results JSON. **This is the most promising endpoint to test once authenticated** — if it exists for the trial it will be far easier to parse than the HTML and likely includes structured per-entry fields. | JSON |
| `/trials/{id}/results/download.pdf` | GET | PDF download. | PDF |
| `/trials/{id}/results/download.json` | GET | JSON download. | JSON |
| `/trials/{trial_id}/manual/search_for_dog?registration_number={n}&user_id={n}` | GET | Trial-secretary "find a dog by ANKC registration" lookup. Probably returns the dog record. | HTML/JSON |
| `/trials/{trial_id}/manual/search_for_user?...` | GET | Trial-secretary user search. | HTML/JSON |
| `/trials/{trial_id}/has_unpaid_entries` | GET | Boolean check. | JSON |
| `/clubs/{club_id}/...` | GET | Club admin views. | HTML |
| `/payments/{id}` | GET | Payment record. | HTML |

CSRF: `<meta name="csrf-token" content="...">` in every page. Required on non-GET requests as `X-CSRF-Token` header (or `authenticity_token` form field). The existing scraper sidesteps this by driving Playwright instead of issuing raw POSTs.

---

## Known taxonomy / encodings

- **Discipline ids** (from `/results` `<select name="discipline">`): `1=Agility, 2=Obedience, 3=Trick Dog, 4=Dances With Dogs, 5=Scent Work, 6=Tracking, 7=Retrieving, 8=Canine Hoopers, 9=CASSA Scent Work, 10=Retrieving Mock Trial, 11=Miscellaneous, 12=SprintDog, 13=Earthdog, 15=Sled Sport, 16=Lure Coursing, 18=Track and Search`. (No 14, no 17 in the dropdown.)
- **Class slugs** (from `<select id="class_selection">` and `<div id="d_...">`): `novice_agility, excellent_agility, masters_agility, novice_jumping, excellent_jumping, masters_jumping, open_agility, open_jumping`. Class numeric ids exist server-side (`class/1` … `class/17` seen in live URLs); the mapping appears to be `1=Novice Agility, 2=Excellent Agility, 3=Masters Agility, 4=Novice Jumping, 5=Excellent Jumping, 6=Masters Jumping, 16=Open Agility, 17=Open Jumping`.
- **Heights**: `200, 300, 400, 500, 600` mm. Already handled by `app/scraper/auth.HEIGHT_RE`.
- **NFC** (Not For Competition) entries have `NFC` appended to cat#, e.g. `1003NFC`. Already handled.
- **Trial vs sub-trial**: `event_id` (e.g. 256) is the catalogue/trial container; `sub_trial_id` (e.g. 487) is the actual day/heat. `/trials/{event_id}` and `/results/{event_id}` use the event id; live results URLs and the `<select id="trial_selection">` use the sub_trial_id.

---

## Identification challenge — how to match a dog across trials

The public results HTML gives only `<Dog Name> - <Handler Name>` as a free-text cell. To do this reliably:

1. **By dog name + handler name string.** Cheap. Will break when handlers change, when a dog has duplicate naming with another (rare for ANKC registered names — they are unique by design), or on minor punctuation/whitespace differences. Use a normaliser (lowercase, collapse whitespace, strip parenthetical suffixes like `(AI)`).
2. **By Dogs Australia registration number.** Stable, but only available behind login (`/dogs.json`, FINAL catalogue xlsx, or trial secretary lookup). The catalogue files your app already downloads (`/trials/{id}/catalogue/get`) typically *do* contain registration numbers — those would be a good source for backfilling a `dog.registration_number` field on the user's dogs.
3. **By cat#.** Only useful within a single trial — cat numbers are reassigned every event.

If you want public-only history lookup: build a normalised `(dog_name_normalised, handler_normalised)` key from the user's known entries, then match against the row text in each trial's results page.

---

## Suggested implementation in this repo

Add `app/scraper/results.py` modelled on `app/scraper/trials.py`:

```python
async def list_result_trials(
    discipline: int | None = None,
    state: str | None = None,
    page_size: int = 200,
) -> list[dict]:
    """Paginate /results.json and return [{id, name, start_date, club_name, state}]."""

async def fetch_trial_results(event_id: int) -> dict:
    """Return {event_id, sub_trials: [{sub_trial_id, name, classes: [{class_slug, height_groups: [{height, sct, length, judge, runs: [{dog_name, handler, time, total_faults, status}]}]}]}]}"""

async def find_dog_history(dog_name: str, handler_name: str | None = None,
                           since: date | None = None) -> list[dict]:
    """Walk past trials and return all rows matching this dog."""
```

For pagination of `/results.json`, ask for a large `limit` (e.g. 1000) and check whether the returned count equals `limit`; advance `offset` accordingly. The endpoint returns a plain array — no `total` field — so you stop when a page comes back short.

Re-use the existing `httpx`/Playwright bones; nothing on the public results path needs Playwright (cookies are not required), so plain `httpx.AsyncClient` is enough and far faster than the Playwright pattern in `app/scraper/auth.py`.

---

## Open questions worth probing later (need an authed session)

1. Exact JSON shape of `/dogs.json`, `/entries.json`, `/trials/{id}/results.json`, and `/trials/{id}/results/download.json`. If any of these include structured per-run data (height, time, faults, status, class id), the entire HTML-parsing layer becomes optional.
2. Whether `/dogs.json` exposes the Dogs Australia registration number (almost certainly yes, given how the manual `search_for_dog` endpoint takes one as input).
3. Whether `/trials/{trial_id}/manual/search_for_dog?registration_number=N` is gated to trial-secretary roles only or available to any signed-in user.
4. Action Cable channel name(s) used by `/results/live/.../view` for streaming — only matters if you want live updates rather than past results.

Sources:
- [Top Dog Events results index](https://www.topdogevents.com.au/results)
- [Trial 256 results (2023 ANKC Agility Nationals)](https://www.topdogevents.com.au/results/256)
- [Live results menu for trial 256](https://www.topdogevents.com.au/trials/256/results/live)
- [Help: Why can't I find my dog in the system?](https://www.topdogevents.com.au/help/why-can-t-i-find-my-dog-in-the-system)
- [Past trials JSON](https://www.topdogevents.com.au/trials/?f=past)
