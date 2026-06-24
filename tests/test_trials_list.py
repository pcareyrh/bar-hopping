from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.models import Session, Trial, SessionEntry


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


def _seed_trials(db_session):
    session = Session(uuid="u1", topdog_email="enc")
    upcoming = Trial(
        external_id="1",
        name="Future Trial",
        start_date=date(2026, 6, 30),
        end_date=date(2026, 6, 30),
    )
    in_progress = Trial(
        external_id="2",
        name="Weekend Trial",
        start_date=date(2026, 6, 23),
        end_date=date(2026, 6, 24),
    )
    finished = Trial(
        external_id="3",
        name="Last Week",
        start_date=date(2026, 6, 16),
        end_date=date(2026, 6, 16),
    )
    db_session.add_all([session, upcoming, in_progress, finished])
    db_session.flush()
    db_session.add_all([
        SessionEntry(
            session_uuid=session.uuid,
            trial_id=upcoming.id,
            dog_name="Fika",
            event_name="Novice Agility",
            cat_number="101",
            height_group=400,
        ),
        SessionEntry(
            session_uuid=session.uuid,
            trial_id=in_progress.id,
            dog_name="Fika",
            event_name="Masters Agility",
            cat_number="410",
            height_group=400,
        ),
        SessionEntry(
            session_uuid=session.uuid,
            trial_id=finished.id,
            dog_name="Fika",
            event_name="Novice Jumping",
            cat_number="201",
            height_group=400,
        ),
    ])
    db_session.commit()
    return upcoming, in_progress, finished


def test_trials_list_includes_in_progress_trials(client, db_session, monkeypatch):
    _seed_trials(db_session)
    monkeypatch.setattr("app.trial_dates.date", type("D", (), {"today": staticmethod(lambda: date(2026, 6, 24))}))

    resp = client.get("/s/u1/trials")
    assert resp.status_code == 200
    body = resp.text
    assert "Future Trial" in body
    assert "Weekend Trial" in body
    assert "Last Week" not in body
