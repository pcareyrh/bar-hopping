"""Tests for app/scraper/my_day.py parsers."""
import pathlib
from datetime import time

import pytest

from app.scraper.my_day import parse_my_day_index, parse_my_day_detail

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# parse_my_day_index
# ---------------------------------------------------------------------------

class TestParseMyDayIndex:
    def setup_method(self):
        self.sessions = parse_my_day_index(load("my_day_index.html"))

    def test_returns_two_sessions(self):
        assert len(self.sessions) == 2

    def test_saturday_session_label(self):
        assert "Saturday" in self.sessions[0]["day_label"]

    def test_saturday_start_time(self):
        assert self.sessions[0]["start_time"] == time(8, 0)

    def test_sunday_no_start_time(self):
        assert self.sessions[1]["start_time"] is None

    def test_saturday_has_two_rings(self):
        assert len(self.sessions[0]["rings"]) == 2

    def test_ring1_name(self):
        assert self.sessions[0]["rings"][0]["ring_name"] == "Ring 1"

    def test_ring1_has_two_classes(self):
        assert len(self.sessions[0]["rings"][0]["classes"]) == 2

    def test_ring2_has_one_class(self):
        assert len(self.sessions[0]["rings"][1]["classes"]) == 1

    def test_is_mine_detected(self):
        cls = self.sessions[0]["rings"][0]["classes"][0]
        assert cls["is_mine"] is True

    def test_not_mine_detected(self):
        cls = self.sessions[0]["rings"][0]["classes"][1]
        assert cls["is_mine"] is False

    def test_class_name_uses_long_label(self):
        cls = self.sessions[0]["rings"][0]["classes"][0]
        assert cls["class_name"] == "Novice Agility"

    def test_class_id_extracted(self):
        cls = self.sessions[0]["rings"][0]["classes"][0]
        assert cls["class_id"] == "10"

    def test_day_id_extracted(self):
        cls = self.sessions[0]["rings"][0]["classes"][0]
        assert cls["day_id"] == "5"

    def test_ring_id_from_query_string(self):
        ring = self.sessions[0]["rings"][0]
        assert ring["ring_id"] == "3"

    def test_sunday_ring_class_id(self):
        cls = self.sessions[1]["rings"][0]["classes"][0]
        assert cls["class_id"] == "30"

    def test_empty_html_returns_empty_list(self):
        assert parse_my_day_index("<html><body></body></html>") == []

    def test_container_missing_returns_empty_list(self):
        assert parse_my_day_index("<div id='other'>content</div>") == []


# ---------------------------------------------------------------------------
# parse_my_day_detail
# ---------------------------------------------------------------------------

class TestParseMyDayDetail:
    def setup_method(self):
        self.entries = parse_my_day_detail(load("my_day_detail.html"))

    def test_returns_four_unique_entries(self):
        # 201, 202, 203NFC, 301 — duplicate entry-101 skipped, ABC skipped
        assert len(self.entries) == 4

    def test_first_entry_cat_number(self):
        assert self.entries[0]["cat_number"] == "201"

    def test_first_entry_dog_name(self):
        assert self.entries[0]["dog_name"] == "Buddy"

    def test_handler_dot_prefix_stripped(self):
        assert self.entries[0]["handler_name"] == "John Smith"

    def test_height_group_from_cat_number(self):
        assert self.entries[0]["height_group"] == 200
        assert self.entries[3]["height_group"] == 300

    def test_nfc_false_for_normal_entry(self):
        assert self.entries[0]["nfc"] is False

    def test_nfc_true_for_nfc_entry(self):
        nfc_entry = next(e for e in self.entries if e["cat_number"] == "203NFC")
        assert nfc_entry["nfc"] is True

    def test_nfc_dog_name_parsed(self):
        nfc_entry = next(e for e in self.entries if e["cat_number"] == "203NFC")
        assert nfc_entry["dog_name"] == "Rocket"

    def test_duplicate_entry_id_deduplicated(self):
        buddies = [e for e in self.entries if e["dog_name"] == "Buddy"]
        assert len(buddies) == 1

    def test_invalid_cat_number_skipped(self):
        ghosts = [e for e in self.entries if e["dog_name"] == "Ghost"]
        assert len(ghosts) == 0

    def test_300_height_entry_present(self):
        three_hundreds = [e for e in self.entries if e["height_group"] == 300]
        assert len(three_hundreds) == 1
        assert three_hundreds[0]["dog_name"] == "Max"

    def test_empty_html_returns_empty_list(self):
        assert parse_my_day_detail("<html><body></body></html>") == []


# ---------------------------------------------------------------------------
# parse_my_day_index — no-ring fallback (ring in header, no .my-day-ring-name)
# ---------------------------------------------------------------------------

class TestParseMyDayIndexNoRing:
    """When .my-day-ring-name is absent, ring should be inferred from header."""

    def setup_method(self):
        self.sessions = parse_my_day_index(load("my_day_index_no_ring.html"))

    def test_returns_two_sessions(self):
        assert len(self.sessions) == 2

    def test_ring1_from_header(self):
        assert self.sessions[0]["rings"][0]["ring_name"] == "Ring 1"

    def test_ring2_from_header(self):
        assert self.sessions[1]["rings"][0]["ring_name"] == "Ring 2"

    def test_ring1_classes(self):
        classes = self.sessions[0]["rings"][0]["classes"]
        assert len(classes) == 2
        assert classes[0]["class_name"] == "Novice Agility"
        assert classes[1]["class_name"] == "Masters Agility"

    def test_ring2_class_is_mine(self):
        cls = self.sessions[1]["rings"][0]["classes"][0]
        assert cls["class_name"] == "Masters Jumping"
        assert cls["is_mine"] is True
