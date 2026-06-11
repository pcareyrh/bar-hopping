"""Tests for multi-day schedule parsing and day-aware class matching."""
from datetime import time

import pytest

from app.scraper.schedule import _parse_schedule_text


def test_explicit_day_headers_tag_each_class():
    text = """
    Day 1
    Ring 1
    8:00 AM Masters Agility
    9:30 AM Masters Jumping
    Day 2
    Ring 1
    9:00 AM Masters Agility
    """
    rows = _parse_schedule_text(text)
    assert [(r["day"], r["class_name"], r["scheduled_start"]) for r in rows] == [
        (1, "Masters Agility", time(8, 0)),
        (1, "Masters Jumping", time(9, 30)),
        (2, "Masters Agility", time(9, 0)),
    ]


def test_weekday_headers_assigned_sequential_day_numbers():
    text = """
    Saturday 23 June
    Ring 2
    08:00 Excellent Agility
    Sunday 24 June
    Ring 2
    08:30 Excellent Agility
    """
    rows = _parse_schedule_text(text)
    assert [(r["day"], r["scheduled_start"]) for r in rows] == [
        (1, time(8, 0)),
        (2, time(8, 30)),
    ]


def test_no_day_header_yields_day_none():
    """Single-day schedules with no day header default to day=None (any day)."""
    text = """
    Ring 1
    8:00 AM Masters Agility
    """
    rows = _parse_schedule_text(text)
    assert len(rows) == 1
    assert rows[0]["day"] is None


def test_match_class_schedule_prefers_matching_day():
    fastapi = pytest.importorskip("fastapi")  # router pulls in fastapi  # noqa: F841
    from types import SimpleNamespace as S

    from app.routers.schedule import _match_class_schedule

    sched = [
        S(class_name="Masters Agility", day=1, scheduled_start=time(8, 0)),
        S(class_name="Masters Agility", day=2, scheduled_start=time(9, 0)),
    ]
    assert _match_class_schedule(sched, "Masters Agility", 1).scheduled_start == time(8, 0)
    assert _match_class_schedule(sched, "Masters Agility", 2).scheduled_start == time(9, 0)
    # A day the class isn't scheduled on must not borrow another day's time.
    assert _match_class_schedule(sched, "Masters Agility", 3) is None


def test_match_class_schedule_day_none_is_backward_compatible():
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from types import SimpleNamespace as S

    from app.routers.schedule import _match_class_schedule

    # Legacy rows with day=None apply to any day.
    legacy = [S(class_name="Masters Agility", day=None, scheduled_start=time(8, 0))]
    assert _match_class_schedule(legacy, "Masters Agility", 1).scheduled_start == time(8, 0)
    assert _match_class_schedule(legacy, "Masters Agility", 5).scheduled_start == time(8, 0)
    # Substring containment still works alongside day filtering.
    sub = [S(class_name="Agility", day=1, scheduled_start=time(8, 0))]
    assert _match_class_schedule(sub, "Masters Agility", 1).scheduled_start == time(8, 0)
