import re
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

    trial_start = trial.start_time or DEFAULT_TRIAL_START
    day_blocks = _compute_catalogue_blocks(
        trial, db,
        base_start=trial_start,
        setup_mins=session.default_setup_mins,
        walk_mins=session.default_walk_mins,
        tpd_for_height=session.tpd_for,
    )

    predictions = _build_predictions(session, trial, db, day_blocks=day_blocks)
    flag_conflicts(predictions)

    user_block_keys = {(p["day"], p["event_name"], p["height_group"]) for p in predictions if not p["pending"]}
    for b in day_blocks:
        b["is_user_block"] = (b["day"], b["event_name"], b["height_group"]) in user_block_keys
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
                datetime.combine(trial.start_date or date.today(), trial_start)
            ),
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
    catalogue_entry_id: str = Form(default=""),
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
        normalized_ring = _bare_ring(entry.ring_number)
        ce_day = getattr(entry.catalogue_entry, "day", 1) or 1
        cs_list = db.query(ClassSchedule).filter(
            ClassSchedule.trial_id == trial_id,
            ClassSchedule.class_name == entry.event_name,
        ).all()
        ring_matches = [c for c in cs_list if _bare_ring(c.ring_number) == normalized_ring]
        # Prefer the row for this entry's day; fall back to a day-agnostic row.
        cs = next((c for c in ring_matches if c.day == ce_day), None) \
            or next((c for c in ring_matches if c.day is None), None)
        if cs:
            if ring_setup_mins.strip():
                cs.ring_setup_mins = int(ring_setup_mins)
            if walk_mins.strip():
                cs.walk_mins = int(walk_mins)

    db.commit()

    predictions = _build_predictions(session, trial, db)
    flag_conflicts(predictions)

    # Re-render the specific day's card. A multi-day entry has one prediction
    # per day, so match the catalogue_entry_id too; fall back to the entry.
    ce_id = int(catalogue_entry_id) if catalogue_entry_id.strip() else None
    pred = next(
        (p for p in predictions
         if p["entry_id"] == entry_id and (ce_id is None or p["catalogue_entry_id"] == ce_id)),
        None,
    )
    if pred is None:
        pred = next((p for p in predictions if p["entry_id"] == entry_id), None)
    return templates.TemplateResponse(
        request, "partials/run_card.html",
        {"session": session, "uuid": uuid, "trial": trial, "p": pred},
    )


def _bare_ring(value: str | None) -> str | None:
    """Normalize a ring_number value to its bare identifier (e.g. "1", "2").

    Handles formats like "Ring 1", "ring 2", "1", etc.
    """
    if not value:
        return None
    bare = re.sub(r"^ring\s*", "", value.strip(), flags=re.I).strip()
    return bare or None


def _ring_label(value: str | None) -> str | None:
    """Return a display label like 'Ring 1' from any ring_number format."""
    bare = _bare_ring(value)
    return f"Ring {bare}" if bare else None


def _ring_of(event_name: str, ring_number: str | None = None) -> str:
    """Classify a class into a ring.

    Prefer the explicit ring_number from the catalogue (e.g. "1", "2") when
    available. Otherwise fall back to the discipline-based heuristic — NSW
    trials with both disciplines run agility in one ring and jumping in
    another in parallel.
    """
    if ring_number:
        bare = _bare_ring(ring_number)
        if bare:
            return f"Ring {bare}"
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
    tpd_for_height,
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

    # Group entries by day number.
    from collections import defaultdict
    by_day: dict[int, list] = defaultdict(list)
    for ce in cat_entries:
        by_day[getattr(ce, "day", 1) or 1].append(ce)

    base_date = trial.start_date or date.today()
    out: list[dict] = []

    for day_num in sorted(by_day.keys()):
        day_entries = by_day[day_num]
        day_date = base_date + timedelta(days=day_num - 1)

        # Group by (event, height); preserve catalogue encounter order.
        counts: dict[tuple[str, int], int] = {}
        max_total: dict[tuple[str, int], int] = {}
        event_order: dict[str, int] = {}
        event_heights: dict[str, list[int]] = {}
        event_rings: dict[str, str | None] = {}   # fallback: first ring seen per event
        height_rings: dict[tuple[str, int], str | None] = {}  # ring per (event, height)
        for ce in day_entries:
            key = (ce.event_name, ce.height_group)
            counts[key] = counts.get(key, 0) + 1
            max_total[key] = max(max_total.get(key, 0), ce.height_group_total or 0)
            if ce.event_name not in event_order:
                event_order[ce.event_name] = len(event_order)
                event_rings[ce.event_name] = getattr(ce, "ring_number", None)
            heights = event_heights.setdefault(ce.event_name, [])
            if ce.height_group not in heights:
                heights.append(ce.height_group)
                height_rings[key] = getattr(ce, "ring_number", None)

        # For HTML sentinel entries (run_position=0, one row per class/height),
        # height_group_total is the real dog count; use whichever is larger.
        for key in counts:
            counts[key] = max(counts[key], max_total[key])

        # Build per-ring running order.
        # Use per-(event, height) ring so classes split across rings at Nationals
        # (e.g. ADM1 400mm in Ring 7 while 500mm runs in Ring 4) land correctly.
        rings: dict[str, list[dict]] = {}
        for event in sorted(event_heights, key=lambda e: event_order[e]):
            for height in event_heights[event]:
                ring = _ring_of(event, height_rings.get((event, height)) or event_rings.get(event))
                rings.setdefault(ring, []).append({
                    "event_name": event,
                    "height_group": height,
                    "ring": ring,
                    "count": counts[(event, height)],
                    "day": day_num,
                    "trial_date": day_date,
                })

        for ring_name, blocks in rings.items():
            cursor = datetime.combine(day_date, base_start)
            last_event: str | None = None
            for b in blocks:
                if b["event_name"] != last_event:
                    b["setup_mins"] = setup_mins
                    b["walk_mins"] = walk_mins
                    if last_event is None:
                        # First event of the day: setup/walk happen before base_start
                        b["setup_start"] = cursor - timedelta(minutes=setup_mins + walk_mins)
                    else:
                        b["setup_start"] = cursor
                        cursor += timedelta(minutes=setup_mins + walk_mins)
                    last_event = b["event_name"]
                else:
                    b["setup_start"] = None
                    b["setup_mins"] = 0
                    b["walk_mins"] = 0
                b["first_run"] = cursor
                cursor += timedelta(seconds=b["count"] * tpd_for_height(b["height_group"], b["event_name"]))
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
        day_blocks = _compute_catalogue_blocks(
            trial, db,
            base_start=trial.start_time or DEFAULT_TRIAL_START,
            setup_mins=session.default_setup_mins,
            walk_mins=session.default_walk_mins,
            tpd_for_height=session.tpd_for,
        )
    block_starts: dict[tuple[str, int, int], datetime] = {
        (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"] for b in day_blocks
    }

    all_class_schedules = (
        db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial.id).all()
    )

    # Index every catalogue entry by (event_name, cat_number) so a single
    # SessionEntry can fan out to one prediction per day it runs (multi-day
    # trials run the same dog/class on several days). cat_number is unique
    # within a trial's catalogue, so this groups the same dog/class across days.
    from collections import defaultdict
    cat_by_key: dict[tuple[str, str], list[CatalogueEntry]] = defaultdict(list)
    for c in db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).all():
        cat_by_key[(c.event_name, c.cat_number)].append(c)

    def _predict_for_ce(entry: SessionEntry, ce: CatalogueEntry) -> dict:
        ce_day = getattr(ce, "day", 1) or 1
        cs = _match_class_schedule(all_class_schedules, ce.event_name, ce_day)

        # Anchor each day's prediction on its own calendar date so multi-day
        # rows sort correctly and don't false-conflict across days.
        day_date = (trial.start_date + timedelta(days=ce_day - 1)) if trial.start_date else None

        height_tpd = session.tpd_for(ce.height_group, ce.event_name)
        if cs and cs.scheduled_start:
            pred = predict_run(
                scheduled_start=cs.scheduled_start,
                ring_setup_mins=cs.ring_setup_mins or session.default_setup_mins,
                walk_mins=cs.walk_mins or session.default_walk_mins,
                run_position=ce.run_position,
                avg_time_per_dog=height_tpd,
                trial_date=day_date,
                position_override=entry.position_override,
                time_per_dog_override=entry.time_per_dog_override,
            )
            predicted_start = pred["predicted_start"]
            predicted_start_str = format_predicted_time(predicted_start)
        elif (ce.event_name, ce.height_group, getattr(ce, "day", 1) or 1) in block_starts:
            pred = predict_run_from_block(
                block_first_run=block_starts[(ce.event_name, ce.height_group, getattr(ce, "day", 1) or 1)],
                run_position=ce.run_position,
                avg_time_per_dog=height_tpd,
                position_override=entry.position_override,
                time_per_dog_override=entry.time_per_dog_override,
            )
            predicted_start = pred["predicted_start"]
            predicted_start_str = format_predicted_time(predicted_start)
        else:
            pred = {}
            predicted_start = None
            predicted_start_str = None

        raw_ring = entry.ring_number or (cs.ring_number if cs else None) or getattr(ce, "ring_number", None)
        return {
            "entry_id": entry.id,
            "card_id": f"{entry.id}-{ce.id}",
            "dog_name": entry.dog_name,
            "event_name": ce.event_name,
            "height_group": ce.height_group,
            "cat_number": ce.cat_number,
            "catalogue_entry_id": ce.id,
            "ring_number": raw_ring,
            "ring_label": _ring_label(raw_ring),
            "day": getattr(ce, "day", 1) or 1,
            "run_position": ce.run_position,
            "height_group_total": ce.height_group_total,
            "nfc": ce.nfc,
            "predicted_start": predicted_start,
            "predicted_start_str": predicted_start_str,
            "effective_position": pred.get("effective_position", ce.run_position),
            "effective_tpd": pred.get("effective_tpd", height_tpd),
            "pending": False,
            "position_override": entry.position_override,
            "time_per_dog_override": entry.time_per_dog_override,
            "conflict": False,
        }

    predictions = []
    for entry in entries:
        ce: CatalogueEntry | None = entry.catalogue_entry
        if ce is None:
            predictions.append({
                "entry_id": entry.id,
                "card_id": str(entry.id),
                "dog_name": entry.dog_name,
                "event_name": entry.event_name,
                "height_group": entry.height_group,
                "cat_number": entry.cat_number,
                "ring_number": entry.ring_number,
                "ring_label": _ring_label(entry.ring_number),
                "catalogue_entry_id": None,
                "day": None,
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

        # Fan out to every day this dog runs this class (one card per day).
        day_entries = sorted(
            cat_by_key.get((ce.event_name, ce.cat_number), [ce]),
            key=lambda c: getattr(c, "day", 1) or 1,
        )
        for sib in day_entries:
            predictions.append(_predict_for_ce(entry, sib))

    predictions.sort(key=lambda p: (p["predicted_start"] is None, p["predicted_start"] or ""))
    return predictions


def _match_class_schedule(
    schedules: list[ClassSchedule], event_name: str, day: int | None = None
) -> ClassSchedule | None:
    """Find the best matching ClassSchedule for a catalogue event name and day.

    A schedule with day == None applies to any day (legacy / single-day
    schedules). For multi-day trials we prefer a day-specific match so the
    same class on different days picks up its own start time, falling back to
    a day-agnostic row. Within each day pass, tries exact name match first,
    then case-insensitive substring containment ('Agility' vs 'Masters Agility').
    """
    en = event_name.lower()

    def _match_in(candidates: list[ClassSchedule]) -> ClassSchedule | None:
        for cs in candidates:
            if cs.class_name == event_name:
                return cs
        for cs in candidates:
            cn = cs.class_name.lower()
            if cn in en or en in cn:
                return cs
        return None

    if day is not None:
        # Day-specific rows win over day-agnostic ones.
        hit = _match_in([cs for cs in schedules if cs.day == day])
        if hit:
            return hit
    return _match_in([cs for cs in schedules if cs.day is None])


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
