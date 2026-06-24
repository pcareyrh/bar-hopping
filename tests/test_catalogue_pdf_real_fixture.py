"""End-to-end regression test for the legacy pdfplumber catalogue parser using a
real (anonymised) TopDog catalogue PDF.

The fixture `Final_Catalogue_upload_v2_ANONYMISED.pdf` is the ADC Pawlympics
catalogue (Format E: "SATURDAY - RING 1 - AM" headers with the class code on the
following line). It exercises `parse_catalogue_pdf` against a full 38-page,
multi-day, multi-ring document — the kind of file users upload via the
"Upload catalogue" flow.

This test only covers the local, deterministic legacy parser; the OpenRouter
path requires a live API key and is covered separately by mocked unit tests in
`tests/test_openrouter_catalogue.py`.
"""
import asyncio
import pathlib
from collections import Counter

import pytest

from app.scraper.catalogue import parse_catalogue_pdf, parse_catalogue_pdf_bytes

# The PDF lives at the repo root (committed as test data). Skip cleanly if a
# checkout doesn't have it, so the suite never hard-fails on its absence.
_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "Final_Catalogue_upload_v2_ANONYMISED.pdf"
_PLAN = pathlib.Path(__file__).resolve().parents[1] / "docs" / "remove-local-pdf-processing-plan.md"

pytestmark = pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason=f"catalogue PDF fixture not present at {_FIXTURE}",
)

VALID_HEIGHTS = {200, 300, 400, 500, 600}


@pytest.fixture(scope="module")
def entries() -> list[dict]:
    return parse_catalogue_pdf(_FIXTURE.read_bytes())


def test_removal_plan_matches_anonymized_pdf_fallback_path(monkeypatch):
    """The removal plan must account for the real fixture-backed local fallback."""
    monkeypatch.delenv("OPENROUTER_ENABLED", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    fallback_entries = asyncio.run(
        parse_catalogue_pdf_bytes(
            _FIXTURE.read_bytes(),
            filename=_FIXTURE.name,
            trial_external_id="anonymous-fixture",
        )
    )

    assert len(fallback_entries) == 900

    plan = _PLAN.read_text()
    assert "Remove the legacy `pdfplumber` catalogue parser" in plan
    assert "`tests/test_catalogue_pdf_real_fixture.py`" in plan
    assert "`pdfplumber==0.11.4`" in plan
    assert "`pypdf==6.14.0`" in plan
    assert "Safe to remove with only catalogue fallback removal? | Required action before removal" in plan


def test_parses_expected_entry_count(entries):
    # Pins the legacy parser's current output for this catalogue.
    assert len(entries) == 900


def test_spans_both_days(entries):
    assert {e["day"] for e in entries} == {1, 2}


def test_all_heights_valid(entries):
    assert {e["height_group"] for e in entries} <= VALID_HEIGHTS
    # Every height group present in this catalogue.
    assert {e["height_group"] for e in entries} == VALID_HEIGHTS


def test_expected_events_present(entries):
    events = {e["event_name"] for e in entries}
    assert len(events) == 20
    # Format E maps session codes (AD1/AD2/AD3 etc.) to canonical names.
    for expected in (
        "Novice Agility (AD1)",
        "Masters Jumping (JDM1)",
        "Open Agility",
        "Open Jumping",
    ):
        assert expected in events, f"missing event {expected!r}"


def test_run_positions_contiguous_per_group(entries):
    """Within each (day, event, height, ring) group run_position must start at 1
    and increase by 1 with no gaps — the core guarantee the scheduler relies on."""
    groups: dict[tuple, list[int]] = {}
    for e in entries:
        key = (e["day"], e["event_name"], e["height_group"], e.get("ring_number"))
        groups.setdefault(key, []).append(e["run_position"])

    for key, positions in groups.items():
        assert sorted(positions) == list(range(1, len(positions) + 1)), \
            f"non-contiguous run positions for {key}: {sorted(positions)}"


def test_height_group_total_matches_non_nfc_count(entries):
    """height_group_total should equal the number of non-NFC entries in the group."""
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        key = (e["day"], e["event_name"], e["height_group"], e.get("ring_number"))
        groups.setdefault(key, []).append(e)

    for key, group in groups.items():
        non_nfc = sum(1 for e in group if not e["nfc"])
        for e in group:
            assert e["height_group_total"] == non_nfc, \
                f"height_group_total mismatch for {key}"


def test_cat_numbers_present(entries):
    # Every row must carry a catalogue number (used to match user entries).
    assert all(e["cat_number"] for e in entries)
