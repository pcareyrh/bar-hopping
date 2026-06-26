"""Tests for live polling helpers in app/worker.py."""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.live_tracking import deserialize_ring_snapshots, serialize_ring_snapshots
from app.models import CatalogueEntry, Trial
from app.worker import (
    _derive_live_status,
    _enqueue_live_poll,
    _live_trial_day,
    _store_live_ring_snapshots,
    _load_live_ring_snapshots,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_derive_live_status_not_running_stays_live():
    rings = [
        {"status": "Complete"},
        {"status": "Not Running"},
    ]
    assert _derive_live_status(rings) == "live"


def test_derive_live_status_all_complete_is_done():
    rings = [{"status": "Complete"}, {"status": "Complete"}]
    assert _derive_live_status(rings) == "done"


def test_derive_live_status_empty_is_done():
    assert _derive_live_status([]) == "done"


def test_live_trial_day_clamped_to_trial_span(db, monkeypatch):
    monkeypatch.setattr("app.worker.date", type("D", (), {"today": staticmethod(lambda: date(2026, 7, 5))})())
    trial = Trial(
        external_id="md",
        name="Multi",
        start_date=date(2026, 6, 28),
        end_date=date(2026, 6, 30),
    )
    db.add(trial)
    db.flush()
    db.add(CatalogueEntry(
        trial_id=trial.id,
        day=2,
        event_name="Novice Agility",
        height_group=400,
        height_group_total=10,
        cat_number="1",
        run_position=1,
    ))
    db.commit()

    assert _live_trial_day(trial, db) == 2


def test_store_and_load_ring_snapshots_with_datetime(monkeypatch):
    stored = {}

    class FakeRedis:
        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("app.worker.get_redis", lambda: FakeRedis())

    snapshots = {
        "351": {
            "ring_id": "351",
            "ring_number": "1",
            "event_name": "Novice Agility",
            "height_group": 400,
            "status": "Height Change",
            "updated": datetime(2026, 6, 28, 12, 0),
            "pause_started_at": datetime(2026, 6, 28, 12, 5),
        }
    }
    _store_live_ring_snapshots(42, snapshots)
    loaded = _load_live_ring_snapshots(42)

    assert loaded["351"]["event_name"] == "Novice Agility"
    assert loaded["351"]["updated"] == datetime(2026, 6, 28, 12, 0)
    assert loaded["351"]["pause_started_at"] == datetime(2026, 6, 28, 12, 5)


def test_serialize_roundtrip_preserves_pause_state():
    snapshots = {
        "1": {
            "status": "Not Running",
            "updated": datetime(2026, 6, 28, 9, 0),
            "pause_started_at": datetime(2026, 6, 28, 9, 30),
        }
    }
    restored = deserialize_ring_snapshots(serialize_ring_snapshots(snapshots))
    assert restored["1"]["pause_started_at"] == datetime(2026, 6, 28, 9, 30)


def test_enqueue_live_poll_uses_stable_job_id(monkeypatch):
    calls = []

    class FakeQueue:
        def enqueue(self, func_path, trial_id, **kwargs):
            calls.append(("enqueue", func_path, trial_id, kwargs))

        def enqueue_in(self, delta, func_path, trial_id, **kwargs):
            calls.append(("enqueue_in", delta, func_path, trial_id, kwargs))

    monkeypatch.setattr("app.worker.get_queue", lambda: FakeQueue())

    _enqueue_live_poll(7)
    assert calls[0][3]["job_id"] == "live_poll:7"

    _enqueue_live_poll(7, delay_seconds=45)
    assert calls[1][0] == "enqueue_in"
    assert calls[1][1] == timedelta(seconds=45)
    assert calls[1][-1]["job_id"] == "live_poll:7"


def test_enqueue_live_poll_ignores_duplicate(monkeypatch):
    from rq.exceptions import DuplicateJobError

    class FakeQueue:
        def enqueue(self, *args, **kwargs):
            raise DuplicateJobError("exists")

    monkeypatch.setattr("app.worker.get_queue", lambda: FakeQueue())
    _enqueue_live_poll(99)
