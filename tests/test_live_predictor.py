"""Tests for live event-anchored prediction."""
from datetime import datetime

from app.engine.predictor import predict_run_from_event, format_predicted_time


def test_predict_run_from_event_position_one():
    start = datetime(2026, 6, 25, 9, 0)
    pred = predict_run_from_event(
        event_started_at=start,
        run_position=1,
        avg_time_per_dog=90,
    )
    assert pred["predicted_start"] == start
    assert pred["first_run_start"] == start


def test_predict_run_from_event_position_four():
    start = datetime(2026, 6, 25, 9, 0)
    pred = predict_run_from_event(
        event_started_at=start,
        run_position=4,
        avg_time_per_dog=90,
    )
    assert pred["predicted_start"] == datetime(2026, 6, 25, 9, 4, 30)
    assert format_predicted_time(pred["predicted_start"]) == "9:04 AM"


def test_predict_run_from_event_respects_overrides():
    start = datetime(2026, 6, 25, 9, 0)
    pred = predict_run_from_event(
        event_started_at=start,
        run_position=10,
        avg_time_per_dog=90,
        position_override=2,
        time_per_dog_override=60,
    )
    assert pred["effective_position"] == 2
    assert pred["effective_tpd"] == 60
    assert pred["predicted_start"] == datetime(2026, 6, 25, 9, 1)
