# OpenRouter Catalogue Extraction Plan

## Objective

Replace the fragile, template-heavy PDF catalogue parser with an OpenRouter-backed extraction path that can process different TopDog catalogue PDF layouts and return a consistent structure for the existing prediction engine.

The goal is not to change prediction behaviour. The goal is to normalize every catalogue source into the same `CatalogueEntry` data shape the application already uses.

## Current codebase flow

1. Users sync their TopDog entries through `app/scraper/auth.py`.
2. Trial metadata is scraped from `/trials/{id}` in `app/scraper/trials.py`.
3. `refresh_trial_docs_job` in `app/worker.py` attempts to populate catalogue and schedule data:
   - preferred source: authenticated `/my_day` HTML via `app/scraper/my_day.py`
   - fallback source: TopDog catalogue document via `app/scraper/catalogue.py`
   - fallback schedule data via `app/scraper/schedule.py`
4. Catalogue data is stored as `CatalogueEntry` rows.
5. The schedule/prediction layer in `app/routers/schedule.py` builds predictions from those normalized catalogue rows.

## Existing downstream catalogue contract

The prediction engine already has a stable contract. Every catalogue source should produce entries with:

| Field | Description |
|---|---|
| `event_name` | Class/event name, preserving round/session suffixes when needed |
| `cat_number` | TopDog catalogue number, including `NFC` suffix when present |
| `day` | Trial day number, 1-based |
| `height_group` | One of `200`, `300`, `400`, `500`, `600` |
| `run_position` | 1-based position within the day/event/height/ring running order |
| `height_group_total` | Non-NFC count for the day/event/height/ring group |
| `nfc` | Whether the entry is non-for-competition |
| `dog_name` | Dog name when available |
| `handler_name` | Handler name when available |
| `ring_number` | Ring identifier when available |

This contract should remain unchanged so `_resolve_catalogue_links`, `_compute_catalogue_blocks`, and `_build_predictions` continue working.

## Problem with the current PDF parser

`app/scraper/catalogue.py` currently handles PDF extraction with many format-specific regular expressions and state-machine branches, including:

- legacy day-prefixed headers
- inline height headers
- ring/session-code headers
- Pawlympics-style code-on-next-line pages
- Nationals day/ring/heat formats
- column order differences for dog, breed, and handler
- special cases for wrapped breed lines and four-digit catalogue numbers

This approach is brittle because every new catalogue layout requires another parser template. The maintenance cost will keep increasing as clubs publish PDFs with different headers, ordering, or column layouts.

## Recommended architecture

Use OpenRouter only for PDF catalogue extraction, while keeping deterministic sources where they are already reliable:

1. Keep `/my_day` HTML as the preferred source when available.
2. Keep XLSX parsing with `openpyxl`.
3. Use OpenRouter as the preferred parser for PDF catalogues.
4. Keep the existing regex PDF parser as a fallback during rollout.

This limits risk because the rest of the application continues to consume the same normalized `CatalogueEntry` rows.

## Proposed new module

Add:

```text
app/scraper/openrouter_catalogue.py
```

Responsibilities:

- accept downloaded PDF bytes plus filename/trial metadata
- base64 encode the PDF as `data:application/pdf;base64,...`
- call `https://openrouter.ai/api/v1/chat/completions`
- send the PDF as a `file` content part
- request strict JSON Schema output
- parse and validate the returned JSON
- return normalized catalogue dictionaries compatible with `CatalogueEntry`

## Configuration

Add environment variables:

```env
OPENROUTER_API_KEY=
OPENROUTER_MODEL=
OPENROUTER_PDF_ENGINE=cloudflare-ai
OPENROUTER_ENABLED=true
```

Suggested defaults:

- `OPENROUTER_ENABLED=false` until rollout is ready.
- `OPENROUTER_PDF_ENGINE=cloudflare-ai` for text-based PDFs.
- Allow `mistral-ocr` for scanned or image-heavy PDFs.

## OpenRouter request shape

OpenRouter supports PDF inputs and structured JSON output through `/api/v1/chat/completions`.

The request should:

- include a user message with:
  - extraction instructions
  - the PDF file content
- include `response_format` with `type: json_schema`
- set `strict: true`
- optionally enable:
  - `file-parser` plugin for PDF parsing
  - `response-healing` plugin for non-streaming JSON repair

Conceptual request:

```json
{
  "model": "${OPENROUTER_MODEL}",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Extract every TopDog catalogue run-order entry..."
        },
        {
          "type": "file",
          "file": {
            "filename": "catalogue.pdf",
            "file_data": "data:application/pdf;base64,..."
          }
        }
      ]
    }
  ],
  "plugins": [
    {
      "id": "file-parser",
      "pdf": {
        "engine": "cloudflare-ai"
      }
    }
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "topdog_catalogue",
      "strict": true,
      "schema": {}
    }
  }
}
```

## JSON Schema target

The schema should require a single top-level object:

```json
{
  "entries": [
    {
      "event_name": "Masters Agility",
      "cat_number": "410",
      "day": 2,
      "height_group": 400,
      "run_position": 7,
      "height_group_total": 24,
      "nfc": false,
      "dog_name": "Example Dog",
      "handler_name": "Example Handler",
      "ring_number": "1"
    }
  ]
}
```

Schema constraints:

- `entries` is required.
- `height_group` enum: `200`, `300`, `400`, `500`, `600`.
- `day` is an integer greater than or equal to `1`.
- `run_position` is an integer greater than or equal to `1`.
- `height_group_total` is an integer greater than or equal to `0`.
- `nfc` is boolean.
- `dog_name`, `handler_name`, and `ring_number` may be `null`.
- `additionalProperties` should be `false`.

## Prompting rules

The prompt should make the model transform the document, not infer missing facts.

Key instructions:

- Extract every catalogue/run-order row.
- Preserve document order.
- Do not sort by catalogue number.
- Do not invent missing dog names, handlers, rings, or days.
- Use `null` when a value is not visible.
- `run_position` is the row order within the same day, event, height group, and ring.
- `height_group_total` is the number of non-NFC entries in that same group.
- If a class has visible heats, rounds, or session codes, preserve them in `event_name`, for example `Open Agility (ADO1)`.
- Treat `NFC` catalogue numbers as non-for-competition.
- Return only schema-valid JSON.

## Integration point

Update `download_and_parse_catalogue` in `app/scraper/catalogue.py`.

Recommended flow:

1. Download the TopDog catalogue as it does today.
2. If the response is XLSX, keep `parse_catalogue_xlsx`.
3. If the response is PDF:
   - if OpenRouter is enabled and `OPENROUTER_API_KEY` is present, call the new OpenRouter extractor
   - validate and normalize returned entries
   - if extraction or validation fails, fall back to existing `parse_catalogue_pdf`
4. Return normalized catalogue dictionaries.

This keeps rollout safe and reversible.

## Validation and normalization

Do not blindly trust model output. Add a deterministic validation step before database writes.

Validation should:

- reject empty extractions
- reject invalid heights
- reject missing `event_name`, `cat_number`, `day`, or `height_group`
- drop exact duplicate rows
- allow the same catalogue number across multiple days or rounds
- normalize `ring_number` to the app's existing bare identifier format when possible
- normalize `nfc` from both the boolean field and `cat_number` suffix

Recommended deterministic recomputation:

- recompute `run_position` from returned row order within `(day, event_name, height_group, ring_number)`
- recompute `height_group_total` from non-NFC entries in that same group

This lets OpenRouter identify rows and grouping while the app owns position/count invariants.

## Database write behaviour

Keep the existing write path:

1. Set linked `SessionEntry.catalogue_entry_id` values to `None`.
2. Delete old `CatalogueEntry` rows for the trial.
3. Insert normalized entries.
4. Call `_resolve_catalogue_links`.
5. Commit.

No prediction database schema change is required for the first implementation.

## Observability

Add logs around OpenRouter extraction:

- trial external ID
- catalogue URL
- PDF byte size
- PDF hash
- OpenRouter model
- PDF parser engine
- extracted row count
- validation failure count
- whether legacy fallback was used

Avoid logging TopDog credentials. Consider avoiding dog and handler names in logs.

## Testing strategy

Add unit tests for the OpenRouter adapter without calling the real API:

- request payload includes PDF file data
- request payload includes strict JSON Schema
- valid OpenRouter JSON normalizes to catalogue dictionaries
- invalid height is rejected
- duplicate rows are dropped
- `run_position` is recomputed from row order
- `height_group_total` excludes NFC entries
- same catalogue number across different days/rounds is allowed
- OpenRouter failure falls back to legacy PDF parser

Keep existing `tests/test_catalogue_pdf_parser.py` during rollout to protect the fallback path.

## Rollout sequence

1. Add OpenRouter environment variables to `.env.example` and deployment config.
2. Add `app/scraper/openrouter_catalogue.py`.
3. Add JSON Schema, prompt construction, and API client code.
4. Add validation/normalization tests.
5. Wire the PDF branch of `download_and_parse_catalogue`.
6. Keep legacy PDF parsing as fallback.
7. Add logging and metrics.
8. Enable in a controlled deployment.
9. Review extraction quality across several catalogue PDFs.
10. Once stable, reduce reliance on regex PDF templates or keep them as an offline fallback.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Model returns schema-valid but semantically wrong data | Deterministically recompute positions/counts and validate against user `SessionEntry` cat numbers |
| Large PDFs exceed context or parser limits | Use OpenRouter file parser annotations and consider page/chunk retry strategy |
| Scanned PDFs need OCR | Make `OPENROUTER_PDF_ENGINE` configurable and support `mistral-ocr` |
| OpenRouter outage | Fall back to the existing parser and mark extraction failures in logs |
| Privacy concerns | Send only catalogue PDFs, never TopDog credentials; avoid sensitive logs |
| Cost variability | Default to cheaper parser engine, log page/usage data, cache by PDF hash later |

## Success criteria

- PDF catalogue extraction no longer requires adding layout-specific regex templates.
- Extracted data lands in the existing `CatalogueEntry` shape.
- User predictions continue to work through the existing schedule pipeline.
- XLSX and `/my_day` behaviour remains unchanged.
- Legacy parser remains available as fallback during rollout.
