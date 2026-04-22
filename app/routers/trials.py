from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session, Trial, CatalogueEntry, ClassSchedule, SessionEntry

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

CACHE_TTL = timedelta(hours=4)


@router.get("/s/{uuid}/trials", response_class=HTMLResponse)
async def trials_list(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    await _ensure_trials_fresh(db)

    all_trials = db.query(Trial).order_by(Trial.start_date).all()
    user_trial_ids = {e.trial_id for e in session.entries}

    return templates.TemplateResponse(
        request, "trials.html",
        {
            "session": session,
            "uuid": uuid,
            "trials": all_trials,
            "user_trial_ids": user_trial_ids,
        },
    )


@router.get("/s/{uuid}/trials/{trial_id}", response_class=HTMLResponse)
async def trial_detail(uuid: str, trial_id: int, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(status_code=404, detail="Trial not found")

    user_entries = (
        db.query(SessionEntry)
        .filter(SessionEntry.session_uuid == uuid, SessionEntry.trial_id == trial_id)
        .all()
    )
    schedules = db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial_id).all()

    return templates.TemplateResponse(
        request, "trial_detail.html",
        {
            "session": session,
            "uuid": uuid,
            "trial": trial,
            "user_entries": user_entries,
            "schedules": schedules,
            "has_catalogue": bool(
                db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).first()
            ),
        },
    )


@router.post("/s/{uuid}/trials/{trial_id}/refresh")
async def refresh_trial(uuid: str, trial_id: int, db: DBSession = Depends(get_db)):
    _get_session(uuid, db)
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(status_code=404, detail="Trial not found")

    await _refresh_trial_docs(trial, db)
    return RedirectResponse(url=f"/s/{uuid}/trials/{trial_id}", status_code=303)


async def _ensure_trials_fresh(db: DBSession):
    from app.scraper.trials import scrape_trials_list, scrape_trial_detail

    oldest = db.query(Trial).order_by(Trial.scraped_at).first()
    now = datetime.utcnow()

    if oldest is None or oldest.scraped_at is None or (now - oldest.scraped_at) > CACHE_TTL:
        try:
            trial_stubs = await scrape_trials_list()
            for stub in trial_stubs:
                existing = db.query(Trial).filter(Trial.external_id == stub["external_id"]).first()
                if not existing:
                    detail = await scrape_trial_detail(stub["external_id"])
                    trial = Trial(
                        external_id=stub["external_id"],
                        name=detail.get("name", stub["name"]),
                        start_date=detail.get("start_date"),
                        venue=detail.get("venue"),
                        state="NSW",
                        schedule_doc_url=detail.get("schedule_doc_url"),
                        catalogue_doc_url=detail.get("catalogue_doc_url"),
                        scraped_at=now,
                    )
                    db.add(trial)
                else:
                    existing.scraped_at = now
            db.commit()
        except Exception:
            pass


async def _refresh_trial_docs(trial: Trial, db: DBSession):
    from app.scraper.catalogue import download_and_parse_catalogue
    from app.scraper.schedule import download_and_parse_schedule
    from app.models import CatalogueEntry, ClassSchedule

    if trial.catalogue_doc_url:
        try:
            entries = await download_and_parse_catalogue(trial.catalogue_doc_url)
            db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).delete()
            for e in entries:
                db.add(CatalogueEntry(trial_id=trial.id, **e))
            db.commit()
            _resolve_catalogue_links(trial, db)
        except Exception:
            pass

    if trial.schedule_doc_url:
        try:
            classes = await download_and_parse_schedule(trial.schedule_doc_url)
            db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial.id).delete()
            for c in classes:
                db.add(ClassSchedule(trial_id=trial.id, **c))
            db.commit()
        except Exception:
            pass

    trial.scraped_at = datetime.utcnow()
    db.commit()


def _resolve_catalogue_links(trial: Trial, db: DBSession):
    session_entries = (
        db.query(SessionEntry).filter(SessionEntry.trial_id == trial.id).all()
    )
    for se in session_entries:
        if se.cat_number:
            ce = (
                db.query(CatalogueEntry)
                .filter(
                    CatalogueEntry.trial_id == trial.id,
                    CatalogueEntry.cat_number == se.cat_number,
                    CatalogueEntry.event_name == se.event_name,
                )
                .first()
            )
            if ce:
                se.catalogue_entry_id = ce.id
    db.commit()


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
