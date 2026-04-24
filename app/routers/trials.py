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
def trials_list(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    _trigger_trials_refresh_if_stale(db)

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
def trial_detail(uuid: str, trial_id: int, request: Request, db: DBSession = Depends(get_db)):
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
            "refreshing": request.query_params.get("refreshing") == "1",
        },
    )


@router.post("/s/{uuid}/trials/{trial_id}/refresh")
def refresh_trial(uuid: str, trial_id: int, db: DBSession = Depends(get_db)):
    _get_session(uuid, db)
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(status_code=404, detail="Trial not found")

    try:
        from app.queue import get_queue
        get_queue().enqueue("app.worker.refresh_trial_docs_job", trial.id, job_timeout=300)
    except Exception:
        pass

    return RedirectResponse(url=f"/s/{uuid}/trials/{trial_id}?refreshing=1", status_code=303)


def _trigger_trials_refresh_if_stale(db: DBSession) -> None:
    oldest = db.query(Trial).order_by(Trial.scraped_at).first()
    now = datetime.utcnow()
    if oldest is None or oldest.scraped_at is None or (now - oldest.scraped_at) > CACHE_TTL:
        try:
            from app.queue import get_queue
            get_queue().enqueue("app.worker.refresh_trials_job", job_timeout=600)
        except Exception:
            pass


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
