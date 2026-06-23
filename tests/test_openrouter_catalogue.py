"""Unit tests for OpenRouter catalogue extraction and parse_catalogue_pdf_bytes fallback."""
import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.openrouter_catalogue import (
    build_request_payload,
    normalize_openrouter_entries,
    _normalize_event_name,
)
from app.scraper.catalogue import parse_catalogue_pdf_bytes


def _raw_entry(**overrides) -> dict:
    base = {
        "event_name": "Novice Agility",
        "cat_number": "201",
        "day": 1,
        "height_group": 200,
        "run_position": 99,
        "height_group_total": 99,
        "nfc": False,
        "dog_name": "Pippi",
        "handler_name": "Ally Elizabeth",
        "ring_number": "Ring 1",
    }
    base.update(overrides)
    return base


def test_build_request_payload_includes_base64_pdf(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    pdf_data = b"%PDF-1.4 minimal test bytes"
    payload = build_request_payload(pdf_data, "catalogue.pdf")

    file_block = payload["messages"][0]["content"][1]
    assert file_block["type"] == "file"
    file_data = file_block["file"]["file_data"]
    assert file_data.startswith("data:application/pdf;base64,")
    b64_part = file_data.split(",", 1)[1]
    assert base64.b64decode(b64_part) == pdf_data
    assert file_block["file"]["filename"] == "catalogue.pdf"
    assert payload["model"] == "google/gemini-2.5-flash"


def test_build_request_payload_strict_json_schema(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    payload = build_request_payload(b"pdf", "cat.pdf")

    rf = payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "topdog_catalogue"
    schema = rf["json_schema"]["schema"]
    assert schema["required"] == ["entries"]
    assert schema["properties"]["entries"]["type"] == "array"


def test_build_request_payload_file_parser_plugin(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setenv("OPENROUTER_PDF_ENGINE", "mistral-ocr")
    payload = build_request_payload(b"pdf", "cat.pdf")

    plugins = payload["plugins"]
    file_parser = next(p for p in plugins if p["id"] == "file-parser")
    assert file_parser["pdf"]["engine"] == "mistral-ocr"
    assert payload["max_tokens"] == 32768


def test_build_request_payload_requires_model():
    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        build_request_payload(b"pdf", "cat.pdf")


def test_normalize_valid_entries():
    raw = [_raw_entry()]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert len(entries) == 1
    e = entries[0]
    assert e["event_name"] == "Novice Agility"
    assert e["cat_number"] == "201"
    assert e["day"] == 1
    assert e["height_group"] == 200
    assert e["run_position"] == 1
    assert e["height_group_total"] == 1
    assert e["nfc"] is False
    assert e["dog_name"] == "Pippi"
    assert e["handler_name"] == "Ally Elizabeth"
    assert e["ring_number"] == "1"


def test_normalize_invalid_height_rejected():
    raw = [
        _raw_entry(height_group=150),
        _raw_entry(cat_number="202"),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 1
    assert len(entries) == 1
    assert entries[0]["cat_number"] == "202"


def test_normalize_duplicate_rows_dropped():
    raw = [
        _raw_entry(),
        _raw_entry(),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert len(entries) == 1


def test_normalize_run_position_recomputed_within_group():
    raw = [
        _raw_entry(cat_number="201", run_position=5),
        _raw_entry(cat_number="202", run_position=1),
        _raw_entry(cat_number="203", run_position=9),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    by_cat = {e["cat_number"]: e for e in entries}
    assert by_cat["201"]["run_position"] == 1
    assert by_cat["202"]["run_position"] == 2
    assert by_cat["203"]["run_position"] == 3


def test_normalize_run_position_separate_groups():
    raw = [
        _raw_entry(cat_number="201", ring_number="Ring 1"),
        _raw_entry(cat_number="202", ring_number="Ring 2"),
        _raw_entry(cat_number="203", event_name="Open Agility", ring_number="Ring 1"),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    by_cat = {e["cat_number"]: e for e in entries}
    assert by_cat["201"]["run_position"] == 1
    assert by_cat["202"]["run_position"] == 1
    assert by_cat["203"]["run_position"] == 1


def test_normalize_height_group_total_excludes_nfc():
    raw = [
        _raw_entry(cat_number="201", nfc=False),
        _raw_entry(cat_number="202NFC", nfc=True),
        _raw_entry(cat_number="203", nfc=False),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert all(e["height_group_total"] == 2 for e in entries)
    nfc_entry = next(e for e in entries if e["cat_number"] == "202NFC")
    assert nfc_entry["nfc"] is True
    assert nfc_entry["run_position"] == 2


def test_normalize_same_cat_number_different_days_allowed():
    raw = [
        _raw_entry(cat_number="500", day=1),
        _raw_entry(cat_number="500", day=2),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert len(entries) == 2
    days = sorted(e["day"] for e in entries)
    assert days == [1, 2]


def test_normalize_nfc_from_cat_number_suffix():
    raw = [_raw_entry(cat_number="301NFC", nfc=False)]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert entries[0]["nfc"] is True


def test_normalize_nfc_suffix_case_insensitive():
    raw = [_raw_entry(cat_number="301nfc", nfc=False)]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert entries[0]["nfc"] is True


def test_normalize_ring_number_to_bare_digit():
    raw = [
        _raw_entry(ring_number="Ring 7"),
        _raw_entry(cat_number="202", ring_number="2"),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    by_cat = {e["cat_number"]: e for e in entries}
    assert by_cat["201"]["ring_number"] == "7"
    assert by_cat["202"]["ring_number"] == "2"


def test_normalize_event_name_from_bare_code():
    assert _normalize_event_name("AD1") == "Novice Agility (AD1)"
    assert _normalize_event_name("JDO") == "Open Jumping"
    assert _normalize_event_name("Agility Dog (AD2)") == "Novice Agility (AD2)"


def test_normalize_empty_input():
    entries, failures = normalize_openrouter_entries([])
    assert entries == []
    assert failures == 0


def test_parse_catalogue_pdf_bytes_openrouter_disabled_uses_legacy(monkeypatch):
    legacy_entries = [{"event_name": "Legacy", "cat_number": "201"}]
    pdf_data = b"%PDF-1.4 test"

    with patch(
        "app.scraper.openrouter_catalogue.is_openrouter_enabled", return_value=False
    ), patch(
        "app.scraper.catalogue.parse_catalogue_pdf", return_value=legacy_entries
    ) as mock_legacy:
        result = asyncio.run(parse_catalogue_pdf_bytes(pdf_data))

    assert result == legacy_entries
    mock_legacy.assert_called_once_with(pdf_data)


def test_parse_catalogue_pdf_bytes_openrouter_failure_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    legacy_entries = [{"event_name": "Fallback", "cat_number": "301"}]
    pdf_data = b"%PDF-1.4 test"

    with patch(
        "app.scraper.openrouter_catalogue.extract_catalogue_from_pdf",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API down"),
    ), patch(
        "app.scraper.catalogue.parse_catalogue_pdf", return_value=legacy_entries
    ) as mock_legacy:
        result = asyncio.run(parse_catalogue_pdf_bytes(pdf_data))

    assert result == legacy_entries
    mock_legacy.assert_called_once_with(pdf_data)
