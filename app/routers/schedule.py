from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule
from app.engine.predictor import predict_run, format_predicted_time, flag_conflicts

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/s/{uuid}/trials/{trial_id}/schedule", response_class=HTMLResponse)
def schedule_view(uuid: str, trial_id: int, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    trial = _get_trial(trial_id, db)
    predictions = _build_predictions(session, trial, db)
    flag_conflicts(predictions)

    return templates.TemplateResponse(
        request, "schedule.html",
        {
            "session": session,
            "uuid": uuid,
            "trial": trial,
            "predictions": predictions,
        },
    )


@router.post("/s/{uuid}/trials/{trial_id}/schedule/{entry_id}/override", response_class=HTMLResponse)
def update_override(
    uuid: str,
    trial_id: int,
    entry_id: int,
    request: Request,
    position_override: str = Form(default=""),
    time_per_dog_override: str = Form(default=""),
    ring_setup_mins: str = Form(default=""),
    walk_mins: str = Form(default=""),
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    trial = _get_trial(trial_id, db)
    entry = db.query(SessionEntry).filter(
        SessionEntry.id == entry_id,
        SessionEntry.session_uuid == uuid,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    entry.position_override = int(position_override) if position_override.strip() else None
    entry.time_per_dog_override = int(time_per_dog_override) if time_per_dog_override.strip() else None

    if entry.catalogue_entry and entry.ring_number:
        cs = db.query(ClassSchedule).filter(
            ClassSchedule.trial_id == trial_id,
            ClassSchedule.ring_number == entry.ring_number,
            ClassSchedule.class_name == entry.event_name,
        ).first()
        if cs:
            if ring_setup_mins.strip():
                cs.ring_setup_mins = int(ring_setup_mins)
            if walk_mins.strip():
                cs.walk_mins = int(walk_mins)

    db.commit()

    predictions = _build_predictions(session, trial, db)
    flag_conflicts(predictions)

    pred = next((p for p in predictions if p["entry_id"] == entry_id), None)
    return templates.TemplateResponse(
        request, "partials/run_card.html",
        {"session": session, "uuid": uuid, "trial": trial, "p": pred},
    )


def _build_predictions(session: Session, trial: Trial, db: DBSession) -> list[dict]:
    entries = (
        db.query(SessionEntry)
        .filter(SessionEntry.session_uuid == session.uuid, SessionEntry.trial_id == trial.id)
        .all()
    )

    predictions = []
    for entry in entries:
        ce: CatalogueEntry | None = entry.catalogue_entry
        if ce is None:
            predictions.append({
                "entry_id": entry.id,
                "dog_name": entry.dog_name,
                "event_name": entry.event_name,
                "height_group": entry.height_group,
                "cat_number": entry.cat_number,
                "ring_number": entry.ring_number,
                "run_position": None,
                "height_group_total": None,
                "nfc": False,
                "predicted_start": None,
                "predicted_start_str": None,
                "effective_position": None,
                "effective_tpd": None,
                "pending": True,
                "position_override": entry.position_override,
                "time_per_dog_override": entry.time_per_dog_override,
                "conflict": False,
            })
            continue

        cs = db.query(ClassSchedule).filter(
            ClassSchedule.trial_id == trial.id,
            ClassSchedule.class_name == ce.event_name,
        ).first()

        if cs and cs.scheduled_start:
            pred = predict_run(
                scheduled_start=cs.scheduled_start,
                ring_setup_mins=cs.ring_setup_mins or session.default_setup_mins,
                walk_mins=cs.walk_mins or session.default_walk_mins,
                run_position=ce.run_position,
                avg_time_per_dog=session.avg_time_per_dog,
                trial_date=trial.start_date,
                position_override=entry.position_override,
                time_per_dog_override=entry.time_per_dog_override,
            )
            predicted_start = pred["predicted_start"]
            predicted_start_str = format_predicted_time(predicted_start)
        else:
            pred = {}
            predicted_start = None
            predicted_start_str = None

        predictions.append({
            "entry_id": entry.id,
            "dog_name": entry.dog_name,
            "event_name": ce.event_name,
            "height_group": ce.height_group,
            "cat_number": ce.cat_number,
            "ring_number": entry.ring_number or (cs.ring_number if cs else None),
            "run_position": ce.run_position,
            "height_group_total": ce.height_group_total,
            "nfc": ce.nfc,
            "predicted_start": predicted_start,
            "predicted_start_str": predicted_start_str,
            "effective_position": pred.get("effective_position", ce.run_position),
            "effective_tpd": pred.get("effective_tpd", session.avg_time_per_dog),
            "pending": False,
            "position_override": entry.position_override,
            "time_per_dog_override": entry.time_per_dog_override,
            "conflict": False,
        })

    predictions.sort(key=lambda p: (p["predicted_start"] is None, p["predicted_start"] or ""))
    return predictions


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _get_trial(trial_id: int, db: DBSession) -> Trial:
    trial = db.query(Trial).filter(Trial.id == trial_id).first()
    if not trial:
        raise HTTPException(status_code=404, detail="Trial not found")
    return trial
