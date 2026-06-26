"""Tests for app/live_tracking.py event boundary transitions."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.live_tracking import (
    _to_naive_aest,
    apply_ring_snapshots,
    deserialize_ring_snapshots,
    serialize_ring_snapshots,
)
from app.models import Base, Trial, EventLiveTiming, EventDurationStat


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _ring(ring_id, ring_number, event_name, height, status, updated):
    return {
        "ring_id": ring_id,
        "ring_number": ring_number,
        "event_name": event_name,
        "height_group": height,
        "status": status,
        "updated": updated,
    }


class TestApplyRingSnapshots:
    def test_running_segment_gets_started_at(self, db):
        trial = Trial(external_id="1307", name="Test")
        db.add(trial)
        db.commit()

        t0 = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
        rings = [_ring("351", "1", "Novice Agility", 400, "Running", t0)]
        prev = apply_ring_snapshots(db, trial.id, 1, {}, rings, t0)
        db.commit()

        row = db.query(EventLiveTiming).one()
        assert row.started_at == datetime(2026, 6, 25, 18, 0)
        assert row.start_confidence == "low"
        assert row.finished_at is None
        assert prev["351"]["event_name"] == "Novice Agility"

    def test_complete_closes_segment_with_duration(self, db):
        trial = Trial(external_id="1307", name="Test")
        db.add(trial)
        db.commit()

        t0 = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 6, 25, 8, 45, tzinfo=timezone.utc)
        ring = _ring("351", "1", "Novice Agility", 400, "Running", t0)
        prev = apply_ring_snapshots(db, trial.id, 1, {}, [ring], t0)
        db.commit()

        ring["status"] = "Complete"
        ring["updated"] = t1
        apply_ring_snapshots(db, trial.id, 1, prev, [ring], t1)
        db.commit()

        row = db.query(EventLiveTiming).one()
        assert row.finished_at == datetime(2026, 6, 25, 18, 45)
        assert row.duration_s == 45 * 60

        stat = db.query(EventDurationStat).one()
        assert stat.median_duration_s == 45 * 60
        assert stat.sample_count == 1

    def test_class_change_closes_old_opens_new(self, db):
        trial = Trial(external_id="1307", name="Test")
        db.add(trial)
        db.commit()

        t0 = datetime(2026, 6, 25, 9, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
        ring = _ring("351", "1", "Novice Agility", 400, "Running", t0)
        prev = apply_ring_snapshots(db, trial.id, 1, {}, [ring], t0)
        db.commit()

        new_ring = _ring("351", "1", "Novice Jumping", 400, "Running", t1)
        apply_ring_snapshots(db, trial.id, 1, prev, [new_ring], t1)
        db.commit()

        rows = db.query(EventLiveTiming).order_by(EventLiveTiming.id).all()
        assert len(rows) == 2
        assert rows[0].event_name == "Novice Agility"
        assert rows[0].finished_at == datetime(2026, 6, 25, 20, 0)
        assert rows[1].event_name == "Novice Jumping"
        assert rows[1].started_at == datetime(2026, 6, 25, 20, 0)
        assert rows[1].start_confidence == "high"


class TestLiveTrackingHelpers:
    def test_running_does_not_reset_existing_started_at_when_prev_missing(self, db):
        trial = Trial(external_id="1307", name="Test")
        db.add(trial)
        db.commit()

        existing_start = datetime(2026, 6, 25, 17, 30)
        row = EventLiveTiming(
            trial_id=trial.id,
            day=1,
            ring_id="351",
            ring_number="1",
            event_name="Novice Agility",
            height_group=400,
            status="Running",
            started_at=existing_start,
            start_confidence="high",
            pause_s=0,
        )
        db.add(row)
        db.commit()

        t0 = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
        ring = _ring("351", "1", "Novice Agility", 400, "Running", t0)
        apply_ring_snapshots(db, trial.id, 1, {}, [ring], t0)
        db.commit()

        row = db.query(EventLiveTiming).one()
        assert row.started_at == existing_start

    def test_to_naive_aest_converts_utc_to_aest(self):
        utc = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
        assert _to_naive_aest(utc) == datetime(2026, 6, 25, 18, 0)

    def test_serialize_deserialize_ring_snapshots_roundtrip(self):
        updated = datetime(2026, 6, 25, 18, 0)
        pause_started = datetime(2026, 6, 25, 18, 30)
        snapshots = {
            "351": {
                "ring_id": "351",
                "ring_number": "1",
                "event_name": "Novice Agility",
                "status": "Running",
                "updated": updated
            }
        }
        snapshots["351"]["pause_started_at"] = pause_started

        raw = serialize_ring_snapshots(snapshots)
        restored = deserialize_ring_snapshots(raw)

        assert restored["351"]["updated"] == updated
        assert restored["351"]["pause_started_at"] == pause_started
        assert restored["351"]["event_name"] == "Novice Agility"
