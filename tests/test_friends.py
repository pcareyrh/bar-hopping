"""Tests for the Friends tab: data state, resolution, predictions, routes."""
from datetime import date, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import (
    Session,
    Trial,
    CatalogueEntry,
    SessionEntry,
    SessionFriend,
    ClassSchedule,
    friend_pin_key,
    normalize_handler_name,
)
from app.friends import (
    is_real_catalogue_entry,
    friend_data_state,
    search_handlers,
    add_friend,
    remove_friend,
    build_friend_predictions,
    catalogue_entries_for_friend,
)
from app.routers.schedule import _build_predictions, predict_catalogue_entry, build_catalogue_index
from app.engine.predictor import flag_conflicts


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


def _seed_trial_with_handlers(db):
    sess = Session(uuid="u1")
    trial = Trial(external_id="t1", name="Test Trial", start_date=date(2026, 6, 28))
    db.add_all([sess, trial])
    db.flush()
    db.add_all([
        _cat(trial, cat_number="410", dog_name="Fika", handler_name="Jane Smith", event_name="Novice Agility", run_position=3),
        _cat(trial, cat_number="411", dog_name="Rex", handler_name="Jane Smith", event_name="Novice Jumping", run_position=5),
        _cat(trial, cat_number="520", dog_name="Bolt", handler_name="Bob Jones", event_name="Novice Agility", run_position=2),
        _cat(trial, cat_number="~Sat~400", dog_name=None, handler_name=None, event_name="Novice Agility", run_position=0),
    ])
    db.commit()
    return sess, trial


def test_normalize_handler_name():
    assert normalize_handler_name("· Jane Smith") == "jane smith"
    assert normalize_handler_name("Jane  Smith") == "jane smith"


def test_is_real_catalogue_entry():
    real = CatalogueEntry(
        trial_id=1, day=1, event_name="A", cat_number="410",
        height_group=400, run_position=1, height_group_total=1,
        dog_name="Fika", handler_name="Jane",
    )
    sentinel = CatalogueEntry(
        trial_id=1, day=1, event_name="A", cat_number="~Sat~400",
        height_group=400, run_position=0, height_group_total=10,
    )
    assert is_real_catalogue_entry(real)
    assert not is_real_catalogue_entry(sentinel)


def test_friend_data_state_available(db):
    _, trial = _seed_trial_with_handlers(db)
    state = friend_data_state(trial, db)
    assert state["state"] == "available"
    assert 1 in state["available_days"]


def test_friend_data_state_summary_only(db):
    trial = Trial(external_id="t2", name="Summary", start_date=date(2026, 6, 28))
    db.add(trial)
    db.flush()
    db.add(_cat(trial, cat_number="~Sat~400", dog_name=None, handler_name=None,
                event_name="Novice Agility", run_position=0))
    db.commit()
    assert friend_data_state(trial, db)["state"] == "summary_only"


def test_friend_data_state_none(db):
    trial = Trial(external_id="t3", name="Empty", start_date=date(2026, 6, 28))
    db.add(trial)
    db.commit()
    assert friend_data_state(trial, db)["state"] == "none"


def test_search_handlers(db):
    _, trial = _seed_trial_with_handlers(db)
    matches = search_handlers(trial.id, "jane", db)
    assert len(matches) == 1
    assert matches[0]["handler_name"] == "Jane Smith"
    assert set(matches[0]["dog_names"]) == {"Fika", "Rex"}


def test_add_friend_by_handler(db):
    sess, trial = _seed_trial_with_handlers(db)
    friend, err = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Jane Smith", db=db)
    assert err is None
    assert friend.handler_name == "Jane Smith"
    rows = catalogue_entries_for_friend(friend, trial.id, db)
    assert len(rows) == 2
    assert {r.dog_name for r in rows} == {"Fika", "Rex"}


def test_add_friend_by_cat_expands_to_handler(db):
    sess, trial = _seed_trial_with_handlers(db)
    friend, err = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="410", db=db)
    assert err is None
    assert friend.handler_name == "Jane Smith"
    assert len(catalogue_entries_for_friend(friend, trial.id, db)) == 2


def test_add_friend_duplicate_is_idempotent(db):
    sess, trial = _seed_trial_with_handlers(db)
    f1, _ = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="410", db=db)
    f2, err = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="411", db=db)
    assert err is None
    assert f1.id == f2.id


def test_add_friend_not_found(db):
    sess, trial = _seed_trial_with_handlers(db)
    _, err = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Nobody", db=db)
    assert "No competitor matched" in err


def test_add_friend_blocked_without_catalogue(db):
    sess = Session(uuid="u1")
    trial = Trial(external_id="t4", name="Empty", start_date=date(2026, 6, 28))
    db.add_all([sess, trial])
    db.commit()
    _, err = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="410", db=db)
    assert "running order" in err.lower()


def test_remove_friend(db):
    sess, trial = _seed_trial_with_handlers(db)
    friend, _ = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Bob", db=db)
    assert remove_friend(friend.id, sess.uuid, trial.id, db)
    assert db.query(SessionFriend).count() == 0


def test_build_friend_predictions(db):
    sess, trial = _seed_trial_with_handlers(db)
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="1",
        class_name="Novice Agility", scheduled_start=time(9, 0),
    ))
    db.commit()
    friend, _ = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Jane Smith", db=db)
    groups = build_friend_predictions(sess, trial, db)
    assert len(groups) == 1
    preds = groups[0]["predictions"]
    assert len(preds) == 2
    assert all(p["is_friend"] for p in preds)
    assert all(p["predicted_start_str"] for p in preds)
    assert not any(p.get("position_override") for p in preds)


def test_friend_prediction_matches_own_entry(db):
    sess, trial = _seed_trial_with_handlers(db)
    ce = db.query(CatalogueEntry).filter(CatalogueEntry.cat_number == "410").first()
    db.add(SessionEntry(
        session_uuid=sess.uuid, trial_id=trial.id, dog_name="Fika",
        event_name="Novice Agility", cat_number="410", height_group=400,
        catalogue_entry_id=ce.id,
    ))
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="1",
        class_name="Novice Agility", scheduled_start=time(9, 0),
    ))
    db.commit()

    mine = _build_predictions(sess, trial, db, day_blocks=[])
    friend, _ = add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="410", db=db)
    groups = build_friend_predictions(sess, trial, db, day_blocks=[])
    friend_pred = next(p for p in groups[0]["predictions"] if p["cat_number"] == "410")
    mine_pred = next(p for p in mine if p["cat_number"] == "410")
    assert friend_pred["predicted_start"] == mine_pred["predicted_start"]


def test_multi_day_friend_fanout(db):
    sess = Session(uuid="u1")
    trial = Trial(external_id="md", name="Nationals", start_date=date(2026, 6, 23))
    db.add_all([sess, trial])
    db.flush()
    db.add_all([
        _cat(trial, day=2, event_name="Masters Agility (ADM1)", cat_number="410",
             dog_name="Fika", handler_name="Jane Smith", run_position=1),
        _cat(trial, day=3, event_name="Masters Agility (ADM2)", cat_number="410",
             dog_name="Fika", handler_name="Jane Smith", run_position=1),
    ])
    db.commit()
    add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Jane", db=db)
    groups = build_friend_predictions(sess, trial, db)
    days = sorted({p["day"] for p in groups[0]["predictions"]})
    assert days == [2, 3]


def test_friend_data_state_partial(db):
    trial = Trial(external_id="p", name="Partial", start_date=date(2026, 6, 28))
    db.add(trial)
    db.flush()
    db.add_all([
        _cat(trial, day=1, cat_number="410", dog_name="A", handler_name="Jane", event_name="Agility"),
        _cat(trial, day=2, cat_number="~Sun~400", dog_name=None, handler_name=None,
             event_name="Agility", run_position=0),
    ])
    db.commit()
    state = friend_data_state(trial, db)
    assert state["state"] == "partial"
    assert 1 in state["available_days"]
    assert 2 in state["pending_days"]


def test_schedule_friends_tab_route(client, client_db):
    sess, trial = _seed_trial_with_handlers(client_db)
    resp = client.get(f"/s/{sess.uuid}/trials/{trial.id}/schedule?tab=friends")
    assert resp.status_code == 200
    assert "Friends" in resp.text
    assert "Find friends" in resp.text or "Refresh friend data" in resp.text


def test_add_friend_post_route(client, client_db):
    sess, trial = _seed_trial_with_handlers(client_db)
    resp = client.post(
        f"/s/{sess.uuid}/trials/{trial.id}/friends",
        data={"query": "Jane Smith"},
    )
    assert resp.status_code == 303
    assert "tab=friends" in resp.headers["location"]
    assert client_db.query(SessionFriend).count() == 1


def test_friend_pin_key():
    assert friend_pin_key(handler_name="Jane Smith") == "handler:jane smith"
    assert friend_pin_key(cat_number="410") == "cat:410"


def test_friend_conflict_with_your_run(db):
    sess, trial = _seed_trial_with_handlers(db)
    ce_user = db.query(CatalogueEntry).filter(CatalogueEntry.cat_number == "520").first()
    db.add(SessionEntry(
        session_uuid=sess.uuid, trial_id=trial.id, dog_name="Bolt",
        event_name="Novice Agility", cat_number="520", height_group=400,
        catalogue_entry_id=ce_user.id,
    ))
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="1",
        class_name="Novice Agility", scheduled_start=time(9, 0),
    ))
    db.commit()

    mine = _build_predictions(sess, trial, db, day_blocks=[])
    add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Jane Smith", db=db)
    groups = build_friend_predictions(sess, trial, db, day_blocks=[])
    friend_preds = groups[0]["predictions"]
    # Jane's Agility run is position 3, user's Bolt is position 2 — same class, close times.
    combined = mine + friend_preds
    flag_conflicts(combined)

    jane_agility = next(p for p in friend_preds if p["event_name"] == "Novice Agility")
    assert jane_agility["conflict"]
    assert jane_agility["conflict_with_yours"]
    assert any(not c["is_friend"] for c in jane_agility["conflicts_with"])
    assert jane_agility["conflicts_with"][0]["dog_name"] == "Bolt"


def test_friend_conflict_with_another_friend(db):
    sess, trial = _seed_trial_with_handlers(db)
    db.add(ClassSchedule(
        trial_id=trial.id, day=1, ring_number="1",
        class_name="Novice Agility", scheduled_start=time(9, 0),
    ))
    db.commit()
    add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Jane Smith", db=db)
    add_friend(session_uuid=sess.uuid, trial_id=trial.id, query="Bob Jones", db=db)
    groups = build_friend_predictions(sess, trial, db, day_blocks=[])
    friend_preds = [p for g in groups for p in g["predictions"]]
    flag_conflicts(friend_preds)

    agility = [p for p in friend_preds if p["event_name"] == "Novice Agility"]
    assert len(agility) == 2
    for p in agility:
        assert p["conflict"]
        assert not p["conflict_with_yours"]
        assert all(c["is_friend"] for c in p["conflicts_with"])
