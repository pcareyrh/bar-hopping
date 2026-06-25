"""Tests for Trial Crew grid builder and schedule tab."""
from datetime import date, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import Session, Trial, CatalogueEntry, SessionEntry, SessionFriend, ClassSchedule
from app.crew import build_crew_grid, _sort_rings
from app.friends import add_friend, build_friend_predictions
from app.routers.schedule import _build_predictions, _compute_catalogue_blocks


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def client_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestingSessionLocal()

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
        engine.dispose()


@pytest.fixture()
def client(client_db):
    return TestClient(app, follow_redirects=False)


def _cat(trial, **kw):
    base = dict(
        trial_id=trial.id,
        height_group=400,
        height_group_total=10,
        nfc=False,
        day=1,
        run_position=1,
    )
    base.update(kw)
    return CatalogueEntry(**base)


def _seed_two_ring_trial(db):
    sess = Session(uuid="crew-u1")
    trial = Trial(external_id="crew-t1", name="Crew Trial", start_date=date(2026, 6, 28))
    db.add_all([sess, trial])
    db.flush()
    db.add_all([
        _cat(trial, cat_number="410", dog_name="Fika", handler_name="Jane Smith",
             event_name="Novice Agility", run_position=3, ring_number="1"),
        _cat(trial, cat_number="520", dog_name="Bolt", handler_name="Bob Jones",
             event_name="Novice Jumping", run_position=2, ring_number="2"),
        _cat(trial, cat_number="530", dog_name="Zig", handler_name="Pat Lee",
             event_name="Novice Agility", run_position=5, ring_number="1"),
    ])
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="1",
        class_name="Novice Agility", scheduled_start=time(9, 0),
    ))
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="2",
        class_name="Novice Jumping", scheduled_start=time(9, 0),
    ))
    db.commit()
    return sess, trial


def _day_blocks(sess, trial, db):
    return _compute_catalogue_blocks(
        trial, db,
        base_start=time(9, 0),
        setup_mins=sess.default_setup_mins,
        walk_mins=sess.default_walk_mins,
        tpd_for_height=sess.tpd_for,
    )


def test_sort_rings_numeric_first():
    assert _sort_rings(["Jumping", "Ring 2", "Ring 1", "Agility"]) == [
        "Ring 1", "Ring 2", "Agility", "Jumping",
    ]


def test_crew_grid_places_events_in_ring_columns(db):
    sess, trial = _seed_two_ring_trial(db)
    blocks = _day_blocks(sess, trial, db)
    predictions = _build_predictions(sess, trial, db, day_blocks=blocks)
    grid = build_crew_grid(blocks, predictions, [], selected_day=1)

    assert "Ring 1" in grid["rings"]
    assert "Ring 2" in grid["rings"]
    agility_rows = [r for r in grid["rows"] if not r["is_lunch_break"] and r["cells"].get("Ring 1")]
    jumping_rows = [r for r in grid["rows"] if not r["is_lunch_break"] and r["cells"].get("Ring 2")]
    assert agility_rows
    assert jumping_rows
    assert agility_rows[0]["cells"]["Ring 2"] is None
    assert jumping_rows[0]["cells"]["Ring 1"] is None


def test_crew_grid_shows_user_and_friend_icons(db):
    sess, trial = _seed_two_ring_trial(db)
    ce = db.query(CatalogueEntry).filter(CatalogueEntry.cat_number == "410").first()
    db.add(SessionEntry(
        session_uuid=sess.uuid, trial_id=trial.id, dog_name="Fika",
        event_name="Novice Agility", cat_number="410", height_group=400,
        catalogue_entry_id=ce.id, ring_number="1",
    ))
    db.commit()

    blocks = _day_blocks(sess, trial, db)
    predictions = _build_predictions(sess, trial, db, day_blocks=blocks)
    add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Bob Jones", db=db)
    friend_groups = build_friend_predictions(sess, trial, db, day_blocks=blocks)
    grid = build_crew_grid(blocks, predictions, friend_groups, selected_day=1)

    agility_cell = next(
        r["cells"]["Ring 1"]
        for r in grid["rows"]
        if not r["is_lunch_break"] and r["cells"].get("Ring 1")
        and r["cells"]["Ring 1"]["event_name"] == "Novice Agility"
    )
    jumping_cell = next(
        r["cells"]["Ring 2"]
        for r in grid["rows"]
        if not r["is_lunch_break"] and r["cells"].get("Ring 2")
        and r["cells"]["Ring 2"]["event_name"] == "Novice Jumping"
    )
    assert len(agility_cell["crew"]) == 1
    assert agility_cell["crew"][0]["dog_initial"] == "F"
    assert len(jumping_cell["crew"]) == 1
    assert jumping_cell["crew"][0]["dog_initial"] == "B"
    assert len(grid["crew_legend"]) == 2


def test_crew_grid_includes_non_crew_events(db):
    sess, trial = _seed_two_ring_trial(db)
    blocks = _day_blocks(sess, trial, db)
    grid = build_crew_grid(blocks, [], [], selected_day=1)

    event_rows = [r for r in grid["rows"] if not r["is_lunch_break"]]
    assert len(event_rows) >= 2
    assert all(
        any(cell for cell in r["cells"].values() if cell)
        for r in event_rows
    )
    assert all(
        r["cells"][ring]["crew"] == []
        for r in event_rows
        for ring in grid["rings"]
        if r["cells"].get(ring)
    )


def test_crew_grid_respects_selected_day(db):
    sess = Session(uuid="crew-md")
    trial = Trial(external_id="crew-md", name="Multi", start_date=date(2026, 6, 23))
    db.add_all([sess, trial])
    db.flush()
    db.add_all([
        _cat(trial, day=1, cat_number="101", dog_name="A", handler_name="H1",
             event_name="Novice Agility", ring_number="1"),
        _cat(trial, day=2, cat_number="102", dog_name="B", handler_name="H2",
             event_name="Novice Jumping", ring_number="2"),
    ])
    db.commit()

    blocks = _day_blocks(sess, trial, db)
    grid_day1 = build_crew_grid(blocks, [], [], selected_day=1)
    grid_day2 = build_crew_grid(blocks, [], [], selected_day=2)
    assert grid_day1["rows"]
    assert grid_day2["rows"]
    day1_events = {
        c["event_name"]
        for r in grid_day1["rows"] if not r["is_lunch_break"]
        for c in r["cells"].values() if c
    }
    day2_events = {
        c["event_name"]
        for r in grid_day2["rows"] if not r["is_lunch_break"]
        for c in r["cells"].values() if c
    }
    assert any("Agility" in e for e in day1_events)
    assert any("Jumping" in e for e in day2_events)


def test_crew_grid_legend_deduplicates_handler_dog(db):
    sess, trial = _seed_two_ring_trial(db)
    ce = db.query(CatalogueEntry).filter(CatalogueEntry.cat_number == "410").first()
    db.add(SessionEntry(
        session_uuid=sess.uuid, trial_id=trial.id, dog_name="Fika",
        event_name="Novice Agility", cat_number="410", height_group=400,
        catalogue_entry_id=ce.id, ring_number="1",
    ))
    db.commit()
    blocks = _day_blocks(sess, trial, db)
    predictions = _build_predictions(sess, trial, db, day_blocks=blocks)
    grid = build_crew_grid(blocks, predictions, [], selected_day=1)
    assert len(grid["crew_legend"]) == 1


def test_schedule_crew_tab_route(client, client_db):
    sess, trial = _seed_two_ring_trial(client_db)
    resp = client.get(f"/s/{sess.uuid}/trials/{trial.id}/schedule?tab=crew")
    assert resp.status_code == 200
    assert "Trial Crew" in resp.text
    assert "Crew" in resp.text
