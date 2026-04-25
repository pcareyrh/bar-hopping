from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule
from app.engine.predictor import (
    predict_run,
    predict_run_from_block,
    format_predicted_time,
    flag_conflicts,
)

# Default trial day start time when no parsed schedule is available.
DEFAULT_TRIAL_START = time(9, 0)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/s/{uuid}/trials/{trial_id}/schedule", response_class=HTMLResponse)
def schedule_view(uuid: str, trial_id: int, request: Request, db: DBSession = Depends(get_db)):
    session = _get_session(uuid, db)
    trial = _get_trial(trial_id, db)

    has_class_schedules = (
        db.query(ClassSchedule.id).filter(ClassSchedule.trial_id == trial_id).first() is not None
    )
    day_blocks: list[dict] = []
    if not has_class_schedules:
        day_blocks = _compute_catalogue_blocks(
            trial, db,
            base_start=DEFAULT_TRIAL_START,
            setup_mins=session.default_setup_mins,
            walk_mins=session.default_walk_mins,
            avg_tpd=session.avg_time_per_dog,
        )

    predictions = _build_predictions(session, trial, db, day_blocks=day_blocks)
    flag_conflicts(predictions)

    user_block_keys = {(p["event_name"], p["height_group"]) for p in predictions if not p["pending"]}
    for b in day_blocks:
        b["is_user_block"] = (b["event_name"], b["height_group"]) in user_block_keys
        b["setup_str"] = format_predicted_time(b["setup_start"]) if b["setup_start"] else None
        b["first_run_str"] = format_predicted_time(b["first_run"])
        b["last_run_str"] = format_predicted_time(b["last_run"])

    return templates.TemplateResponse(
        request, "schedule.html",
        {
            "session": session,
            "uuid": uuid,
            "trial": trial,
            "predictions": predictions,
            "day_blocks": day_blocks,
            "trial_start_str": format_predicted_time(
                datetime.combine(trial.start_date or date.today(), DEFAULT_TRIAL_START)
            ) if not has_class_schedules else None,
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


def _ring_of(event_name: str) -> str:
    """Classify a class into a ring. NSW trials with both disciplines run
    agility in one ring and jumping in another in parallel."""
    n = (event_name or "").lower()
    if "jumping" in n:
        return "Jumping"
    return "Agility"


def _compute_catalogue_blocks(
    trial: Trial,
    db: DBSession,
    *,
    base_start: time,
    setup_mins: int,
    walk_mins: int,
    avg_tpd: int,
) -> list[dict]:
    """Estimate when each (event_name, height_group) block runs based purely on
    the catalogue. We assume two rings (agility + jumping) running in parallel
    from base_start. Within each ring, events run in the order they first
    appear in the catalogue; within each event, heights run ascending.
    Each block costs setup+walk before the first dog plus tpd seconds per dog
    (NFC inclusive).

    Returns list of dicts ordered by first_run across both rings:
        event_name, height_group, ring, count, first_run, last_run."""
    cat_entries = (
        db.query(CatalogueEntry)
        .filter(CatalogueEntry.trial_id == trial.id)
        .order_by(CatalogueEntry.id)
        .all()
    )
    if not cat_entries:
        return []

    # Group by (event, height) with counts; preserve catalogue encounter order
    # for both the event sequence and the heights within each event (NSW
    # judging order doesn't always run heights ascending).
    counts: dict[tuple[str, int], int] = {}
    event_order: dict[str, int] = {}
    event_heights: dict[str, list[int]] = {}
    for ce in cat_entries:
        counts[(ce.event_name, ce.height_group)] = counts.get((ce.event_name, ce.height_group), 0) + 1
        if ce.event_name not in event_order:
            event_order[ce.event_name] = len(event_order)
        heights = event_heights.setdefault(ce.event_name, [])
        if ce.height_group not in heights:
            heights.append(ce.height_group)

    # Build per-ring running order.
    rings: dict[str, list[dict]] = {}
    for event in sorted(event_heights, key=lambda e: event_order[e]):
        ring = _ring_of(event)
        ring_list = rings.setdefault(ring, [])
        for height in event_heights[event]:
            ring_list.append({
                "event_name": event,
                "height_group": height,
                "ring": ring,
                "count": counts[(event, height)],
            })

    # Walk each ring sequentially from base_start. Setup + walk happens once
    # per event (judge briefing + course walk for the new course), not once
    # per height — heights of the same event run back-to-back.
    base_date = trial.start_date or date.today()
    out: list[dict] = []
    for ring_name, blocks in rings.items():
        cursor = datetime.combine(base_date, base_start)
        last_event: str | None = None
        for b in blocks:
            if b["event_name"] != last_event:
                b["setup_start"] = cursor
                b["setup_mins"] = setup_mins
                b["walk_mins"] = walk_mins
                cursor += timedelta(minutes=setup_mins + walk_mins)
                last_event = b["event_name"]
            else:
                b["setup_start"] = None
                b["setup_mins"] = 0
                b["walk_mins"] = 0
            b["first_run"] = cursor
            cursor += timedelta(seconds=b["count"] * avg_tpd)
            b["last_run"] = cursor
            out.append(b)

    out.sort(key=lambda b: b["first_run"])
    return out


def _build_predictions(
    session: Session,
    trial: Trial,
    db: DBSession,
    *,
    day_blocks: list[dict] | None = None,
) -> list[dict]:
    entries = (
        db.query(SessionEntry)
        .filter(SessionEntry.session_uuid == session.uuid, SessionEntry.trial_id == trial.id)
        .all()
    )

    if day_blocks is None:
        # Recompute when called from the override handler — cheap.
        has_class_schedules = (
            db.query(ClassSchedule.id).filter(ClassSchedule.trial_id == trial.id).first() is not None
        )
        day_blocks = (
            []
            if has_class_schedules
            else _compute_catalogue_blocks(
                trial, db,
                base_start=DEFAULT_TRIAL_START,
                setup_mins=session.default_setup_mins,
                walk_mins=session.default_walk_mins,
                avg_tpd=session.avg_time_per_dog,
            )
        )
    block_starts: dict[tuple[str, int], datetime] = {
        (b["event_name"], b["height_group"]): b["first_run"] for b in day_blocks
    }

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
        elif (ce.event_name, ce.height_group) in block_starts:
            pred = predict_run_from_block(
                block_first_run=block_starts[(ce.event_name, ce.height_group)],
                run_position=ce.run_position,
                avg_time_per_dog=session.avg_time_per_dog,
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
