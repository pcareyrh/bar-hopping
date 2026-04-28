import html as html_lib
import logging

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session
from app import crypto

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/sessions")
def create_session(db: DBSession = Depends(get_db)):
    session = Session()
    db.add(session)
    db.commit()
    log.info("Session created: %s", session.uuid)
    return RedirectResponse(url=f"/s/{session.uuid}/setup", status_code=303)


@router.get("/s/{uuid}")
def resume_session(uuid: str, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    target = "trials" if session.topdog_email else "setup"
    return RedirectResponse(url=f"/s/{uuid}/{target}", status_code=303)


@router.get("/s/{uuid}/setup", response_class=HTMLResponse)
def setup_page(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    return templates.TemplateResponse(
        request, "setup.html",
        {"session": session, "uuid": uuid},
    )


@router.post("/s/{uuid}/sync")
def sync_entries(
    uuid: str,
    email: str = Form(...),
    password: str = Form(...),
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    session.topdog_email = crypto.encrypt(email)
    session.topdog_password = crypto.encrypt(password)
    db.commit()

    from app.queue import get_queue, get_redis
    job = get_queue().enqueue("app.worker.sync_session_job", session.uuid, job_timeout=300)
    get_redis().setex(f"sync_job:{uuid}", 3600, job.id)
    log.info("Sync enqueued for session %s, job %s", uuid, job.id)

    return RedirectResponse(url=f"/s/{uuid}/syncing", status_code=303)


@router.get("/s/{uuid}/syncing", response_class=HTMLResponse)
def syncing_page(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    _get_session(uuid, db)
    from app.queue import get_sync_status
    status = get_sync_status(uuid) or {"message": "Starting sync…", "current": 0, "total": 0}
    return templates.TemplateResponse(
        request, "syncing.html",
        {"uuid": uuid, **status},
    )


@router.get("/s/{uuid}/sync-status", response_class=HTMLResponse)
def sync_status(uuid: str, request: Request):
    from app.queue import get_redis, get_sync_status
    from rq.job import Job

    redis = get_redis()
    job_id_bytes = redis.get(f"sync_job:{uuid}")

    if not job_id_bytes:
        return HTMLResponse(content="", headers={"HX-Redirect": f"/s/{uuid}/trials"})

    job = Job.fetch(job_id_bytes.decode(), connection=redis)

    if job.is_finished:
        return HTMLResponse(content="", headers={"HX-Redirect": f"/s/{uuid}/trials"})

    if job.is_failed:
        log.warning("Sync job failed for session %s: %s", uuid, job.exc_info)
        error = html_lib.escape(str(job.exc_info or "Unknown error")[:300])
        return HTMLResponse(
            f'<div id="sync-status">'
            f'<div class="bg-red-50 border border-red-200 text-red-700 rounded-lg px-4 py-3 mb-4 text-sm">'
            f'Sync failed: {error}</div>'
            f'<a href="/s/{uuid}/setup" class="text-brand hover:underline text-sm">Try again</a>'
            f'</div>'
        )

    status = get_sync_status(uuid) or {"message": "Starting sync…", "current": 0, "total": 0}
    return templates.TemplateResponse(
        request, "partials/_sync_progress.html",
        {"uuid": uuid, **status},
    )


@router.get("/s/{uuid}/settings", response_class=HTMLResponse)
def settings_page(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    return templates.TemplateResponse(
        request, "settings.html",
        {"session": session, "uuid": uuid},
    )


@router.post("/s/{uuid}/logout")
def logout(uuid: str, db: DBSession = Depends(get_db)):
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if session:
        from app.queue import get_redis
        get_redis().delete(f"sync_job:{uuid}")
        db.delete(session)
        db.commit()
        log.info("Session deleted: %s", uuid)
    return RedirectResponse(url="/", status_code=303)


@router.post("/s/{uuid}/settings")
def update_settings(
    uuid: str,
    tpd_200: int = Form(90),
    tpd_300: int = Form(90),
    tpd_400: int = Form(90),
    tpd_500: int = Form(90),
    tpd_600: int = Form(90),
    default_setup_mins: int = Form(10),
    default_walk_mins: int = Form(10),
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    session.tpd_200 = tpd_200
    session.tpd_300 = tpd_300
    session.tpd_400 = tpd_400
    session.tpd_500 = tpd_500
    session.tpd_600 = tpd_600
    session.default_setup_mins = default_setup_mins
    session.default_walk_mins = default_walk_mins
    db.commit()
    return RedirectResponse(url=f"/s/{uuid}/trials", status_code=303)


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
