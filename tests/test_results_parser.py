"""Smoke tests for app.scraper.results parsing.

Run with `python -m pytest tests/` from the worker image (httpx + bs4 in
requirements.txt). The web image lacks bs4 so this won't run there.
"""
from pathlib import Path

from app.scraper.results import parse_subtrial_html
from app.models import normalise_name, normalise_handler

FIXTURE = Path(__file__).parent / "fixtures" / "results_256.html"


def _runs():
    html = FIXTURE.read_text()
    return parse_subtrial_html(html, sub_trial_id="487", sub_trial_label="Jumping 1")


def test_run_count_and_classes():
    runs = _runs()
    # 4 novice 200 + 1 novice 500 + 1 excellent jumping 300
    assert len(runs) == 6
    assert {r["class_slug"] for r in runs} == {"novice_agility", "excellent_jumping"}


def test_height_groups_split_correctly():
    runs = _runs()
    novice = [r for r in runs if r["class_slug"] == "novice_agility"]
    assert {r["height_group"] for r in novice} == {200, 500}


def test_header_metadata_captured():
    runs = _runs()
    novice_200 = next(r for r in runs if r["class_slug"] == "novice_agility" and r["height_group"] == 200)
    assert novice_200["sct_seconds"] == 50.0
    assert novice_200["course_length_m"] == 130
    assert "Neethling" in novice_200["judge_name"]


def test_clean_run_parsed():
    runs = _runs()
    clement = next(r for r in runs if r["dog_name_raw"].startswith("Adalacia"))
    assert clement["handler_name_raw"] == "Carolyne Fitzpatrick"
    assert clement["time_seconds"] == 35.99
    assert clement["total_faults"] == 0
    assert clement["status"] is None
    assert clement["height_group"] == 200


def test_dog_name_with_hyphen_splits_on_last():
    runs = _runs()
    foo = next(r for r in runs if r["dog_name_raw"].startswith("Foo - Bar"))
    # Last ' - ' separates dog from handler.
    assert foo["dog_name_raw"] == "Foo - Bar Baz"
    assert foo["handler_name_raw"] == "Jane Doe"


def test_disqualified_row():
    runs = _runs()
    dq = next(r for r in runs if r["dog_name_raw"].startswith("Disqualified Dog"))
    assert dq["status"] == "DQ"
    assert dq["time_seconds"] is None
    assert dq["total_faults"] is None


def test_absent_row():
    runs = _runs()
    ab = next(r for r in runs if r["dog_name_raw"].startswith("Absent Dog"))
    assert ab["status"] == "ABS"
    assert ab["time_seconds"] is None


def test_row_index_resets_per_height_group():
    runs = _runs()
    novice_200 = [r for r in runs if r["class_slug"] == "novice_agility" and r["height_group"] == 200]
    assert [r["row_index"] for r in novice_200] == [1, 2, 3, 4]
    novice_500 = [r for r in runs if r["class_slug"] == "novice_agility" and r["height_group"] == 500]
    assert [r["row_index"] for r in novice_500] == [1]


def test_normalise_name_strips_ai_suffix():
    assert normalise_name("Champ (AI)") == "champ"
    assert normalise_name("Champ  (ai) ") == "champ"
    assert normalise_name("O'Reilly's Joy!") == "o reilly s joy"


def test_normalise_handler_falls_back_to_placeholder():
    assert normalise_handler(None) == "-"
    assert normalise_handler("") == "-"
    assert normalise_handler("Jane Doe") == "jane doe"
