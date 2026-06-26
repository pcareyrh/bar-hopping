"""Unit tests for OpenRouter timetable lunch break extraction."""
import asyncio
from datetime import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.openrouter_timetable import (
    TIMETABLE_JSON_SCHEMA,
    detect_timetable_page_ranges,
    extract_lunch_breaks_from_pdf,
    is_openrouter_timetable_enabled,
    normalize_timetable_breaks,
)

_NATIONALS_PDF = Path("/Users/patrick/Downloads/Draft 2026 Agility Nationals Catalogue.pdf")


def _raw_break(**overrides) -> dict:
    base = {
        "day": 2,
        "ring": "3",
        "start_time": "12:35",
        "duration_mins": None,
        "label": "BUILD & WALK & LUNCH",
    }
    base.update(overrides)
    return base


@pytest.mark.skipif(
    not _NATIONALS_PDF.exists(),
    reason=f"Nationals PDF fixture not present at {_NATIONALS_PDF}",
)
def test_detect_timetable_page_ranges_nationals():
    ranges = detect_timetable_page_ranges(_NATIONALS_PDF.read_bytes())
    assert ranges == [(29, 29, 2), (31, 31, 3), (33, 33, 4)]


def test_timetable_json_schema_shape():
    assert TIMETABLE_JSON_SCHEMA["required"] == ["page_summary", "lunch_breaks"]
    items = TIMETABLE_JSON_SCHEMA["properties"]["lunch_breaks"]["items"]
    assert set(items["required"]) >= {"day", "ring", "start_time", "label"}


def test_normalize_ring_label():
    breaks, deduped = normalize_timetable_breaks([_raw_break(ring="RING 3")])
    assert deduped == 0
    assert len(breaks) == 1
    assert breaks[0]["ring"] == "Ring 3"


def test_normalize_time_parsing_and_default_duration():
    breaks, _ = normalize_timetable_breaks([_raw_break(start_time="12:35", duration_mins=None)])
    assert breaks[0]["lunch_break_at"] == time(12, 35)
    assert breaks[0]["lunch_break_mins"] == 45


def test_normalize_day_hint_fallback():
    breaks, _ = normalize_timetable_breaks(
        [_raw_break(day=None, label="LUNCH")],
        day_hint=2,
    )
    assert len(breaks) == 1
    assert breaks[0]["day"] == 2


def test_normalize_duplicate_dedup_keeps_earliest():
    raw = [
        _raw_break(ring="2", start_time="14:10", label="LUNCH"),
        _raw_break(ring="2", start_time="12:50", label="LUNCH"),
    ]
    breaks, deduped = normalize_timetable_breaks(raw)
    assert deduped == 1
    assert len(breaks) == 1
    assert breaks[0]["lunch_break_at"] == time(12, 50)


def test_normalize_label_filter_rejects_walking():
    raw = [_raw_break(ring="1", start_time="11:00", label="WALKING 400", duration_mins=30)]
    breaks, deduped = normalize_timetable_breaks(raw)
    assert breaks == []
    assert deduped == 0


def test_normalize_label_filter_accepts_build_walk_lunch():
    raw = [_raw_break(label="BUILD & WALK & LUNCH")]
    breaks, _ = normalize_timetable_breaks(raw)
    assert len(breaks) == 1


def test_normalize_label_filter_accepts_walk_and_lunch():
    raw = [_raw_break(label="WALK & LUNCH")]
    breaks, _ = normalize_timetable_breaks(raw)
    assert len(breaks) == 1


def test_normalize_ignores_group_2_when_earlier_lunch_exists():
    raw = [
        _raw_break(ring="2", start_time="12:50", label="WALK & LUNCH"),
        _raw_break(ring="2", start_time="14:30", label="WALK & LUNCH GROUP 2"),
    ]
    breaks, deduped = normalize_timetable_breaks(raw)
    assert deduped == 1
    assert len(breaks) == 1
    assert breaks[0]["lunch_break_at"] == time(12, 50)


def test_is_openrouter_timetable_enabled_requires_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_TIMETABLE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    assert is_openrouter_timetable_enabled() is True

    monkeypatch.setenv("OPENROUTER_TIMETABLE_ENABLED", "false")
    assert is_openrouter_timetable_enabled() is False


def test_extract_lunch_breaks_from_pdf_mocked_integration(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_TIMETABLE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    pdf_data = b"%PDF-1.4 test"
    api_breaks = [
        {
            "day": 2,
            "ring": "RING 3",
            "start_time": "12:35",
            "duration_mins": None,
            "label": "BUILD & WALK & LUNCH",
        },
        {
            "day": 2,
            "ring": "2",
            "start_time": "12:50",
            "duration_mins": 45,
            "label": "LUNCH",
        },
    ]

    with patch(
        "app.scraper.openrouter_timetable.detect_timetable_page_ranges",
        return_value=[(29, 29, 2)],
    ), patch(
        "app.scraper.openrouter_timetable._extract_single_page_pdf",
        return_value=b"%PDF-1.4 page",
    ), patch(
        "app.scraper.openrouter_timetable._call_openrouter",
        new_callable=AsyncMock,
        return_value=api_breaks,
    ):
        breaks = asyncio.run(
            extract_lunch_breaks_from_pdf(pdf_data, trial_external_id="nationals-2026")
        )

    assert len(breaks) == 2
    by_ring = {b["ring"]: b for b in breaks}
    assert by_ring["Ring 3"]["lunch_break_at"] == time(12, 35)
    assert by_ring["Ring 2"]["lunch_break_at"] == time(12, 50)
    assert by_ring["Ring 3"]["day"] == 2


def test_extract_lunch_breaks_from_pdf_api_failure_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_TIMETABLE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    from app.scraper.openrouter_catalogue import OpenRouterApiError

    with patch(
        "app.scraper.openrouter_timetable.detect_timetable_page_ranges",
        return_value=[(29, 29, 2)],
    ), patch(
        "app.scraper.openrouter_timetable._extract_single_page_pdf",
        return_value=b"%PDF-1.4 page",
    ), patch(
        "app.scraper.openrouter_timetable._call_openrouter",
        new_callable=AsyncMock,
        side_effect=OpenRouterApiError("HTTP 502"),
    ):
        breaks = asyncio.run(extract_lunch_breaks_from_pdf(b"%PDF-1.4 test"))

    assert breaks == []


def test_extract_lunch_breaks_from_pdf_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TIMETABLE_ENABLED", "false")
    breaks = asyncio.run(extract_lunch_breaks_from_pdf(b"%PDF-1.4 test"))
    assert breaks == []
