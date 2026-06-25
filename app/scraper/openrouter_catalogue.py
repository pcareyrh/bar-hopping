"""OpenRouter-backed TopDog catalogue PDF extraction."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
VALID_HEIGHTS = {200, 300, 400, 500, 600}
DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 600

# Gemini strict JSON schema rejects integer minimum/enum constraints on nested items.
CATALOGUE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "event_name": {"type": "string"},
                    "cat_number": {"type": "string"},
                    "day": {"type": "integer"},
                    "height_group": {"type": "integer"},
                    "run_position": {"type": "integer"},
                    "height_group_total": {"type": "integer"},
                    "nfc": {"type": "boolean"},
                    "dog_name": {"type": ["string", "null"]},
                    "handler_name": {"type": ["string", "null"]},
                    "ring_number": {"type": ["string", "null"]},
                },
                "required": [
                    "event_name",
                    "cat_number",
                    "day",
                    "height_group",
                    "run_position",
                    "height_group_total",
                    "nfc",
                    "dog_name",
                    "handler_name",
                    "ring_number",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["entries"],
    "additionalProperties": False,
}

_RE_RING_NUMBER = re.compile(r"(\d+)")
_RE_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_RE_PAGE_RANGE_START = re.compile(r"^(\d+)")


class OpenRouterApiError(Exception):
    """OpenRouter HTTP, auth, provider, or configuration failure."""


class OpenRouterParseError(Exception):
    """Malformed or truncated model JSON output."""


def _page_range_start(page_range: str | None) -> int:
    if not page_range:
        return 1
    match = _RE_PAGE_RANGE_START.match(page_range.strip())
    return int(match.group(1)) if match else 1


def _absolute_page_range(page_offset: int, local_start: int, local_end: int) -> str:
    """Convert local 0-based half-open indices to absolute 1-based inclusive range."""
    return f"{page_offset + local_start}-{page_offset + local_end - 1}"


def _env(name: str, default: str = "") -> str:
    """Read env var, strip whitespace and inline comments."""
    raw = os.getenv(name, default)
    return raw.split("#", 1)[0].strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def is_openrouter_enabled() -> bool:
    if _env("OPENROUTER_ENABLED", "false").lower() != "true":
        return False
    return bool(_env("OPENROUTER_API_KEY"))


def _openrouter_model() -> str | None:
    model = _env("OPENROUTER_MODEL")
    return model or None


def _pdf_engine() -> str:
    return _env("OPENROUTER_PDF_ENGINE", "mistral-ocr") or "mistral-ocr"


def _pages_per_chunk() -> int:
    return max(1, _env_int("OPENROUTER_PDF_PAGES_PER_CHUNK", 12))


def _chunk_overlap() -> int:
    return max(0, _env_int("OPENROUTER_PDF_CHUNK_OVERLAP", 0))


def _max_tokens() -> int:
    return max(1024, _env_int("OPENROUTER_MAX_TOKENS", 32768))


def _max_concurrency() -> int:
    return max(1, _env_int("OPENROUTER_MAX_CONCURRENCY", 3))


def extraction_timeout_seconds() -> int:
    return max(
        1,
        _env_int("OPENROUTER_EXTRACTION_TIMEOUT", DEFAULT_EXTRACTION_TIMEOUT_SECONDS),
    )


def extraction_prompt(*, page_range: str | None = None, state_hint: str | None = None) -> str:
    prompt = (
        "Extract every TopDog agility catalogue run-order entry from this PDF.\n\n"
        "Rules:\n"
        "- Extract every catalogue/run-order row in document order. Do not skip rows.\n"
        "- Do not sort by catalogue number.\n"
        "- Do not invent missing dog names, handlers, rings, or days — use null when not visible.\n"
        "- run_position is the row order within the same day, event, height group, and ring.\n"
        "- height_group_total is the number of non-NFC entries in that same group.\n"
        "- event_name must include the full class name and session code in parentheses, "
        'e.g. "Novice Agility (AD1)", "Excellent Jumping (JDX2)", "Masters Agility (ADM1)". '
        "Never return bare codes like AD1 or JDO alone.\n"
        "- Infer day from visible headers. When DAY N markers are present, use N as the "
        "day number for all following entries until the next DAY marker — never restart "
        "at SATURDAY=1 mid-catalogue. SATURDAY/SUNDAY map to days 1 and 2 only in "
        "short two-day catalogues that lack DAY N markers.\n"
        "- Infer ring_number from RING N headers when visible.\n"
        "- Treat catalogue numbers ending in NFC as non-for-competition (nfc=true).\n"
        "- height_group must be one of 200, 300, 400, 500, 600.\n"
        "- Return only schema-valid JSON."
    )
    if page_range:
        prompt += f"\n\nThis chunk covers PDF pages {page_range}."
    if state_hint:
        prompt += f"\n\nContext from the previous chunk: {state_hint}"
    return prompt


def split_pdf_into_chunks(pdf_data: bytes, pages_per_chunk: int) -> list[tuple[str, bytes, str]]:
    """Split a PDF into page-range chunks for OpenRouter extraction."""
    from pypdf import PdfReader, PdfWriter

    overlap = _chunk_overlap()
    reader = PdfReader(io.BytesIO(pdf_data))
    total_pages = len(reader.pages)
    if total_pages <= pages_per_chunk:
        return [("catalogue.pdf", pdf_data, f"1-{total_pages}")]

    chunks: list[tuple[str, bytes, str]] = []
    start = 0
    while start < total_pages:
        chunk_start = max(0, start - overlap) if start > 0 else 0
        end = min(start + pages_per_chunk, total_pages)
        writer = PdfWriter()
        for page_idx in range(chunk_start, end):
            writer.add_page(reader.pages[page_idx])
        buf = io.BytesIO()
        writer.write(buf)
        filename = f"catalogue-pages-{chunk_start + 1}-{end}.pdf"
        page_range = f"{chunk_start + 1}-{end}"
        chunks.append((filename, buf.getvalue(), page_range))
        start = end
    return chunks


def build_request_payload(
    pdf_data: bytes,
    filename: str,
    *,
    page_range: str | None = None,
    state_hint: str | None = None,
) -> dict[str, Any]:
    model = _openrouter_model()
    if not model:
        raise ValueError("OPENROUTER_MODEL is not configured")

    b64 = base64.b64encode(pdf_data).decode("ascii")
    file_data = f"data:application/pdf;base64,{b64}"

    return {
        "model": model,
        "max_tokens": _max_tokens(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": extraction_prompt(page_range=page_range, state_hint=state_hint)},
                    {
                        "type": "file",
                        "file": {
                            "filename": filename,
                            "file_data": file_data,
                        },
                    },
                ],
            }
        ],
        "plugins": [
            {
                "id": "file-parser",
                "pdf": {"engine": _pdf_engine()},
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "topdog_catalogue",
                "strict": True,
                "schema": CATALOGUE_JSON_SCHEMA,
            },
        },
    }


def _normalize_ring_number(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _RE_RING_NUMBER.search(text)
    return match.group(1) if match else text


def _normalize_nfc(cat_number: str, nfc: bool) -> bool:
    return nfc or cat_number.upper().endswith("NFC")


def _entry_identity(entry: dict) -> tuple:
    return (
        entry["day"],
        entry["event_name"],
        entry["cat_number"],
    )


def _normalize_event_name(name: str) -> str:
    from app.scraper.catalogue import _event_name_from_code

    name = name.strip()
    paren = re.search(r"\(([A-Z]{2,4}\d?)\)", name)
    if paren:
        mapped = _event_name_from_code(paren.group(1))
        if mapped:
            return mapped
    bare = re.match(r"^\(?([A-Z]{2,4}\d?)\)?$", name)
    if bare:
        mapped = _event_name_from_code(bare.group(1))
        if mapped:
            return mapped
    return name


def normalize_openrouter_entries(raw_entries: list[dict]) -> tuple[list[dict], int]:
    """Validate, dedupe, normalize, and recompute positions/counts.

    Returns (normalized_entries, validation_failure_count).
    """
    if not raw_entries:
        return [], 0

    cleaned: list[dict] = []
    seen: set[tuple] = set()
    failures = 0

    for raw in raw_entries:
        event_name = _normalize_event_name(raw.get("event_name") or "")
        cat_number = str(raw.get("cat_number") or "").strip()
        day = raw.get("day")
        height_group = raw.get("height_group")

        if not event_name or not cat_number or not isinstance(day, int) or day < 1:
            failures += 1
            continue
        if height_group not in VALID_HEIGHTS:
            failures += 1
            continue

        nfc = _normalize_nfc(cat_number, bool(raw.get("nfc", False)))
        ring_number = _normalize_ring_number(raw.get("ring_number"))
        dog_name = raw.get("dog_name")
        handler_name = raw.get("handler_name")
        if isinstance(dog_name, str):
            dog_name = dog_name.strip() or None
        if isinstance(handler_name, str):
            handler_name = handler_name.strip() or None

        entry = {
            "event_name": event_name,
            "cat_number": cat_number,
            "day": day,
            "height_group": height_group,
            "run_position": 0,
            "height_group_total": 0,
            "nfc": nfc,
            "dog_name": dog_name,
            "handler_name": handler_name,
            "ring_number": ring_number,
        }

        identity = _entry_identity(entry)
        if identity in seen:
            continue
        seen.add(identity)
        cleaned.append(entry)

    if not cleaned:
        return [], failures

    groups: dict[tuple, list[dict]] = {}
    for entry in cleaned:
        key = (entry["day"], entry["event_name"], entry["height_group"], entry["ring_number"])
        groups.setdefault(key, []).append(entry)

    results: list[dict] = []
    for group_entries in groups.values():
        non_nfc_total = sum(1 for e in group_entries if not e["nfc"])
        for pos, entry in enumerate(group_entries, start=1):
            results.append(
                {
                    **entry,
                    "run_position": pos,
                    "height_group_total": non_nfc_total,
                }
            )

    return results, failures


def _parse_response_content(content: str) -> list[dict]:
    text = content.strip()
    fence = _RE_JSON_FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    payload = json.loads(text)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise OpenRouterParseError("OpenRouter response missing entries array")
    return entries


def _openrouter_error_message(body: dict[str, Any]) -> str:
    error = body.get("error") or {}
    message = error.get("message") or "OpenRouter request failed"
    metadata = error.get("metadata") or {}
    provider = metadata.get("provider_name")
    if provider:
        return f"{message} (provider={provider})"
    return message


async def _call_openrouter(payload: dict[str, Any], *, api_key: str) -> list[dict]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(OPENROUTER_API_URL, headers=headers, json=payload)

    if resp.status_code != 200:
        try:
            error_body = resp.json()
        except json.JSONDecodeError:
            detail = resp.text.strip() or "empty response body"
            raise OpenRouterApiError(
                f"OpenRouter request failed with HTTP {resp.status_code}: {detail}"
            ) from None
        if not isinstance(error_body, dict):
            raise OpenRouterApiError(f"OpenRouter request failed with HTTP {resp.status_code}")
        raise OpenRouterApiError(
            f"OpenRouter request failed with HTTP {resp.status_code}: "
            f"{_openrouter_error_message(error_body)}"
        )

    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise OpenRouterApiError("OpenRouter returned a non-JSON response") from exc

    if not isinstance(body, dict):
        raise OpenRouterApiError("OpenRouter returned an invalid response")

    if body.get("error"):
        raise OpenRouterApiError(_openrouter_error_message(body))

    choices = body.get("choices") or []
    if not choices:
        raise OpenRouterApiError("OpenRouter response missing choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise OpenRouterApiError("OpenRouter response missing message content")

    return _parse_response_content(content)


async def _extract_chunk(
    pdf_data: bytes,
    filename: str,
    *,
    api_key: str,
    page_range: str | None = None,
    state_hint: str | None = None,
) -> list[dict]:
    payload = build_request_payload(
        pdf_data,
        filename,
        page_range=page_range,
        state_hint=state_hint,
    )
    return await _call_openrouter(payload, api_key=api_key)


async def _extract_chunk_resilient(
    pdf_data: bytes,
    filename: str,
    *,
    api_key: str,
    page_range: str | None = None,
    state_hint: str | None = None,
) -> list[dict]:
    try:
        return await _extract_chunk(
            pdf_data,
            filename,
            api_key=api_key,
            page_range=page_range,
            state_hint=state_hint,
        )
    except (json.JSONDecodeError, OpenRouterParseError) as exc:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_data))
        page_count = len(reader.pages)
        if page_count <= 1:
            raise OpenRouterParseError(f"OpenRouter chunk failed for {filename}: {exc}") from exc

        page_offset = _page_range_start(page_range)
        mid = page_count // 2
        log.warning(
            "openrouter_catalogue: chunk %s failed (%s); splitting into %d + %d pages",
            filename,
            exc,
            mid,
            page_count - mid,
        )

        def _subchunk(local_start: int, local_end: int) -> tuple[str, bytes, str]:
            from pypdf import PdfWriter

            writer = PdfWriter()
            for page_idx in range(local_start, local_end):
                writer.add_page(reader.pages[page_idx])
            buf = io.BytesIO()
            writer.write(buf)
            abs_range = _absolute_page_range(page_offset, local_start, local_end)
            sub_name = f"{filename.rsplit('.', 1)[0]}-split-{abs_range}.pdf"
            return sub_name, buf.getvalue(), abs_range

        left_name, left_bytes, left_range = _subchunk(0, mid)
        right_name, right_bytes, right_range = _subchunk(mid, page_count)

        left_entries = await _extract_chunk_resilient(
            left_bytes,
            left_name,
            api_key=api_key,
            page_range=left_range,
            state_hint=state_hint,
        )
        right_hint = _state_hint_from_entries(left_entries) or state_hint
        right_entries = await _extract_chunk_resilient(
            right_bytes,
            right_name,
            api_key=api_key,
            page_range=right_range,
            state_hint=right_hint,
        )
        return left_entries + right_entries


def _state_hint_from_entries(entries: list[dict]) -> str | None:
    if not entries:
        return None
    last = entries[-1]
    max_day = max(e.get("day", 1) for e in entries if isinstance(e.get("day"), int))
    return (
        f"max_day={max_day}, day={last.get('day')}, event_name={last.get('event_name')}, "
        f"height_group={last.get('height_group')}, ring_number={last.get('ring_number')}"
    )


async def extract_catalogue_from_pdf(
    pdf_data: bytes,
    *,
    filename: str = "catalogue.pdf",
    trial_external_id: str | None = None,
    catalogue_url: str | None = None,
) -> list[dict]:
    """Call OpenRouter to extract catalogue entries from PDF bytes."""
    api_key = _env("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is not configured")

    pdf_hash = hashlib.sha256(pdf_data).hexdigest()[:16]
    model = _openrouter_model()
    engine = _pdf_engine()
    pages_per_chunk = _pages_per_chunk()
    chunks = split_pdf_into_chunks(pdf_data, pages_per_chunk)

    log.info(
        "openrouter_catalogue: extracting trial=%s url=%s bytes=%d hash=%s model=%s "
        "engine=%s chunks=%d pages_per_chunk=%d",
        trial_external_id or "?",
        catalogue_url or "?",
        len(pdf_data),
        pdf_hash,
        model,
        engine,
        len(chunks),
        pages_per_chunk,
    )

    raw_entries: list[dict] = []
    state_hint: str | None = None

    async def _extract_logged_chunk(
        chunk_name: str,
        chunk_bytes: bytes,
        page_range: str,
        hint: str | None,
    ) -> list[dict]:
        chunk_entries = await _extract_chunk_resilient(
            chunk_bytes,
            chunk_name,
            api_key=api_key,
            page_range=page_range,
            state_hint=hint,
        )
        log.info(
            "openrouter_catalogue: chunk trial=%s pages=%s entries=%d",
            trial_external_id or "?",
            page_range,
            len(chunk_entries),
        )
        return chunk_entries

    for chunk_name, chunk_bytes, page_range in chunks:
        chunk_entries = await _extract_logged_chunk(
            chunk_name,
            chunk_bytes,
            page_range,
            state_hint,
        )
        if len(chunks) > 1 and not normalize_openrouter_entries(chunk_entries)[0]:
            raise ValueError(
                "OpenRouter extraction produced no valid entries "
                f"for PDF pages {page_range}"
            )
        raw_entries.extend(chunk_entries)
        state_hint = _state_hint_from_entries(chunk_entries) or state_hint

    entries, failure_count = normalize_openrouter_entries(raw_entries)

    log.info(
        "openrouter_catalogue: extracted trial=%s raw=%d normalized=%d validation_failures=%d chunks=%d",
        trial_external_id or "?",
        len(raw_entries),
        len(entries),
        failure_count,
        len(chunks),
    )

    if not entries:
        raise ValueError("OpenRouter extraction produced no valid entries")
    if failure_count >= len(entries):
        raise ValueError(
            "OpenRouter extraction produced too many invalid entries "
            f"({failure_count} invalid, {len(entries)} valid)"
        )

    return entries
