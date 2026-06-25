"""Integration test: upload Final_Catalogue_upload_v2_ANONYMISED.pdf and use Friends tab.

Exercises the full path:
  parse PDF → upload_catalogue_job → CatalogueEntry rows → add friend → predictions → schedule UI

Uses the legacy pdfplumber parser (OpenRouter disabled) so the test is fast and deterministic.
Skips cleanly when the PDF fixture is not present in the checkout.
"""
import pathlib
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.database import Base, get_db
from app.main import app
from app.models import CatalogueEntry, Session, SessionFriend, Trial
from app.friends import (
    add_friend,
    build_friend_predictions,
    catalogue_entries_for_friend,
    friend_data_state,
    search_handlers,
)
from app.worker import upload_catalogue_job

_FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "Final_Catalogue_upload_v2_ANONYMISED.pdf"

pytestmark = pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason=f"catalogue PDF fixture not present at {_FIXTURE}",
)


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    return _FIXTURE.read_bytes()


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine, monkeypatch):
    TestingSessionLocal = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(
        "app.scraper.openrouter_catalogue.is_openrouter_enabled",
        lambda: False,
    )

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


@pytest.fixture()
def client(db_session):
    return TestClient(app, follow_redirects=False)


def _seed_session_and_trial(db) -> tuple[Session, Trial]:
    session = Session(uuid="pdf-u1")
    trial = Trial(
        external_id="adc-pawlympics",
        name="ADC Pawlympics (anon fixture)",
        start_date=date(2026, 6, 28),
    )
    db.add_all([session, trial])
    db.commit()
    return session, trial


def _upload_fixture(db, pdf_bytes: bytes) -> Trial:
    _, trial = _seed_session_and_trial(db)
    upload_catalogue_job(trial.id, pdf_bytes, "application/pdf")
    db.expire_all()
    return trial


def test_upload_pdf_populates_catalogue(db_session, pdf_bytes):
    trial = _upload_fixture(db_session, pdf_bytes)

    count = db_session.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).count()
    assert count == 900

    state = friend_data_state(trial, db_session)
    assert state["state"] == "available"
    assert state["available_days"] == [1, 2]


def test_search_handlers_after_pdf_upload(db_session, pdf_bytes):
    trial = _upload_fixture(db_session, pdf_bytes)

    # Fuzzy search: "Handler 1" is a substring of Handler 10, 105, etc.
    matches = search_handlers(trial.id, "Handler 1", db_session)
    handler_names = {m["handler_name"] for m in matches}
    assert "Handler 1" in handler_names
    handler_1 = next(m for m in matches if m["handler_name"] == "Handler 1")
    assert len(handler_1["dog_names"]) >= 2


def test_add_friend_by_handler_after_pdf_upload(db_session, pdf_bytes):
    session, trial = _seed_session_and_trial(db_session)
    upload_catalogue_job(trial.id, pdf_bytes, "application/pdf")
    db_session.expire_all()

    friend, err = add_friend(
        session_uuid=session.uuid,
        trial_id=trial.id,
        query="",
        handler_name="Handler 1",
        db=db_session,
    )
    assert err is None
    assert friend.handler_name == "Handler 1"

    catalogue_rows = catalogue_entries_for_friend(friend, trial.id, db_session)
    dog_names = {r.dog_name for r in catalogue_rows if r.dog_name}
    assert len(dog_names) >= 2

    groups = build_friend_predictions(session, trial, db_session)
    assert len(groups) == 1
    preds = groups[0]["predictions"]
    assert len(preds) >= len(dog_names)
    assert {p["handler_name"] for p in preds} == {"Handler 1"}
    assert any(p["predicted_start_str"] for p in preds)


def test_add_friend_by_cat_expands_to_handler_after_pdf_upload(db_session, pdf_bytes):
    session, trial = _seed_session_and_trial(db_session)
    upload_catalogue_job(trial.id, pdf_bytes, "application/pdf")
    db_session.expire_all()

    friend, err = add_friend(
        session_uuid=session.uuid,
        trial_id=trial.id,
        query="500",
        db=db_session,
    )
    assert err is None
    assert friend.handler_name == "Handler 1"

    groups = build_friend_predictions(session, trial, db_session)
    dog_names = {p["dog_name"] for p in groups[0]["predictions"] if p["dog_name"]}
    assert "Dog 500" in dog_names
    assert len(dog_names) >= 2


def test_friends_schedule_tab_after_pdf_upload(client, db_session, pdf_bytes):
    session, trial = _seed_session_and_trial(db_session)
    upload_catalogue_job(trial.id, pdf_bytes, "application/pdf")
    db_session.expire_all()

    add_resp = client.post(
        f"/s/{session.uuid}/trials/{trial.id}/friends",
        data={"handler_name": "Handler 1"},
    )
    assert add_resp.status_code == 303
    assert "tab=friends" in add_resp.headers["location"]
    assert db_session.query(SessionFriend).count() == 1

    page = client.get(f"/s/{session.uuid}/trials/{trial.id}/schedule?tab=friends")
    assert page.status_code == 200
    assert "Handler 1" in page.text
    assert "Dog 500" in page.text
    assert "Friend data from the catalogue" in page.text


def test_upload_catalogue_route_enqueues_job(client, db_session, pdf_bytes, monkeypatch):
    """HTTP upload-catalogue → synchronous job run → friends can be added."""
    from app.scraper.catalogue import parse_catalogue_pdf

    session, trial = _seed_session_and_trial(db_session)

    # TestClient runs an event loop; avoid asyncio.run() inside the worker job.
    monkeypatch.setattr(
        "app.scraper.catalogue.parse_catalogue_pdf_bytes_sync",
        lambda data, **kw: parse_catalogue_pdf(data),
    )

    def _run_upload_job(func_path, trial_id, data, content_type, **kwargs):
        assert func_path == "app.worker.upload_catalogue_job"
        upload_catalogue_job(trial_id, data, content_type)
        return type("Job", (), {"id": "test-job"})()

    class _FakeQueue:
        def enqueue(self, func_path, *args, **kwargs):
            return _run_upload_job(func_path, *args, **kwargs)

    monkeypatch.setattr("app.queue.get_queue", lambda: _FakeQueue())

    resp = client.post(
        f"/s/{session.uuid}/trials/{trial.id}/upload-catalogue",
        files={"file": (_FIXTURE.name, pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 303
    assert "refreshing=1" in resp.headers["location"]

    db_session.expire_all()
    assert db_session.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).count() == 900

    friend, err = add_friend(
        session_uuid=session.uuid,
        trial_id=trial.id,
        query="",
        handler_name="Handler 1",
        db=db_session,
    )
    assert err is None
    assert friend is not None

    page = client.get(f"/s/{session.uuid}/trials/{trial.id}/schedule?tab=friends")
    assert page.status_code == 200
    assert "Handler 1" in page.text
