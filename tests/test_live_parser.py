"""Tests for app/scraper/live.py parsers."""
import pathlib
from datetime import datetime, timezone

import pytest

from app.scraper.live import parse_class_name, parse_ring_status

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestParseClassName:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Excellent Gamblers (400)", ("Excellent Gamblers", 400)),
            ("Novice Agility (400)", ("Novice Agility", 400)),
            ("Novice Jumping (600)", ("Novice Jumping", 600)),
            ("Masters Agility", ("Masters Agility", None)),
            ("  Open Jumping (500)  ", ("Open Jumping", 500)),
            ("", ("", None)),
        ],
    )
    def test_parse_class_name(self, text, expected):
        assert parse_class_name(text) == expected


class TestParseRingStatus:
    def setup_method(self):
        self.rings = parse_ring_status(load("live_board.html"))

    def test_returns_two_rings(self):
        assert len(self.rings) == 2

    def test_ring_351_identity(self):
        ring = self.rings[0]
        assert ring["ring_id"] == "351"
        assert ring["ring_number"] == "1"

    def test_ring_351_event(self):
        ring = self.rings[0]
        assert ring["event_name"] == "Novice Agility"
        assert ring["height_group"] == 400

    def test_ring_351_status(self):
        assert self.rings[0]["status"] == "Running"

    def test_ring_351_updated(self):
        updated = self.rings[0]["updated"]
        assert updated == datetime(2026, 6, 25, 4, 4, 26, tzinfo=timezone.utc)

    def test_ring_352_identity(self):
        ring = self.rings[1]
        assert ring["ring_id"] == "352"
        assert ring["ring_number"] == "2"

    def test_ring_352_event(self):
        ring = self.rings[1]
        assert ring["event_name"] == "Novice Jumping"
        assert ring["height_group"] == 400

    def test_ring_352_status(self):
        assert self.rings[1]["status"] == "Complete"

    def test_ring_352_updated(self):
        updated = self.rings[1]["updated"]
        assert updated == datetime(2026, 6, 25, 4, 18, 10, tzinfo=timezone.utc)

    def test_empty_html_returns_empty_list(self):
        assert parse_ring_status("<html><body></body></html>") == []
