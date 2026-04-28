import logging

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session, Trial, CatalogueEntry, ClassSchedule, SessionEntry

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/s/{uuid}/trials", response_class=HTMLResponse)
def trials_list(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    user_trial_ids = {e.trial_id for e in session.entries}

    user_trials = (
        db.query(Trial)
        .filter(Trial.id.in_(user_trial_ids))
        .order_by(Trial.start_date)
        .all()
        if user_trial_ids else []
    )

    return templates.TemplateResponse(
        request, "trials.html",
        {
            "session": session,
            "uuid": uuid,
            "trials": user_trials,
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
        job = get_queue().enqueue("app.worker.refresh_trial_docs_job", trial.id, uuid, job_timeout=300)
        log.info("Trial refresh enqueued: trial_id=%s session=%s job=%s", trial.id, uuid, job.id)
    except Exception:
        log.warning("Failed to enqueue trial refresh: trial_id=%s session=%s", trial.id, uuid, exc_info=True)

    return RedirectResponse(url=f"/s/{uuid}/trials/{trial_id}?refreshing=1", status_code=303)


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
