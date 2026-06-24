from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.models import Base, CatalogueEntry, Session as UserSession, SessionEntry, Trial


def test_sync_preserves_active_in_progress_trials(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(
        "app.trial_dates.date",
        type("D", (), {"today": staticmethod(lambda: date(2026, 6, 24))}),
    )

    db = TestingSessionLocal()
    try:
        session = UserSession(uuid="u1", topdog_email="enc", topdog_password="enc")
        trial = Trial(
            external_id="1307",
            name="Weekend Trial",
            start_date=date(2026, 6, 23),
            end_date=date(2026, 6, 24),
        )
        db.add_all([session, trial])
        db.flush()
        db.add(SessionEntry(
            session_uuid=session.uuid,
            trial_id=trial.id,
            dog_name="Fika",
            event_name="Masters Agility",
            cat_number="410",
            height_group=400,
        ))
        db.add(CatalogueEntry(
            trial_id=trial.id,
            day=1,
            event_name="Masters Agility",
            cat_number="410",
            height_group=400,
            run_position=1,
            height_group_total=1,
            nfc=False,
        ))
        db.commit()
        trial_id = trial.id
    finally:
        db.close()

    async def fake_sync_user_entries(_email, _password):
        return []

    monkeypatch.setattr("app.scraper.auth.sync_user_entries", fake_sync_user_entries)
    monkeypatch.setattr(worker, "set_sync_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.crypto, "decrypt", lambda value: value)
    monkeypatch.setattr(worker, "get_queue", lambda: type("Q", (), {"enqueue": lambda *a, **k: None})())

    worker.sync_session_job("u1")

    db = TestingSessionLocal()
    try:
        entries = db.query(SessionEntry).filter(SessionEntry.session_uuid == "u1").all()
        assert len(entries) == 1
        assert entries[0].dog_name == "Fika"
        assert entries[0].trial_id == trial_id
    finally:
        db.close()
        engine.dispose()
