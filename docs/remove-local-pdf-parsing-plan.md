# Plan: Remove Local PDF Parsing and Standardize on OpenRouter

## Goal

Remove all **local PDF parsing logic** from the catalogue pipeline and make the OpenRouter workflow the only supported PDF extraction method, while preserving prediction behavior and minimizing operational risk.

This plan also evaluates whether `playwright` can be removed and defines a test strategy using the local anonymized fixture:

- `Final_Catalogue_upload_v2_ANONYMISED.pdf`

## Scope and assumptions

- In scope:
  - Catalogue PDF parsing (`app/scraper/catalogue.py`) migration to OpenRouter-only.
  - Dependency cleanup related to local catalogue PDF parsing.
  - Investigation and recommendation for removing `playwright`.
  - Regression validation plan with local anonymized PDF fixture.
- Out of scope (for this phase):
  - Re-architecting unrelated scraping features unless needed to remove dependencies.
  - Changing prediction engine contracts.

## Current-state analysis

### Where local PDF parsing exists today

1. Catalogue parser (local/regex + `pdfplumber`)
   - `parse_catalogue_pdf()` and related regex/state-machine logic in `app/scraper/catalogue.py`.
   - `parse_catalogue_pdf_bytes()` currently attempts OpenRouter first, then falls back to local parser.

2. Schedule parser (local `pdfplumber`)
   - `parse_schedule_pdf()` in `app/scraper/schedule.py`.
   - This is independent from OpenRouter catalogue extraction and still uses local PDF text extraction.

3. Dependencies
   - `pdfplumber` in `requirements.txt`.
   - `pypdf` in `requirements.txt` (currently used by OpenRouter chunking in `app/scraper/openrouter_catalogue.py`).
   - `playwright` in `requirements.txt`, plus browser install in `Dockerfile`.

4. Tests anchored on local parser behavior
   - `tests/test_catalogue_pdf_parser.py`
   - `tests/test_catalogue_pdf_real_fixture.py`
   - Existing OpenRouter tests still include fallback assertions (`tests/test_openrouter_catalogue.py`).

### Key implication

Removing local catalogue PDF parsing is straightforward.  
Removing **all PDF-related local dependencies** is a separate concern because schedule parsing still uses `pdfplumber`, and OpenRouter chunking still uses `pypdf`.

## Recommendations (high-level)

1. Implement a two-step dependency strategy:
   - Step A: Remove local catalogue parser + fallback.
   - Step B: Decide schedule PDF strategy (drop PDF schedule support vs OpenRouter schedule extraction) before removing `pdfplumber`.

2. Keep `pypdf` unless OpenRouter chunking/resiliency logic is redesigned to avoid local PDF splitting.

3. Do **not** remove `playwright` in the same change as catalogue parser removal.  
   Treat Playwright removal as a separate stream with explicit authentication migration work.

## Detailed execution plan

## Phase 1 — Catalogue: OpenRouter-only cutover

### Code changes

1. `app/scraper/catalogue.py`
   - Remove `parse_catalogue_pdf()` and all helper functions used only by that parser:
     - `_parse_pdf_pages`
     - `_extract_pdf_lines`
     - `_split_dog_handler`
     - `_split_dog_handler_nationals`
     - related regex constants/class-code maps used only for local parsing.
   - Update `parse_catalogue_pdf_bytes()`:
     - Require OpenRouter path only.
     - Remove legacy fallback behavior.
     - Raise explicit parse/config error when OpenRouter is disabled/misconfigured.
   - Keep xlsx and HTML entries flows unchanged.

2. `app/worker.py`
   - Ensure upload/refresh flows handle OpenRouter parse failures with user-safe error behavior (already mostly present, but remove references to fallback semantics in logs/comments).

3. User-facing text updates
   - `app/templates/trial_detail.html`:
     - Update upload error/help text to reflect OpenRouter-only PDF handling.
   - Any docs/comments that still describe local PDF fallback.

4. Test suite updates
   - Remove local parser tests:
     - `tests/test_catalogue_pdf_parser.py`
     - `tests/test_catalogue_pdf_real_fixture.py` (or replace with OpenRouter contract tests; see Test Plan section).
   - Update `tests/test_openrouter_catalogue.py`:
     - remove fallback assertions.
     - add explicit assertions that failure surfaces as OpenRouter/config errors.

### Dependency updates for Phase 1

- Do **not** remove `pdfplumber` yet if `parse_schedule_pdf()` remains.
- Keep `pypdf` (required by `openrouter_catalogue.py` chunking).

## Phase 2 — Remove remaining local PDF parsing (`pdfplumber`)

This phase is required if the target is truly "no local PDF parsing in the app."

### Decision point

Choose one:

1. **Option 2A (preferred for reliability):** Replace schedule PDF parsing with OpenRouter schedule extraction.
   - Add `openrouter_schedule.py`.
   - Normalize to existing `ClassSchedule` schema.
   - Keep schedule HTML path unchanged.

2. **Option 2B (simpler, higher risk):** Drop schedule PDF support entirely.
   - If schedule document is PDF, fail with clear message and rely on `/my_day` or schedule HTML only.

### After decision implementation

- Remove `parse_schedule_pdf()` and local PDF code from `app/scraper/schedule.py` if no longer needed.
- Remove `pdfplumber` from `requirements.txt`.

## Phase 3 — Playwright removal investigation and recommendation

## Can Playwright be removed today?

Short answer: **not safely in the same change**.

Why:

- `app/scraper/auth.py` currently depends on Playwright for login/session cookie acquisition and `/entries` scraping.
- `app/scraper/trials.py` still contains Playwright code paths (`scrape_trial_detail`, `scrape_trial_details_batch`) used by `sync_session_job`.
- `/my_day` and schedule fetches depend on authenticated cookies currently obtained through Playwright login flow.

## Recommended path to remove Playwright

1. Build an HTTP-only auth client for TopDog (Devise flow):
   - GET sign-in page, extract CSRF token.
   - POST credentials and persist cookie jar.
2. Move `/entries` to JSON/HTML parsing via `httpx` only.
3. Replace any remaining Playwright trial-detail calls with existing `fetch_trial_detail()` HTTP path.
4. Add auth/session integration tests with mocked responses.
5. Then remove:
   - `playwright` from `requirements.txt`
   - `playwright install --with-deps chromium` from `Dockerfile`
   - Playwright imports and dead code.

This should be a dedicated follow-up to keep risk isolated.

## Test plan (validation + verification)

## Success criteria

1. Catalogue PDF parsing works only through OpenRouter.
2. Prediction outputs continue to use unchanged `CatalogueEntry` contract.
3. No feature regressions in refresh/upload and schedule generation flows.
4. Dependency removals do not break worker/web startup.

## Fixture-backed regression strategy (required)

Use `Final_Catalogue_upload_v2_ANONYMISED.pdf` as the canonical regression document.

### Baseline capture (pre-removal)

Before deleting legacy parser, generate a baseline JSON artifact from the local fixture that records:

- entry count
- distinct days
- distinct events count
- per-height distribution
- per (day,event,height,ring) run-position continuity and `height_group_total` integrity
- sample key set `(day,event_name,height_group,cat_number,ring_number)`

Expected baseline values from existing tests:

- total entries: 900
- days: {1, 2}
- heights: {200, 300, 400, 500, 600}
- representative events include:
  - `Novice Agility (AD1)`
  - `Masters Jumping (JDM1)`
  - `Open Agility`
  - `Open Jumping`

Store this as a test fixture (e.g. JSON under `tests/fixtures/`) to compare OpenRouter output quality after local parser removal.

### Automated tests to add/update

1. OpenRouter config/error behavior
   - PDF parse fails fast when OpenRouter disabled/misconfigured (no fallback).

2. OpenRouter normalization invariants
   - run positions contiguous per grouping key.
   - `height_group_total` equals non-NFC count.
   - valid heights only.
   - duplicate identity rows deduped.

3. Fixture-based extraction parity test (integration-style, gated)
   - If `OPENROUTER_API_KEY` is present:
     - run extraction on `Final_Catalogue_upload_v2_ANONYMISED.pdf`.
     - compare against stored baseline tolerances:
       - exact invariants must pass.
       - count/keys within agreed threshold.
   - If key absent:
     - skip with explicit message (do not fail CI by default).

4. Worker path regression
   - `upload_catalogue_job` and `refresh_trial_docs_job` should handle OpenRouter failures gracefully (no stale links, no partial DB corruption).

### Command-level verification matrix

1. Targeted unit tests:
   - OpenRouter module tests.
   - Worker catalogue upload/refresh tests.
2. Fixture parity test:
   - run only when OpenRouter credentials are configured.
3. Full syntax sanity:
   - project py_compile check used by this repo.

## Rollout plan

1. Land Phase 1 behind existing OpenRouter env controls but remove fallback code path.
2. Run fixture-based parity in staging with real OpenRouter credentials.
3. Monitor worker logs for extraction failures/timeouts.
4. Decide and execute Phase 2 (schedule PDF strategy) to enable `pdfplumber` removal.
5. Execute Phase 3 for Playwright removal as independent effort.

## Risks and mitigations

1. OpenRouter outage or rate/cost spikes
   - Add retry/circuit-breaker and clear failure status to UI.
   - Cache extraction by PDF hash to reduce repeat calls.

2. Quality drift vs legacy parser
   - Keep fixture-baseline comparison for ongoing validation.
   - Add alerting for high invalid-entry rates.

3. Schedule parsing regression when dropping `pdfplumber`
   - Do not remove until schedule strategy is implemented and tested.

4. Auth breakage if Playwright removed prematurely
   - Keep Playwright until HTTP auth flow is verified end-to-end.

## Additional ideas

1. PDF hash cache table
   - Cache normalized OpenRouter output by hash + model + engine.
   - Reduce latency/cost for repeated uploads/refreshes.

2. Extraction quality telemetry
   - Persist normalized count, failure_count, duration, and chunk count.
   - Use thresholds to detect regressions automatically.

3. Gradual strictness mode
   - First pass: warning on low-confidence extraction.
   - Second pass: hard fail for invalid extraction once confidence is proven.

4. Operational guardrails
   - Per-trial extraction timeout budget.
   - Queue-level retry policy tuned for OpenRouter transient failures.
