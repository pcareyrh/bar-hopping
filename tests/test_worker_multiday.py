from datetime import date, time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.models import Base, CatalogueEntry, Session as UserSession, SessionEntry, Trial
from app.routers.schedule import _build_predictions, _compute_catalogue_blocks


def test_refresh_supplements_partial_my_day_with_late_catalogue(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    try:
        session = UserSession(uuid="u1")
        trial = Trial(
            external_id="1307",
            name="Multi Day",
            start_date=date(2026, 6, 23),
            catalogue_doc_url="https://www.topdogevents.com.au/trials/1307/entries",
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
        db.commit()
        trial_id = trial.id
    finally:
        db.close()

    async def fake_resolve_auth_cookies(_db, _session_uuid):
        return {"session": "cookie"}

    async def fake_fetch_trial_detail(external_id):
        return {
            "external_id": external_id,
            "catalogue_doc_url": "https://example.test/full-catalogue.pdf",
        }

    async def fake_fetch_my_day(_external_id, _cookies):
        return {
            "catalogue_entries": [
                {
                    "day": 1,
                    "event_name": "Masters Agility",
                    "cat_number": "410",
                    "height_group": 400,
                    "run_position": 1,
                    "height_group_total": 1,
                    "nfc": False,
                    "dog_name": "Fika",
                    "handler_name": "Handler",
                    "ring_number": "1",
                },
            ],
            "class_schedules": [],
            "start_time": time(8, 0),
        }

    async def fake_download_and_parse_catalogue(url, trial_external_id=None):
        assert url == "https://example.test/full-catalogue.pdf"
        assert trial_external_id == "1307"
        return [
            {
                "day": 1,
                "event_name": "Masters Agility",
                "cat_number": "410",
                "height_group": 400,
                "run_position": 1,
                "height_group_total": 1,
                "nfc": False,
                "dog_name": "Fika",
                "handler_name": "Handler",
                "ring_number": "1",
            },
            {
                "day": 2,
                "event_name": "Masters Agility",
                "cat_number": "410",
                "height_group": 400,
                "run_position": 1,
                "height_group_total": 1,
                "nfc": False,
                "dog_name": "Fika",
                "handler_name": "Handler",
                "ring_number": "1",
            },
        ]

    monkeypatch.setattr(worker, "_resolve_auth_cookies", fake_resolve_auth_cookies)
    monkeypatch.setattr("app.scraper.trials.fetch_trial_detail", fake_fetch_trial_detail)
    monkeypatch.setattr("app.scraper.my_day.fetch_my_day", fake_fetch_my_day)
    monkeypatch.setattr(
        "app.scraper.catalogue.download_and_parse_catalogue",
        fake_download_and_parse_catalogue,
    )

    worker.refresh_trial_docs_job(trial_id, session_uuid="u1")

    db = TestingSessionLocal()
    try:
        rows = (
            db.query(CatalogueEntry)
            .filter(CatalogueEntry.trial_id == trial_id)
            .order_by(CatalogueEntry.day)
            .all()
        )
        assert [row.day for row in rows] == [1, 2]

        refreshed_trial = db.get(Trial, trial_id)
        refreshed_session = db.get(UserSession, "u1")
        assert refreshed_trial.catalogue_doc_url == "https://example.test/full-catalogue.pdf"

        blocks = _compute_catalogue_blocks(
            refreshed_trial,
            db,
            base_start=refreshed_trial.start_time,
            setup_mins=refreshed_session.default_setup_mins,
            walk_mins=refreshed_session.default_walk_mins,
            tpd_for_height=refreshed_session.tpd_for,
        )
        predictions = _build_predictions(refreshed_session, refreshed_trial, db, day_blocks=blocks)
        assert [prediction["day"] for prediction in predictions] == [1, 2]
    finally:
        db.close()
        engine.dispose()


def test_refresh_supplements_partial_my_day_with_stored_catalogue(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    try:
        session = UserSession(uuid="u1")
        trial = Trial(
            external_id="1308",
            name="Uploaded Multi Day",
            start_date=date(2026, 6, 23),
        )
        db.add_all([session, trial])
        db.flush()
        db.add_all([
            SessionEntry(
                session_uuid=session.uuid,
                trial_id=trial.id,
                dog_name="Fika",
                event_name="Masters Agility",
                cat_number="410",
                height_group=400,
            ),
            CatalogueEntry(
                trial_id=trial.id,
                day=1,
                event_name="Masters Agility",
                cat_number="410",
                height_group=400,
                run_position=1,
                height_group_total=1,
                nfc=False,
                dog_name="Fika",
                handler_name="Handler",
                ring_number="1",
            ),
            CatalogueEntry(
                trial_id=trial.id,
                day=2,
                event_name="Masters Agility",
                cat_number="410",
                height_group=400,
                run_position=1,
                height_group_total=1,
                nfc=False,
                dog_name="Fika",
                handler_name="Handler",
                ring_number="1",
            ),
        ])
        db.commit()
        trial_id = trial.id
    finally:
        db.close()

    async def fake_resolve_auth_cookies(_db, _session_uuid):
        return {"session": "cookie"}

    async def fake_fetch_trial_detail(external_id):
        return {"external_id": external_id}

    async def fake_fetch_my_day(_external_id, _cookies):
        return {
            "catalogue_entries": [
                {
                    "day": 1,
                    "event_name": "Masters Agility",
                    "cat_number": "410",
                    "height_group": 400,
                    "run_position": 1,
                    "height_group_total": 1,
                    "nfc": False,
                    "dog_name": "Fika",
                    "handler_name": "Handler",
                    "ring_number": "1",
                },
            ],
            "class_schedules": [],
            "start_time": time(8, 0),
        }

    monkeypatch.setattr(worker, "_resolve_auth_cookies", fake_resolve_auth_cookies)
    monkeypatch.setattr("app.scraper.trials.fetch_trial_detail", fake_fetch_trial_detail)
    monkeypatch.setattr("app.scraper.my_day.fetch_my_day", fake_fetch_my_day)

    worker.refresh_trial_docs_job(trial_id, session_uuid="u1")

    db = TestingSessionLocal()
    try:
        rows = (
            db.query(CatalogueEntry)
            .filter(CatalogueEntry.trial_id == trial_id)
            .order_by(CatalogueEntry.day)
            .all()
        )
        assert [row.day for row in rows] == [1, 2]

        refreshed_trial = db.get(Trial, trial_id)
        refreshed_session = db.get(UserSession, "u1")
        assert refreshed_trial.catalogue_doc_url is None

        blocks = _compute_catalogue_blocks(
            refreshed_trial,
            db,
            base_start=refreshed_trial.start_time,
            setup_mins=refreshed_session.default_setup_mins,
            walk_mins=refreshed_session.default_walk_mins,
            tpd_for_height=refreshed_session.tpd_for,
        )
        predictions = _build_predictions(refreshed_session, refreshed_trial, db, day_blocks=blocks)
        assert [prediction["day"] for prediction in predictions] == [1, 2]
    finally:
        db.close()
        engine.dispose()
