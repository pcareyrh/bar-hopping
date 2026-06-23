"""Tests for issue #29 — catalogue persisted for all users + overwrite guard.

Covers two behaviours:
  1. A catalogue is stored at the trial level and linked to *every* session
     entered in that trial, so a second user reuses the first user's parsed
     catalogue instead of paying to re-parse it.
  2. Uploading/refreshing a trial that already has catalogue data is blocked
     unless the request explicitly confirms the overwrite.
"""
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import Session, Trial, CatalogueEntry, SessionEntry
from app.worker import _resolve_catalogue_links


@pytest.fixture()
def db_session():
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
def client(db_session):
    return TestClient(app, follow_redirects=False)


def _make_trial_with_catalogue(db) -> Trial:
    trial = Trial(external_id="9001", name="Test Trial")
    db.add(trial)
    db.flush()
    db.add(CatalogueEntry(
        trial_id=trial.id, day=1, event_name="Masters Agility", cat_number="100",
        height_group=500, run_position=1, height_group_total=10,
    ))
    db.commit()
    return trial


def _make_session(db, uuid: str) -> Session:
    s = Session(uuid=uuid)
    db.add(s)
    db.commit()
    return s


# ---------------------------------------------------------------------------
# Persisted for all users
# ---------------------------------------------------------------------------

def test_catalogue_linked_across_multiple_sessions(db_session):
    """A single trial-level catalogue links to entries from every session."""
    trial = _make_trial_with_catalogue(db_session)
    ce = db_session.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).first()

    # Two different users (sessions) entered in the same trial/class.
    for uuid in ("user-a", "user-b"):
        _make_session(db_session, uuid)
        db_session.add(SessionEntry(
            session_uuid=uuid, trial_id=trial.id,
            dog_name=f"Dog {uuid}", event_name="Masters Agility",
            cat_number="100", height_group=500,
        ))
    db_session.commit()

    _resolve_catalogue_links(trial, db_session)

    entries = db_session.query(SessionEntry).filter(SessionEntry.trial_id == trial.id).all()
    assert len(entries) == 2
    assert all(e.catalogue_entry_id == ce.id for e in entries), \
        "both sessions should share the single trial-level catalogue entry"


# ---------------------------------------------------------------------------
# Overwrite guard
# ---------------------------------------------------------------------------

def test_refresh_blocked_without_overwrite(client, db_session):
    trial = _make_trial_with_catalogue(db_session)
    _make_session(db_session, "u1")

    resp = client.post(f"/s/u1/trials/{trial.id}/refresh")
    assert resp.status_code == 303
    assert "overwrite_blocked=1" in resp.headers["location"]


def test_refresh_allowed_with_overwrite(client, db_session):
    trial = _make_trial_with_catalogue(db_session)
    _make_session(db_session, "u1")

    resp = client.post(f"/s/u1/trials/{trial.id}/refresh", data={"overwrite": "1"})
    assert resp.status_code == 303
    assert "refreshing=1" in resp.headers["location"]


def test_refresh_allowed_when_no_catalogue(client, db_session):
    trial = Trial(external_id="9002", name="No Catalogue Trial")
    db_session.add(trial)
    db_session.commit()
    _make_session(db_session, "u1")

    resp = client.post(f"/s/u1/trials/{trial.id}/refresh")
    assert resp.status_code == 303
    assert "refreshing=1" in resp.headers["location"]


def test_upload_blocked_without_overwrite(client, db_session):
    trial = _make_trial_with_catalogue(db_session)
    _make_session(db_session, "u1")

    resp = client.post(
        f"/s/u1/trials/{trial.id}/upload-catalogue",
        files={"file": ("cat.xlsx", b"dummy-bytes", "application/vnd.ms-excel")},
    )
    assert resp.status_code == 303
    assert "overwrite_blocked=1" in resp.headers["location"]
