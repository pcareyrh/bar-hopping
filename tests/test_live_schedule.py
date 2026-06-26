"""Tests for live timing lookup against coded catalogue event names."""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.friends import build_friend_predictions
from app.models import (
    CatalogueEntry,
    EventLiveTiming,
    Session,
    SessionFriend,
    Trial,
    friend_pin_key,
)
from app.routers.schedule import (
    _compute_catalogue_blocks,
    _event_timings_for_trial,
    _find_event_timing,
    predict_catalogue_entry,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed_trial(db):
    session = Session(uuid="live-test")
    trial = Trial(
        external_id="t-live",
        name="Live Test",
        start_date=date(2026, 6, 28),
        start_time=time(9, 0),
    )
    db.add_all([session, trial])
    db.flush()
    return session, trial


def test_predict_catalogue_entry_matches_coded_name_to_live_timing(db):
    session, trial = _seed_trial(db)
    started = datetime(2026, 6, 28, 10, 15)
    db.add(
        EventLiveTiming(
            trial_id=trial.id,
            day=1,
            ring_id="351",
            ring_number="1",
            event_name="Masters Agility",
            height_group=500,
            started_at=started,
            finished_at=None,
            status="Running",
        )
    )
    ce = CatalogueEntry(
        trial_id=trial.id,
        day=1,
        event_name="Masters Agility (ADM1)",
        height_group=500,
        height_group_total=12,
        cat_number="410",
        dog_name="Fika",
        handler_name="Jane Smith",
        run_position=3,
        ring_number="1",
    )
    db.add(ce)
    db.commit()

    event_timings = _event_timings_for_trial(trial, db)
    pred = predict_catalogue_entry(
        ce=ce,
        session=session,
        trial=trial,
        all_class_schedules=[],
        block_starts={},
        entry_id=1,
        event_timings=event_timings,
        live_enabled=True,
    )

    assert pred["prediction_source"] == "event_live"
    assert pred["event_started_at"] == started
    assert pred["predicted_start"] is not None


def test_predict_catalogue_entry_shows_offset_from_paper_schedule(db):
    session, trial = _seed_trial(db)
    started = datetime(2026, 6, 28, 10, 15)
    db.add(
        EventLiveTiming(
            trial_id=trial.id,
            day=1,
            ring_id="351",
            ring_number="1",
            event_name="Masters Agility",
            height_group=500,
            started_at=started,
            finished_at=None,
            status="Running",
        )
    )
    ce = CatalogueEntry(
        trial_id=trial.id,
        day=1,
        event_name="Masters Agility",
        height_group=500,
        height_group_total=12,
        cat_number="410",
        dog_name="Fika",
        handler_name="Jane Smith",
        run_position=3,
        ring_number="1",
    )
    db.add(ce)
    db.commit()

    paper_blocks = _compute_catalogue_blocks(
        trial,
        db,
        base_start=time(9, 0),
        setup_mins=session.default_setup_mins,
        walk_mins=session.default_walk_mins,
        tpd_for_height=session.tpd_for,
    )
    paper_block_starts = {
        (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"]
        for b in paper_blocks
    }
    live_blocks = _compute_catalogue_blocks(
        trial,
        db,
        base_start=time(9, 0),
        setup_mins=session.default_setup_mins,
        walk_mins=session.default_walk_mins,
        tpd_for_height=session.tpd_for,
        event_timings=_event_timings_for_trial(trial, db),
    )
    block_starts = {
        (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"]
        for b in live_blocks
    }

    pred = predict_catalogue_entry(
        ce=ce,
        session=session,
        trial=trial,
        all_class_schedules=[],
        block_starts=block_starts,
        entry_id=1,
        event_timings=_event_timings_for_trial(trial, db),
        paper_block_starts=paper_block_starts,
        live_enabled=True,
    )

    assert pred["paper_predicted_start_str"]
    assert pred["prediction_offset_str"]
    assert pred["paper_predicted_start_str"] == "9:03 AM"
    assert pred["predicted_start"] == started + timedelta(seconds=90 * 2)
    assert pred["prediction_offset_str"] == "75 min ahead"


def test_find_event_timing_fallback_without_numeric_ring(db):
    session, trial = _seed_trial(db)
    timing_row = EventLiveTiming(
        trial_id=trial.id,
        day=1,
        ring_id="351",
        ring_number="1",
        event_name="Masters Agility",
        height_group=500,
        started_at=datetime(2026, 6, 28, 10, 0),
        finished_at=None,
        status="Running",
    )
    db.add(timing_row)
    db.commit()

    event_timings = _event_timings_for_trial(trial, db)
    hit = _find_event_timing(
        event_timings,
        day=1,
        bare_ring=None,
        event_name="Masters Agility (ADM1)",
        height_group=500,
    )
    assert hit is timing_row

    heuristic_hit = _find_event_timing(
        event_timings,
        day=1,
        bare_ring="Agility",
        event_name="Masters Agility (ADM1)",
        height_group=500,
    )
    assert heuristic_hit is timing_row


def test_friend_predictions_use_live_timing(db):
    session, trial = _seed_trial(db)
    started = datetime(2026, 6, 28, 11, 0)
    db.add(
        EventLiveTiming(
            trial_id=trial.id,
            day=1,
            ring_id="351",
            ring_number="1",
            event_name="Masters Agility",
            height_group=500,
            started_at=started,
            finished_at=None,
            status="Running",
        )
    )
    db.add(
        CatalogueEntry(
            trial_id=trial.id,
            day=1,
            event_name="Masters Agility (ADM1)",
            height_group=500,
            height_group_total=12,
            cat_number="410",
            dog_name="Fika",
            handler_name="Jane Smith",
            run_position=2,
            ring_number="1",
        )
    )
    db.add(
        SessionFriend(
            session_uuid=session.uuid,
            trial_id=trial.id,
            handler_name="Jane Smith",
            label="Jane Smith",
            pin_key=friend_pin_key(handler_name="Jane Smith"),
        )
    )
    db.commit()

    event_timings = _event_timings_for_trial(trial, db)
    groups = build_friend_predictions(
        session,
        trial,
        db,
        event_timings=event_timings,
        live_enabled=True,
    )

    assert len(groups) == 1
    preds = groups[0]["predictions"]
    assert len(preds) == 1
    assert preds[0]["prediction_source"] == "event_live"
    assert preds[0]["event_started_at"] == started
