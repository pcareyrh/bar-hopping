"""OpenRouter-backed TopDog timetable PDF lunch-break extraction."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from datetime import datetime, time
from typing import Any

import httpx

from app.routers.schedule import _ring_label
from app.scraper.openrouter_catalogue import (
    OPENROUTER_API_URL,
    OpenRouterApiError,
    OpenRouterParseError,
    _env,
    _env_int,
    _max_tokens,
    _openrouter_error_message,
    _openrouter_model,
    _pdf_engine,
)

log = logging.getLogger(__name__)

DEFAULT_LUNCH_BREAK_MINS = 45
DEFAULT_MAX_TIMETABLE_PAGES = 10
_SCHEDULE_OVERVIEW_PAGES = {7, 8}

_RE_DAY_TIMETABLE = re.compile(r"^DAY\s+(\d+)\s+TIMETABLE", re.MULTILINE)
_RE_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_RE_LUNCH_LABEL = re.compile(
    r"(?i)(lunch|walk\s*&\s*lunch|build\s*&\s*walk\s*&\s*lunch)"
)
_RE_GROUP_2 = re.compile(r"(?i)GROUP\s+2")

TIMETABLE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_summary": {"type": "string"},
        "lunch_breaks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": ["integer", "null"]},
                    "ring": {"type": "string"},
                    "start_time": {"type": "string"},
                    "duration_mins": {"type": ["integer", "null"]},
                    "label": {"type": "string"},
                },
                "required": ["day", "ring", "start_time", "duration_mins", "label"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["page_summary", "lunch_breaks"],
    "additionalProperties": False,
}


def _timetable_max_pages() -> int:
    return max(1, _env_int("OPENROUTER_TIMETABLE_MAX_PAGES", DEFAULT_MAX_TIMETABLE_PAGES))


def is_openrouter_timetable_enabled() -> bool:
    openrouter_on = _env("OPENROUTER_ENABLED", "false").lower() == "true"
    default = "true" if openrouter_on else "false"
    if _env("OPENROUTER_TIMETABLE_ENABLED", default).lower() != "true":
        return False
    if not _env("OPENROUTER_API_KEY"):
        return False
    return bool(_openrouter_model())


def detect_timetable_page_ranges(pdf_data: bytes) -> list[tuple[int, int, int | None]]:
    """Return [(start_page, end_page, day_hint), ...] with 1-based inclusive page numbers."""
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — cannot detect timetable pages")
        return []

    ranges: list[tuple[int, int, int | None]] = []
    with pdfplumber.open(io.BytesIO(pdf_data)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            if page_idx in _SCHEDULE_OVERVIEW_PAGES:
                continue
            text = page.extract_text() or ""
            match = _RE_DAY_TIMETABLE.search(text)
            if match:
                ranges.append((page_idx, page_idx, int(match.group(1))))
    return ranges


def timetable_extraction_prompt(*, page_range: str | None = None) -> str:
    prompt = (
        "Extract lunch and walk/lunch break rows from this TopDog agility timetable PDF page.\n\n"
        "Rules:\n"
        "- Extract rows labelled LUNCH, WALK & LUNCH, BUILD & WALK & LUNCH, or similar "
        "lunch-related labels.\n"
        "- Include day (integer), ring (number or RING N), start_time as 24-hour HH:MM, "
        "duration_mins when visible, and the full row label text.\n"
        "- Do not invent times or rings; use null for day when not visible on the page.\n"
        "- Ignore pure WALKING rows (course walks without lunch).\n"
        "- Ignore sponsor pages, judge bios, and run-order catalogue rows.\n"
        "- Return an empty lunch_breaks array when this page has no timetable breaks.\n"
        "- Return only schema-valid JSON."
    )
    if page_range:
        prompt += f"\n\nThis chunk covers PDF page {page_range}."
    return prompt


def build_timetable_request_payload(
    pdf_data: bytes,
    filename: str,
    *,
    page_range: str | None = None,
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
                    {
                        "type": "text",
                        "text": timetable_extraction_prompt(page_range=page_range),
                    },
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
                "name": "topdog_timetable",
                "strict": True,
                "schema": TIMETABLE_JSON_SCHEMA,
            },
        },
    }


def _parse_start_time(value: str | None) -> time | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _label_accepted(label: str | None) -> bool:
    if not label:
        return False
    text = label.strip()
    if not text:
        return False
    if re.search(r"(?i)^WALKING\b", text) and not _RE_LUNCH_LABEL.search(text):
        return False
    return bool(_RE_LUNCH_LABEL.search(text))


def _dedup_normalized_breaks(breaks: list[dict]) -> list[dict]:
    """Keep earliest lunch per (day, ring); drop GROUP 2 when an earlier lunch exists."""
    by_key: dict[tuple[int, str], list[dict]] = {}
    for item in breaks:
        key = (item["day"], item["ring"])
        by_key.setdefault(key, []).append(item)

    result: list[dict] = []
    for key, group in by_key.items():
        group.sort(key=lambda b: b["lunch_break_at"] or time(23, 59))

        filtered: list[dict] = []
        for item in group:
            label = item.get("_label") or ""
            if _RE_GROUP_2.search(label) and any(
                other["lunch_break_at"]
                and item["lunch_break_at"]
                and other["lunch_break_at"] < item["lunch_break_at"]
                for other in group
                if other is not item
            ):
                log.info(
                    "openrouter_timetable: dropped GROUP 2 lunch day=%s ring=%s at=%s",
                    key[0],
                    key[1],
                    item["lunch_break_at"],
                )
                continue
            filtered.append(item)

        lunch_rows = [
            item
            for item in filtered
            if "lunch" in (item.get("_label") or "").lower()
            and item.get("lunch_break_at") is not None
        ]
        if not lunch_rows:
            continue

        winner = min(lunch_rows, key=lambda b: b["lunch_break_at"])
        for item in lunch_rows:
            if item is not winner:
                log.info(
                    "openrouter_timetable: dropped duplicate lunch day=%s ring=%s at=%s label=%r",
                    key[0],
                    key[1],
                    item["lunch_break_at"],
                    item.get("_label"),
                )
        result.append({k: v for k, v in winner.items() if not k.startswith("_")})

    result.sort(key=lambda b: (b["day"], b["ring"], b["lunch_break_at"] or time(0, 0)))
    return result


def _merge_break_lists(breaks: list[dict]) -> list[dict]:
    """Merge breaks from multiple pages; keep earliest lunch per (day, ring)."""
    by_key: dict[tuple[int, str], dict] = {}
    for item in breaks:
        key = (item["day"], item["ring"])
        existing = by_key.get(key)
        if existing is None or (
            item["lunch_break_at"] is not None
            and (
                existing["lunch_break_at"] is None
                or item["lunch_break_at"] < existing["lunch_break_at"]
            )
        ):
            if existing is not None:
                log.info(
                    "openrouter_timetable: dropped duplicate lunch day=%s ring=%s at=%s",
                    key[0],
                    key[1],
                    existing["lunch_break_at"],
                )
            by_key[key] = item
        else:
            log.info(
                "openrouter_timetable: dropped duplicate lunch day=%s ring=%s at=%s",
                key[0],
                key[1],
                item["lunch_break_at"],
            )
    return sorted(
        by_key.values(),
        key=lambda b: (b["day"], b["ring"], b["lunch_break_at"] or time(0, 0)),
    )


def normalize_timetable_breaks(
    raw: list[dict],
    *,
    day_hint: int | None = None,
) -> tuple[list[dict], int]:
    """Validate and normalize raw OpenRouter lunch_break rows.

    Returns (normalized_breaks, deduped_count).
    """
    normalized: list[dict] = []

    for row in raw:
        label = str(row.get("label") or "").strip()
        if not _label_accepted(label):
            continue

        day = row.get("day")
        if not isinstance(day, int) or day < 1:
            day = day_hint
        if not isinstance(day, int) or day < 1:
            continue

        ring_label = _ring_label(str(row.get("ring") or "").strip() or None)
        if not ring_label:
            continue

        lunch_break_at = _parse_start_time(row.get("start_time"))
        if lunch_break_at is None:
            continue

        duration = row.get("duration_mins")
        if not isinstance(duration, int) or duration < 0:
            duration = DEFAULT_LUNCH_BREAK_MINS

        normalized.append(
            {
                "day": day,
                "ring": ring_label,
                "lunch_break_at": lunch_break_at,
                "lunch_break_mins": duration,
                "_label": label,
            }
        )

    pre_dedup = len(normalized)
    deduped = _dedup_normalized_breaks(normalized)
    return deduped, pre_dedup - len(deduped)


def _parse_timetable_response(content: str) -> list[dict]:
    text = content.strip()
    fence = _RE_JSON_FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    payload = json.loads(text)
    breaks = payload.get("lunch_breaks")
    if not isinstance(breaks, list):
        raise OpenRouterParseError("OpenRouter response missing lunch_breaks array")
    return breaks


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

    return _parse_timetable_response(content)


def _extract_single_page_pdf(pdf_data: bytes, page_num: int) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_data))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_num - 1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def extract_lunch_breaks_from_pdf(
    pdf_data: bytes,
    *,
    trial_external_id: str | None = None,
) -> list[dict]:
    """Return [{day, ring, lunch_break_at, lunch_break_mins}, ...] from timetable pages."""
    if not is_openrouter_timetable_enabled():
        log.info(
            "openrouter_timetable: disabled — skipping lunch extraction for trial=%s",
            trial_external_id or "?",
        )
        return []

    api_key = _env("OPENROUTER_API_KEY")
    if not api_key:
        log.warning("openrouter_timetable: OPENROUTER_API_KEY is not configured")
        return []

    page_ranges = detect_timetable_page_ranges(pdf_data)
    if not page_ranges:
        return []

    max_pages = _timetable_max_pages()
    if len(page_ranges) > max_pages:
        log.warning(
            "openrouter_timetable: capping timetable pages from %d to %d",
            len(page_ranges),
            max_pages,
        )
        page_ranges = page_ranges[:max_pages]

    page_nums = [start for start, _end, _hint in page_ranges]
    log.info(
        "openrouter_timetable: trial=%s pages=%s chunks=%d",
        trial_external_id or "?",
        page_nums,
        len(page_ranges),
    )

    all_breaks: list[dict] = []
    try:
        for start_page, _end_page, day_hint in page_ranges:
            chunk_bytes = _extract_single_page_pdf(pdf_data, start_page)
            filename = f"timetable-page-{start_page}.pdf"
            page_range = str(start_page)
            payload = build_timetable_request_payload(
                chunk_bytes,
                filename,
                page_range=page_range,
            )
            raw_breaks = await _call_openrouter(payload, api_key=api_key)
            normalized, deduped = normalize_timetable_breaks(raw_breaks, day_hint=day_hint)
            log.info(
                "openrouter_timetable: chunk page=%s raw_breaks=%d normalized=%d deduped=%d",
                start_page,
                len(raw_breaks),
                len(normalized),
                deduped,
            )
            all_breaks.extend(normalized)
    except (OpenRouterApiError, OpenRouterParseError, json.JSONDecodeError, ValueError) as exc:
        log.warning(
            "openrouter_timetable: extraction failed trial=%s: %s",
            trial_external_id or "?",
            exc,
        )
        return []

    if not all_breaks:
        return []

    return _merge_break_lists(all_breaks)
