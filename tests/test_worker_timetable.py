"""Worker integration tests for timetable lunch break persistence."""
import asyncio
from datetime import date, time
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.models import Base, CatalogueEntry, Trial, TrialLunchBreak
from app.worker import (
    _extract_and_merge_lunch_breaks,
    _merge_lunch_breaks,
    refresh_trial_docs_job,
    upload_catalogue_job,
)


def _minimal_catalogue_entry(**overrides):
    base = {
        "event_name": "Novice Agility (AD1)",
        "cat_number": "201",
        "day": 1,
        "height_group": 400,
        "run_position": 1,
        "height_group_total": 1,
        "nfc": False,
        "dog_name": "Dog",
        "handler_name": "Handler",
        "ring_number": "1",
    }
    base.update(overrides)
    return base


def _sample_breaks():
    return [
        {"day": 2, "ring": "Ring 3", "lunch_break_at": time(12, 35), "lunch_break_mins": 45},
        {"day": 2, "ring": "Ring 2", "lunch_break_at": time(12, 50), "lunch_break_mins": 45},
        {"day": 3, "ring": "Ring 1", "lunch_break_at": time(13, 0), "lunch_break_mins": 45},
    ]


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(worker, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(
        "app.scraper.openrouter_catalogue.is_openrouter_enabled",
        lambda: False,
    )
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _seed_trial(db) -> Trial:
    trial = Trial(
        external_id="nationals-2026",
        name="Agility Nationals",
        start_date=date(2026, 6, 23),
    )
    db.add(trial)
    db.commit()
    return trial


def _mock_extract_breaks(monkeypatch, breaks):
    async def _fake_extract(data, trial_external_id=None):
        return breaks

    monkeypatch.setattr(
        "app.scraper.openrouter_timetable.extract_lunch_breaks_from_pdf",
        _fake_extract,
    )


def test_upload_catalogue_job_creates_lunch_breaks(db, monkeypatch):
    trial = _seed_trial(db)
    pdf_data = b"%PDF-1.4 catalogue"
    mock_breaks = _sample_breaks()

    monkeypatch.setattr(
        "app.scraper.catalogue.parse_catalogue_pdf_bytes_sync",
        lambda data, **kw: [_minimal_catalogue_entry()],
    )
    _mock_extract_breaks(monkeypatch, mock_breaks)

    upload_catalogue_job(trial.id, pdf_data, "application/pdf")
    db.expire_all()

    rows = (
        db.query(TrialLunchBreak)
        .filter(TrialLunchBreak.trial_id == trial.id)
        .order_by(TrialLunchBreak.day, TrialLunchBreak.ring)
        .all()
    )
    assert len(rows) == 3
    assert {(r.day, r.ring, r.lunch_break_at) for r in rows} == {
        (2, "Ring 2", time(12, 50)),
        (2, "Ring 3", time(12, 35)),
        (3, "Ring 1", time(13, 0)),
    }


def test_upload_overwrites_breaks_for_extracted_days(db, monkeypatch):
    trial = _seed_trial(db)
    db.add(
        TrialLunchBreak(
            trial_id=trial.id,
            day=2,
            ring="Ring 3",
            lunch_break_at=time(11, 0),
            lunch_break_mins=30,
        )
    )
    db.add(
        TrialLunchBreak(
            trial_id=trial.id,
            day=5,
            ring="Ring 1",
            lunch_break_at=time(10, 0),
            lunch_break_mins=45,
        )
    )
    db.commit()

    monkeypatch.setattr(
        "app.scraper.catalogue.parse_catalogue_pdf_bytes_sync",
        lambda data, **kw: [_minimal_catalogue_entry(day=2)],
    )
    _mock_extract_breaks(
        monkeypatch,
        [{"day": 2, "ring": "Ring 3", "lunch_break_at": time(12, 35), "lunch_break_mins": 45}],
    )

    upload_catalogue_job(trial.id, b"%PDF-1.4", "application/pdf")
    db.expire_all()

    rows = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).all()
    by_key = {(r.day, r.ring): r for r in rows}

    assert by_key[(2, "Ring 3")].lunch_break_at == time(12, 35)
    assert by_key[(5, "Ring 1")].lunch_break_at == time(10, 0)


def test_merge_lunch_breaks_fill_missing_preserves_manual(db):
    trial = _seed_trial(db)
    db.add(
        TrialLunchBreak(
            trial_id=trial.id,
            day=2,
            ring="Ring 1",
            lunch_break_at=time(11, 30),
            lunch_break_mins=60,
        )
    )
    db.commit()

    _merge_lunch_breaks(
        db,
        trial.id,
        [
            {"day": 2, "ring": "Ring 1", "lunch_break_at": time(12, 0), "lunch_break_mins": 45},
            {"day": 2, "ring": "Ring 2", "lunch_break_at": time(12, 50), "lunch_break_mins": 45},
        ],
        fill_missing_only=True,
    )
    db.commit()
    db.expire_all()

    rows = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).all()
    by_key = {(r.day, r.ring): r for r in rows}

    assert by_key[(2, "Ring 1")].lunch_break_at == time(11, 30)
    assert by_key[(2, "Ring 1")].lunch_break_mins == 60
    assert by_key[(2, "Ring 2")].lunch_break_at == time(12, 50)


def test_refresh_fill_missing_preserves_manual_breaks(db, monkeypatch):
    trial = _seed_trial(db)
    trial.catalogue_doc_url = "https://example.test/nationals.pdf"
    db.commit()

    db.add(
        TrialLunchBreak(
            trial_id=trial.id,
            day=2,
            ring="Ring 1",
            lunch_break_at=time(11, 30),
            lunch_break_mins=60,
        )
    )
    db.commit()
    trial_id = trial.id

    pdf_data = b"%PDF-1.4 nationals"

    async def fake_download_catalogue_pdf(url, *, cookies=None):
        assert url == trial.catalogue_doc_url
        return pdf_data

    async def fake_parse_catalogue_pdf_bytes(*args, **kwargs):
        return [_minimal_catalogue_entry(day=2)]

    monkeypatch.setattr(
        "app.scraper.catalogue.download_catalogue_pdf",
        fake_download_catalogue_pdf,
    )
    monkeypatch.setattr(
        "app.scraper.catalogue.parse_catalogue_pdf_bytes",
        fake_parse_catalogue_pdf_bytes,
    )
    _mock_extract_breaks(
        monkeypatch,
        [
            {"day": 2, "ring": "Ring 1", "lunch_break_at": time(12, 0), "lunch_break_mins": 45},
            {"day": 2, "ring": "Ring 2", "lunch_break_at": time(12, 50), "lunch_break_mins": 45},
        ],
    )

    refresh_trial_docs_job(trial_id)
    db.expire_all()

    rows = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial_id).all()
    by_key = {(r.day, r.ring): r for r in rows}

    assert by_key[(2, "Ring 1")].lunch_break_at == time(11, 30)
    assert by_key[(2, "Ring 2")].lunch_break_at == time(12, 50)


def test_extract_and_merge_swallows_extraction_errors(db, monkeypatch):
    trial = _seed_trial(db)

    async def boom(*args, **kwargs):
        raise RuntimeError("API down")

    monkeypatch.setattr(
        "app.scraper.openrouter_timetable.extract_lunch_breaks_from_pdf",
        boom,
    )

    asyncio.run(_extract_and_merge_lunch_breaks(db, trial.id, b"%PDF-1.4", trial.external_id))
    db.commit()

    assert db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).count() == 0
