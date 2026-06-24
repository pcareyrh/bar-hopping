# Remove Fallback Local PDF Processing Plan

## Goal

Remove the legacy local PDF catalogue parser and its fallback behavior so catalogue PDFs are handled by the OpenRouter extraction path only. Remove PDF-related dependencies only after replacing or deleting every remaining local PDF code path that still needs them.

## Current PDF Processing Map

| Area | Current behavior | Local PDF dependency |
|---|---|---|
| Catalogue PDF fallback | `app/scraper/catalogue.py` calls `parse_catalogue_pdf()` when OpenRouter is disabled or raises during `parse_catalogue_pdf_bytes()` | `pdfplumber` |
| Catalogue PDF OpenRouter path | `app/scraper/openrouter_catalogue.py` sends PDFs to OpenRouter, but locally splits large PDFs and retries malformed chunks | `pypdf` |
| Schedule PDF parsing | `app/scraper/schedule.py` parses schedule PDFs locally before reusing the text parser | `pdfplumber` |
| Catalogue XLSX parsing | `app/scraper/catalogue.py` parses `.xlsx` catalogues with `openpyxl` | none |
| Schedule HTML parsing | `app/scraper/schedule.py` parses schedule HTML with BeautifulSoup | none |

## Dependency Audit

| Dependency | Explicit declaration | Runtime use found | Safe to remove with only catalogue fallback removal? | Required action before removal |
|---|---|---|---|---|
| `pdfplumber==0.11.4` | `requirements.txt` | `parse_catalogue_pdf()` and `parse_schedule_pdf()` | No | Remove/rewrite schedule PDF support as well as the catalogue fallback. |
| `pypdf==6.14.0` | `requirements.txt` | OpenRouter PDF chunking and chunk retry splitting in `openrouter_catalogue.py`; test PDF generation in `tests/test_openrouter_catalogue.py` | No | Replace OpenRouter chunking/retry splitting with a no-local-PDF strategy, or keep `pypdf`. |
| `pdfminer.six`, `Pillow`, `pypdfium2` | Transitive via `pdfplumber` | No direct imports found | Yes, after `pdfplumber` is removed | No direct requirement changes needed; they disappear when `pdfplumber` is uninstalled. |
| `openpyxl` | `requirements.txt` | Catalogue XLSX parser | No | Keep. It is unrelated to PDF fallback. |
| `beautifulsoup4` | `requirements.txt` | Schedule HTML, trial/catalogue HTML parsing | No | Keep. It is unrelated to PDF fallback. |
| `httpx` | `requirements.txt` | TopDog downloads and OpenRouter API calls | No | Keep. It is unrelated to PDF fallback. |

## Removal Scope

### In scope

- Remove the legacy `pdfplumber` catalogue parser from `app/scraper/catalogue.py`.
- Remove the fallback from `parse_catalogue_pdf_bytes()` so OpenRouter failures fail visibly instead of silently falling back to local parsing.
- Keep catalogue PDF uploads/downloads routed to OpenRouter unless the product decision is to reject PDFs entirely.
- Remove or replace every remaining `pdfplumber` and `pypdf` use before deleting their requirement pins.
- Update tests, scripts, and docs that assume the legacy parser exists.

### Out of scope

- Removing XLSX catalogue support.
- Removing `/my_day` HTML sync.
- Removing schedule HTML parsing.
- Removing OpenRouter configuration or the normalized `CatalogueEntry` contract.

## Implementation Plan

### 1. Preserve shared non-PDF catalogue helpers

`openrouter_catalogue._normalize_event_name()` imports `_event_name_from_code()` from `app/scraper/catalogue.py`. Before deleting legacy PDF code, move `_CLASS_CODE_TO_NAME` and `_event_name_from_code()` into a small shared module such as `app/scraper/class_codes.py`, then update:

- `app/scraper/catalogue.py`
- `app/scraper/openrouter_catalogue.py`
- tests that assert class-code mapping behavior

Keep `_flush_height_groups()` in `catalogue.py` if XLSX parsing still uses it.

### 2. Remove the catalogue fallback path

Change `parse_catalogue_pdf_bytes()` so:

- OpenRouter must be enabled for PDF catalogue parsing.
- missing OpenRouter configuration raises a clear error for PDF catalogue parsing.
- OpenRouter extraction errors propagate to the caller after logging context.
- no code calls `parse_catalogue_pdf()`.

Then delete from `app/scraper/catalogue.py`:

- `parse_catalogue_pdf()`
- `_parse_pdf_pages()`
- `_extract_pdf_lines()`
- `_split_dog_handler()`
- `_split_dog_handler_nationals()`
- PDF-only regular expressions and parser comments

### 3. Decide how to remove `pypdf`

`pypdf` is not part of the legacy fallback; it supports the current OpenRouter path. To remove it, replace local chunking with one of these designs:

1. Send the complete PDF to OpenRouter in one request and rely on OpenRouter's file parser.
2. Move chunking outside the app, for example to an upstream service or preprocessing job that is not part of this runtime.
3. Keep catalogue PDF support but reject PDFs above a configured byte/page limit when no local chunking exists.

The simplest dependency-removal implementation is option 1 with strong logging and a clear error when OpenRouter rejects a large PDF. Remove `OPENROUTER_PDF_PAGES_PER_CHUNK` and `OPENROUTER_PDF_CHUNK_OVERLAP` only if chunking is deleted.

### 4. Decide how to remove `pdfplumber`

`pdfplumber` also powers schedule PDF parsing. To remove it, make one product decision:

1. Support schedule HTML only and fail clearly when TopDog returns a schedule PDF.
2. Route schedule PDFs through OpenRouter or another external extractor.
3. Prefer `/my_day` for schedule data and treat authenticated schedule PDFs as unsupported fallback data.

The smallest dependency-removal implementation is option 1: remove `parse_schedule_pdf()`, keep `parse_schedule_html()`, and make `download_and_parse_schedule()` raise a clear unsupported-format error for PDFs.

### 5. Update user-facing flows

If catalogue PDF upload remains OpenRouter-backed:

- keep `.pdf` in `app/templates/trial_detail.html`
- update upload error text to say PDF parsing requires OpenRouter configuration
- keep `upload_catalogue_job()` PDF routing to `parse_catalogue_pdf_bytes_sync()`

If PDFs are rejected entirely:

- remove `.pdf` from the upload `accept` attribute
- reject PDF content in `upload_catalogue_job()` and surface `upload_error`
- update copy to advertise `.xlsx` only

### 6. Remove obsolete tests and fixtures

Delete tests that exercise the local catalogue parser:

- `tests/test_catalogue_pdf_parser.py`
- `tests/test_catalogue_pdf_real_fixture.py`
- fallback-specific assertions in `tests/test_openrouter_catalogue.py`
- committed catalogue PDF fixtures used only by those tests, if present

Add or update tests for:

- OpenRouter disabled for PDF catalogue parsing raises a clear error
- OpenRouter failure does not call a legacy fallback
- OpenRouter catalogue extraction still normalizes entries
- schedule PDF behavior after the `pdfplumber` decision
- no imports of removed parser functions remain

### 7. Remove or rewrite developer tooling

`scripts/compare_catalogue_extractors.py` compares the legacy parser against OpenRouter. After fallback removal, either delete the script or rewrite it as an OpenRouter-only validation script that summarizes extracted entries without importing `parse_catalogue_pdf()`.

### 8. Update docs and configuration

Update:

- `docs/openrouter-catalogue-extraction-plan.md` to remove fallback rollout language
- `README.md` and `PLAN.md` to remove schedule PDF support if `pdfplumber` is removed
- `RESULTS.md` if its dependency guidance still lists `pdfplumber`
- `.env.example`, `docker-compose.yml`, and Nomad files if chunking env vars are removed with `pypdf`

Then remove from `requirements.txt` only after the code changes above are complete:

- `pdfplumber==0.11.4`
- `pypdf==6.14.0`

`requirements.web.txt` does not declare these packages today, so no web requirements change is needed.

## Verification Plan

Run repository searches before and after dependency removal:

- `rg "pdfplumber|pypdf" app tests scripts docs README.md PLAN.md RESULTS.md requirements.txt`
- `rg "parse_catalogue_pdf|_parse_pdf_pages|parse_schedule_pdf|split_pdf_into_chunks" app tests scripts`
- `rg "OPENROUTER_PDF_PAGES_PER_CHUNK|OPENROUTER_PDF_CHUNK_OVERLAP" .`

Run automated checks:

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python -m py_compile $(rg --files -g '*.py' app scripts migrations)`

Expected final state:

- no `pdfplumber` or `pypdf` import remains
- no `pdfplumber` or `pypdf` pin remains in requirements
- catalogue PDF parsing either succeeds through OpenRouter or fails with an explicit OpenRouter/configuration error
- schedule PDF behavior matches the chosen product decision
- XLSX catalogue parsing, schedule HTML parsing, `/my_day`, and prediction tests continue to pass
