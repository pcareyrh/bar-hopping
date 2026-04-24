import html as html_lib

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session
from app import crypto

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.post("/sessions")
def create_session(db: DBSession = Depends(get_db)):
    session = Session()
    db.add(session)
    db.commit()
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


@router.post("/s/{uuid}/settings")
def update_settings(
    uuid: str,
    avg_time_per_dog: int = Form(90),
    default_setup_mins: int = Form(10),
    default_walk_mins: int = Form(10),
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    session.avg_time_per_dog = avg_time_per_dog
    session.default_setup_mins = default_setup_mins
    session.default_walk_mins = default_walk_mins
    db.commit()
    return RedirectResponse(url=f"/s/{uuid}/trials", status_code=303)


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
