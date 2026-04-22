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


@router.get("/s/{uuid}/setup", response_class=HTMLResponse)
def setup_page(uuid: str, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    return templates.TemplateResponse(
        request, "setup.html",
        {"session": session, "uuid": uuid},
    )


@router.post("/s/{uuid}/sync")
async def sync_entries(
    uuid: str,
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    session.topdog_email = crypto.encrypt(email)
    session.topdog_password = crypto.encrypt(password)
    db.commit()

    from app.tasks import run_sync
    try:
        await run_sync(session, db)
    except ValueError as e:
        return templates.TemplateResponse(
            request, "setup.html",
            {"session": session, "uuid": uuid, "error": str(e)},
        )

    return RedirectResponse(url=f"/s/{uuid}/trials", status_code=303)


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
