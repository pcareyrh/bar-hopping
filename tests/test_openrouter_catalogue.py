"""Unit tests for OpenRouter catalogue extraction and parse_catalogue_pdf_bytes fallback."""
import asyncio
import base64
import io
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.openrouter_catalogue import (
    OpenRouterApiError,
    _absolute_page_range,
    _call_openrouter,
    _extract_chunk_resilient,
    _page_range_start,
    build_request_payload,
    extract_catalogue_from_pdf,
    extraction_timeout_seconds,
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


def test_build_request_payload_requires_model(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        build_request_payload(b"pdf", "cat.pdf")


def test_extraction_timeout_defaults_below_catalogue_job_budget(monkeypatch):
    monkeypatch.delenv("OPENROUTER_EXTRACTION_TIMEOUT", raising=False)

    assert extraction_timeout_seconds() == 600


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


def test_normalize_duplicate_rows_match_database_key():
    raw = [
        _raw_entry(ring_number="Ring 1", height_group=200),
        _raw_entry(ring_number="Ring 2", height_group=300),
    ]
    entries, failures = normalize_openrouter_entries(raw)
    assert failures == 0
    assert len(entries) == 1
    assert entries[0]["ring_number"] == "1"
    assert entries[0]["height_group"] == 200


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


def test_page_range_start_parses_absolute_offset():
    assert _page_range_start("13-24") == 13
    assert _page_range_start(None) == 1


def test_absolute_page_range_uses_document_offset():
    assert _absolute_page_range(13, 0, 6) == "13-18"
    assert _absolute_page_range(13, 6, 12) == "19-24"


def test_extract_chunk_resilient_api_error_fails_fast():
    pdf_data = b"%PDF-1.4 test"

    async def run():
        with patch(
            "app.scraper.openrouter_catalogue._extract_chunk",
            new_callable=AsyncMock,
            side_effect=OpenRouterApiError("Invalid API key"),
        ):
            with pytest.raises(OpenRouterApiError, match="Invalid API key"):
                await _extract_chunk_resilient(
                    pdf_data,
                    "catalogue.pdf",
                    api_key="test-key",
                )

    asyncio.run(run())


def test_call_openrouter_non_json_http_error_is_api_error(monkeypatch):
    class FakeResponse:
        status_code = 502
        text = "<html>bad gateway</html>"

        def json(self):
            raise json.JSONDecodeError("bad", self.text, 0)

    class FakeClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("app.scraper.openrouter_catalogue.httpx.AsyncClient", FakeClient)

    with pytest.raises(OpenRouterApiError, match="HTTP 502"):
        asyncio.run(_call_openrouter({"messages": []}, api_key="test-key"))


def test_extract_chunk_resilient_split_uses_absolute_page_range():
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_data = buf.getvalue()

    call_ranges: list[str | None] = []

    async def fake_extract(
        pdf_data,
        filename,
        *,
        api_key,
        page_range=None,
        state_hint=None,
    ):
        call_ranges.append(page_range)
        if page_range == "13-16":
            raise json.JSONDecodeError("truncated", "", 0)
        return [{"event_name": "Novice Agility (AD1)", "cat_number": "500", "day": 1,
                 "height_group": 500, "run_position": 1, "height_group_total": 1,
                 "nfc": False, "dog_name": "Dog", "handler_name": "Handler", "ring_number": "1"}]

    with patch("app.scraper.openrouter_catalogue._extract_chunk", side_effect=fake_extract):
        entries = asyncio.run(
            _extract_chunk_resilient(
                pdf_data,
                "catalogue-pages-13-16.pdf",
                api_key="test-key",
                page_range="13-16",
            )
        )

    assert len(entries) == 2
    assert "13-14" in call_ranges
    assert "15-16" in call_ranges


def test_normalize_empty_input():
    entries, failures = normalize_openrouter_entries([])
    assert entries == []
    assert failures == 0


def test_extract_catalogue_rejects_more_invalid_than_valid(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    raw_entries = [
        _raw_entry(cat_number="201"),
        _raw_entry(cat_number="", dog_name="Missing cat"),
        _raw_entry(cat_number="202", height_group=150),
    ]

    with patch(
        "app.scraper.openrouter_catalogue.split_pdf_into_chunks",
        return_value=[("catalogue.pdf", b"%PDF-1.4 test", "1-1")],
    ), patch(
        "app.scraper.openrouter_catalogue._extract_chunk_resilient",
        new_callable=AsyncMock,
        return_value=raw_entries,
    ):
        with pytest.raises(ValueError, match="too many invalid entries"):
            asyncio.run(extract_catalogue_from_pdf(b"%PDF-1.4 test"))


def test_extract_catalogue_rejects_empty_chunk(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    chunks = [
        ("catalogue-pages-1-1.pdf", b"one", "1-1"),
        ("catalogue-pages-2-2.pdf", b"two", "2-2"),
    ]

    async def fake_extract(pdf_data, filename, *, api_key, page_range=None, state_hint=None):
        if page_range == "2-2":
            return []
        return [_raw_entry(cat_number="201")]

    with patch(
        "app.scraper.openrouter_catalogue.split_pdf_into_chunks",
        return_value=chunks,
    ), patch(
        "app.scraper.openrouter_catalogue._extract_chunk_resilient",
        side_effect=fake_extract,
    ):
        with pytest.raises(ValueError, match="no valid entries.*2-2"):
            asyncio.run(extract_catalogue_from_pdf(b"%PDF-1.4 test"))


def test_extract_catalogue_passes_previous_chunk_hint(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    monkeypatch.setenv("OPENROUTER_MAX_CONCURRENCY", "2")
    chunks = [
        ("catalogue-pages-1-1.pdf", b"one", "1-1"),
        ("catalogue-pages-2-2.pdf", b"two", "2-2"),
        ("catalogue-pages-3-3.pdf", b"three", "3-3"),
    ]
    active = 0
    max_active = 0
    calls = []

    async def fake_extract(pdf_data, filename, *, api_key, page_range=None, state_hint=None):
        nonlocal active, max_active
        calls.append((page_range, state_hint))
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [
            _raw_entry(
                cat_number=page_range.replace("-", ""),
                event_name=f"Event {page_range}",
                ring_number=f"Ring {page_range}",
            )
        ]

    with patch(
        "app.scraper.openrouter_catalogue.split_pdf_into_chunks",
        return_value=chunks,
    ), patch(
        "app.scraper.openrouter_catalogue._extract_chunk_resilient",
        side_effect=fake_extract,
    ):
        entries = asyncio.run(extract_catalogue_from_pdf(b"%PDF-1.4 test"))

    assert max_active == 1
    assert calls == [
        ("1-1", None),
        ("2-2", "day=1, event_name=Event 1-1, height_group=200, ring_number=Ring 1-1"),
        ("3-3", "day=1, event_name=Event 2-2, height_group=200, ring_number=Ring 2-2"),
    ]
    assert [e["cat_number"] for e in entries] == ["11", "22", "33"]


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


def test_parse_catalogue_pdf_bytes_partial_openrouter_falls_back(monkeypatch):
    monkeypatch.setenv("OPENROUTER_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    legacy_entries = [{"event_name": "Fallback", "cat_number": "301"}]
    raw_entries = [
        _raw_entry(cat_number="201"),
        _raw_entry(cat_number=""),
        _raw_entry(cat_number="202", height_group=150),
    ]
    pdf_data = b"%PDF-1.4 test"

    with patch(
        "app.scraper.openrouter_catalogue.split_pdf_into_chunks",
        return_value=[("catalogue.pdf", pdf_data, "1-1")],
    ), patch(
        "app.scraper.openrouter_catalogue._extract_chunk_resilient",
        new_callable=AsyncMock,
        return_value=raw_entries,
    ), patch(
        "app.scraper.catalogue.parse_catalogue_pdf", return_value=legacy_entries
    ) as mock_legacy:
        result = asyncio.run(parse_catalogue_pdf_bytes(pdf_data))

    assert result == legacy_entries
    mock_legacy.assert_called_once_with(pdf_data)
