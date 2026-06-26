import os
import re
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import (
    Session,
    Trial,
    SessionEntry,
    CatalogueEntry,
    ClassSchedule,
    TrialLunchBreak,
    EventLiveTiming,
    EventDurationStat,
)
from app.engine.predictor import (
    predict_run,
    predict_run_from_block,
    predict_run_from_event,
    format_predicted_time,
    flag_conflicts,
)

# Default trial day start time when no parsed schedule is available.
DEFAULT_TRIAL_START = time(9, 0)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/s/{uuid}/trials/{trial_id}/schedule", response_class=HTMLResponse)
def schedule_view(uuid: str, trial_id: int, request: Request, db: DBSession = Depends(get_db), day: int | None = Query(default=None)):
    session = _get_session(uuid, db)
    trial = _get_trial(trial_id, db)

    trial_start = trial.start_time or DEFAULT_TRIAL_START
    has_class_schedules = (
        db.query(ClassSchedule.id).filter(ClassSchedule.trial_id == trial_id).first() is not None
    )
    lb_records = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial_id).all()
    lunch_breaks = {(r.day, r.ring): (r.lunch_break_at, r.lunch_break_mins) for r in lb_records}

    live_enabled = os.getenv("LIVE_PREDICTIONS_ENABLED", "1") != "0"
    event_timings = _event_timings_for_trial(trial, db) if live_enabled else None
    duration_stats = _duration_stats_for_trial(trial, db) if live_enabled else None

    day_blocks = _compute_catalogue_blocks(
        trial, db,
        base_start=trial_start,
        setup_mins=session.default_setup_mins,
        walk_mins=session.default_walk_mins,
        tpd_for_height=session.tpd_for,
        lunch_breaks=lunch_breaks,
        event_timings=event_timings,
        duration_stats=duration_stats,
    )

    predictions = _build_predictions(
        session, trial, db,
        day_blocks=day_blocks,
        lunch_breaks=lunch_breaks,
        event_timings=event_timings,
        duration_stats=duration_stats,
    )
    flag_conflicts(predictions)

    ring_offsets: dict[str, str] = {}
    for p in predictions:
        ring_label = p.get("ring_label")
        offset_str = p.get("ring_offset_str")
        if ring_label and offset_str and ring_label not in ring_offsets:
            ring_offsets[ring_label] = offset_str

    any_running = False
    if live_enabled and event_timings:
        any_running = any(
            t.started_at is not None and t.finished_at is None
            for t in event_timings.values()
        )

    user_block_keys = {(p["day"], p["event_name"], p["height_group"]) for p in predictions if not p["pending"]}
    for b in day_blocks:
        b.setdefault("is_lunch_break", False)
        b["is_user_block"] = (b["day"], b["event_name"], b["height_group"]) in user_block_keys
        b["setup_str"] = format_predicted_time(b["setup_start"]) if b["setup_start"] else None
        b["first_run_str"] = format_predicted_time(b["first_run"])
        b["last_run_str"] = format_predicted_time(b["last_run"])

    available_days = sorted({b["day"] for b in day_blocks})
    selected_day = day if day in available_days else (available_days[0] if available_days else 1)
    day_dates = {b["day"]: b.get("trial_date") for b in day_blocks if not b.get("is_lunch_break")}
    day_blocks_view = [b for b in day_blocks if b["day"] == selected_day]

    return templates.TemplateResponse(
        request, "schedule.html",
        {
            "session": session,
            "uuid": uuid,
            "trial": trial,
            "predictions": predictions,
            "day_blocks": day_blocks_view,
            "available_days": available_days,
            "selected_day": selected_day,
            "day_dates": day_dates,
            "multi_day": len(available_days) > 1,
            "trial_start_str": format_predicted_time(
                datetime.combine(trial.start_date or date.today(), trial_start)
            ) if not has_class_schedules else None,
            "has_class_schedules": has_class_schedules,
            "live_status": trial.live_status,
            "live_synced_at": trial.live_synced_at,
            "ring_offsets": ring_offsets,
            "any_running": any_running,
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


def _strip_event_code(name: str | None) -> str:
    """Strip a trailing " (CODE)" / " (CODE1)" session/round suffix.

    Nationals-style catalogues split one logical class into separately-coded
    runs — e.g. "Masters Agility (ADM1)", "Masters Agility (ADM2)", or finals
    rounds "Open Agility (ADO1/2/3)" — while the TopDog /entries page lists the
    bare class name once per run. Normalizing both to "Masters Agility" lets a
    single SessionEntry fan out to every run. Mirrors _norm in
    worker._resolve_catalogue_links.
    """
    return re.sub(r"\s*\([A-Z]{2,4}\d*\)\s*$", "", name or "").strip()


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


def _event_timings_for_trial(trial: Trial, db: DBSession) -> dict:
    """Map (day, ring_number, event_name, height_group) → EventLiveTiming row."""
    rows = db.query(EventLiveTiming).filter(EventLiveTiming.trial_id == trial.id).all()
    return {(r.day, r.ring_number, r.event_name, r.height_group): r for r in rows}


def _duration_stats_for_trial(trial: Trial, db: DBSession) -> dict:
    """Map (event_name, height_group) → EventDurationStat row."""
    rows = db.query(EventDurationStat).filter(EventDurationStat.trial_id == trial.id).all()
    return {(r.event_name, r.height_group): r for r in rows}


def _estimate_segment_duration(
    stat: EventDurationStat | None,
    catalogue_count: int,
    tpd: int,
) -> int:
    if stat and stat.median_duration_s:
        return stat.median_duration_s
    return catalogue_count * tpd


def _ring_changeover_s(
    trial_id: int,
    ring_number: str,
    day: int,
    db: DBSession,
    default_s: int,
) -> int:
    """Gap between consecutive finished→started segments on a ring today, else default."""
    rows = (
        db.query(EventLiveTiming)
        .filter(
            EventLiveTiming.trial_id == trial_id,
            EventLiveTiming.day == day,
            EventLiveTiming.ring_number == ring_number,
            EventLiveTiming.started_at.isnot(None),
        )
        .order_by(EventLiveTiming.started_at)
        .all()
    )
    gaps: list[int] = []
    for i in range(len(rows) - 1):
        prev, nxt = rows[i], rows[i + 1]
        if prev.finished_at and nxt.started_at:
            gap = int((nxt.started_at - prev.finished_at).total_seconds())
            if gap >= 0:
                gaps.append(gap)
    if gaps:
        gaps.sort()
        return gaps[len(gaps) // 2]
    return default_s


def _format_ring_offset(paper_start: datetime | None, measured_start: datetime | None) -> str | None:
    if not paper_start or not measured_start:
        return None
    mins = int(round((paper_start - measured_start).total_seconds() / 60))
    if mins == 0:
        return None
    if mins > 0:
        return f"{mins} min behind"
    return f"{abs(mins)} min ahead"


@router.post("/s/{uuid}/trials/{trial_id}/schedule/lunch-break")
def update_lunch_break(
    uuid: str,
    trial_id: int,
    day: int = Form(...),
    ring: str = Form(...),
    lunch_break_at: str = Form(default=""),
    lunch_break_mins: int = Form(default=45),
    db: DBSession = Depends(get_db),
):
    _get_session(uuid, db)
    _get_trial(trial_id, db)
    if not 0 <= lunch_break_mins <= 480:
        raise HTTPException(status_code=400, detail="lunch_break_mins must be between 0 and 480")
    lb = db.query(TrialLunchBreak).filter_by(trial_id=trial_id, day=day, ring=ring).first()
    if lb is None:
        lb = TrialLunchBreak(trial_id=trial_id, day=day, ring=ring)
        db.add(lb)
    if lunch_break_at.strip():
        try:
            lb.lunch_break_at = datetime.strptime(lunch_break_at.strip(), "%H:%M").time()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid lunch_break_at format; expected HH:MM")
    else:
        lb.lunch_break_at = None
    lb.lunch_break_mins = lunch_break_mins
    db.commit()
    return RedirectResponse(f"/s/{uuid}/settings", status_code=303)


def lunch_break_configs_for_trial(trial: Trial, db: DBSession) -> list[dict]:
    """Return one lunch-break config per (day, ring) pair that has runs in the
    trial catalogue, merged with any saved TrialLunchBreak values.

    Used by the settings page so users can adjust each ring's lunch break. The
    (day, ring) pairs mirror those produced by `_compute_catalogue_blocks` —
    ring names come from `_ring_of` — so saved breaks line up with the blocks
    shown on the Full Day Schedule.
    """
    cat_entries = (
        db.query(CatalogueEntry)
        .filter(CatalogueEntry.trial_id == trial.id)
        .order_by(CatalogueEntry.id)
        .all()
    )
    if not cat_entries:
        return []

    lb_records = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).all()
    lunch_breaks = {(r.day, r.ring): (r.lunch_break_at, r.lunch_break_mins) for r in lb_records}

    from collections import defaultdict
    by_day: dict[int, list] = defaultdict(list)
    for ce in cat_entries:
        by_day[getattr(ce, "day", 1) or 1].append(ce)

    seen: dict[tuple[int, str], dict] = {}
    for day_num in sorted(by_day.keys()):
        day_entries = by_day[day_num]
        event_order: dict[str, int] = {}
        event_heights: dict[str, list[int]] = {}
        event_rings: dict[str, str | None] = {}
        height_rings: dict[tuple[str, int], str | None] = {}
        for ce in day_entries:
            key = (ce.event_name, ce.height_group)
            if ce.event_name not in event_order:
                event_order[ce.event_name] = len(event_order)
                event_rings[ce.event_name] = getattr(ce, "ring_number", None)
            heights = event_heights.setdefault(ce.event_name, [])
            if ce.height_group not in heights:
                heights.append(ce.height_group)
                height_rings[key] = getattr(ce, "ring_number", None)

        for event in sorted(event_heights, key=lambda e: event_order[e]):
            for height in event_heights[event]:
                ring = _ring_of(event, height_rings.get((event, height)) or event_rings.get(event))
                key = (day_num, ring)
                if key not in seen:
                    lb = lunch_breaks.get(key)
                    seen[key] = {
                        "day": day_num,
                        "ring": ring,
                        "lunch_break_at": lb[0] if lb else None,
                        "lunch_break_mins": lb[1] if lb else 45,
                    }
    return [seen[k] for k in sorted(seen)]


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
    lunch_breaks: dict[tuple[int, str], tuple[time | None, int]] | None = None,
    event_timings: dict | None = None,
    duration_stats: dict | None = None,
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
            lunch_injected = False
            ring_lb = (lunch_breaks or {}).get((day_num, ring_name))
            lb_at: time | None = ring_lb[0] if ring_lb else None
            lb_mins: int = ring_lb[1] if ring_lb else 45
            bare_ring = _bare_ring(ring_name)
            changeover_s = (
                _ring_changeover_s(
                    trial.id, bare_ring, day_num, db,
                    (setup_mins + walk_mins) * 60,
                )
                if event_timings and bare_ring
                else (setup_mins + walk_mins) * 60
            )
            for b in blocks:
                timing = (
                    event_timings.get((day_num, bare_ring, b["event_name"], b["height_group"]))
                    if event_timings and bare_ring
                    else None
                )
                tpd = tpd_for_height(b["height_group"], b["event_name"])

                if timing and timing.finished_at:
                    b["setup_start"] = None
                    b["setup_mins"] = 0
                    b["walk_mins"] = 0
                    b["first_run"] = timing.started_at or timing.finished_at
                    b["last_run"] = timing.finished_at
                    cursor = timing.finished_at
                    last_event = b["event_name"]
                elif timing and timing.started_at:
                    b["setup_start"] = None
                    b["setup_mins"] = 0
                    b["walk_mins"] = 0
                    b["first_run"] = timing.started_at
                    dur_s = _estimate_segment_duration(
                        (duration_stats or {}).get((b["event_name"], b["height_group"])),
                        b["count"],
                        tpd,
                    )
                    b["last_run"] = timing.started_at + timedelta(seconds=dur_s)
                    cursor = b["last_run"]
                    last_event = b["event_name"]
                elif event_timings:
                    if b["event_name"] != last_event:
                        b["setup_mins"] = setup_mins
                        b["walk_mins"] = walk_mins
                        if last_event is None:
                            b["setup_start"] = cursor - timedelta(minutes=setup_mins + walk_mins)
                        else:
                            b["setup_start"] = cursor
                            cursor += timedelta(seconds=changeover_s)
                        last_event = b["event_name"]
                    else:
                        b["setup_start"] = None
                        b["setup_mins"] = 0
                        b["walk_mins"] = 0
                    b["first_run"] = cursor
                    dur_s = _estimate_segment_duration(
                        (duration_stats or {}).get((b["event_name"], b["height_group"])),
                        b["count"],
                        tpd,
                    )
                    cursor += timedelta(seconds=dur_s)
                    b["last_run"] = cursor
                else:
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
                    cursor += timedelta(seconds=b["count"] * tpd)
                    b["last_run"] = cursor
                out.append(b)
                if lb_at and not lunch_injected and cursor.time() >= lb_at:
                    lunch_start = cursor
                    cursor += timedelta(minutes=lb_mins)
                    out.append({
                        "event_name": "Lunch Break",
                        "height_group": None,
                        "ring": ring_name,
                        "count": 0,
                        "day": day_num,
                        "trial_date": day_date,
                        "setup_start": None,
                        "setup_mins": 0,
                        "walk_mins": 0,
                        "first_run": lunch_start,
                        "last_run": cursor,
                        "is_lunch_break": True,
                        "duration_mins": lb_mins,
                    })
                    lunch_injected = True

    out.sort(key=lambda b: b["first_run"])
    return out


def _build_predictions(
    session: Session,
    trial: Trial,
    db: DBSession,
    *,
    day_blocks: list[dict] | None = None,
    lunch_breaks: dict[tuple[int, str], tuple[time | None, int]] | None = None,
    event_timings: dict | None = None,
    duration_stats: dict | None = None,
) -> list[dict]:
    entries = (
        db.query(SessionEntry)
        .filter(SessionEntry.session_uuid == session.uuid, SessionEntry.trial_id == trial.id)
        .all()
    )

    live_enabled = os.getenv("LIVE_PREDICTIONS_ENABLED", "1") != "0"
    block_kwargs = dict(
        base_start=trial.start_time or DEFAULT_TRIAL_START,
        setup_mins=session.default_setup_mins,
        walk_mins=session.default_walk_mins,
        tpd_for_height=session.tpd_for,
    )

    if lunch_breaks is None:
        lb_records = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).all()
        lunch_breaks = {(r.day, r.ring): (r.lunch_break_at, r.lunch_break_mins) for r in lb_records}

    paper_block_starts: dict[tuple[str, int, int], datetime] = {}
    if live_enabled:
        if event_timings is None:
            event_timings = _event_timings_for_trial(trial, db)
        if duration_stats is None:
            duration_stats = _duration_stats_for_trial(trial, db)
        paper_blocks = _compute_catalogue_blocks(trial, db, lunch_breaks=lunch_breaks, **block_kwargs)
        paper_block_starts = {
            (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"]
            for b in paper_blocks
        }
        if day_blocks is None:
            day_blocks = _compute_catalogue_blocks(
                trial, db,
                lunch_breaks=lunch_breaks,
                event_timings=event_timings,
                duration_stats=duration_stats,
                **block_kwargs,
            )
    elif day_blocks is None:
        day_blocks = _compute_catalogue_blocks(trial, db, lunch_breaks=lunch_breaks, **block_kwargs)
        event_timings = {}
    else:
        event_timings = event_timings or {}

    block_starts: dict[tuple[str, int, int], datetime] = {
        (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"] for b in day_blocks
    }

    all_class_schedules = (
        db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial.id).all()
    )

    # Index every catalogue entry by (stripped event_name, cat_number) so a
    # single SessionEntry can fan out to every run of the class it covers. A
    # logical class may span several days (same name, different day) and/or be
    # split into separately-coded runs/rounds (e.g. ADM1/ADM2, ADO1/2/3) that
    # share a calendar day. The /entries page lists each run once under the
    # bare class name, but the SessionEntry dedup collapses them; stripping the
    # "(CODE)" suffix here regroups all of a dog's runs of the class. cat_number
    # is unique to one dog within a trial, so this never merges across dogs.
    from collections import defaultdict
    cat_by_key: dict[tuple[str, str], list[CatalogueEntry]] = defaultdict(list)
    for c in db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial.id).all():
        cat_by_key[(_strip_event_code(c.event_name), c.cat_number)].append(c)

    def _predict_for_ce(entry: SessionEntry, ce: CatalogueEntry) -> dict:
        ce_day = getattr(ce, "day", 1) or 1
        cs = _match_class_schedule(all_class_schedules, ce.event_name, ce_day)

        # Anchor each day's prediction on its own calendar date so multi-day
        # rows sort correctly and don't false-conflict across days.
        day_date = (trial.start_date + timedelta(days=ce_day - 1)) if trial.start_date else None

        height_tpd = session.tpd_for(ce.height_group, ce.event_name)
        raw_ring = entry.ring_number or (cs.ring_number if cs else None) or getattr(ce, "ring_number", None)
        bare_ring = _bare_ring(raw_ring)

        timing = (
            event_timings.get((ce_day, bare_ring, ce.event_name, ce.height_group))
            if live_enabled and event_timings and bare_ring
            else None
        )

        prediction_source = "scheduled"
        event_started_at = None
        ring_offset_str = None
        live_finished = False
        pred: dict = {}

        if timing and timing.started_at and not timing.finished_at:
            pred = predict_run_from_event(
                event_started_at=timing.started_at,
                run_position=ce.run_position,
                avg_time_per_dog=height_tpd,
                position_override=entry.position_override,
                time_per_dog_override=entry.time_per_dog_override,
            )
            predicted_start = pred["predicted_start"]
            predicted_start_str = format_predicted_time(predicted_start)
            prediction_source = "event_live"
            event_started_at = timing.started_at
            ring_offset_str = _format_ring_offset(
                paper_block_starts.get((ce.event_name, ce.height_group, ce_day)),
                timing.started_at,
            )
        elif timing and timing.finished_at:
            if timing.started_at:
                pred = predict_run_from_event(
                    event_started_at=timing.started_at,
                    run_position=ce.run_position,
                    avg_time_per_dog=height_tpd,
                    position_override=entry.position_override,
                    time_per_dog_override=entry.time_per_dog_override,
                )
                predicted_start = pred["predicted_start"]
                predicted_start_str = format_predicted_time(predicted_start)
                event_started_at = timing.started_at
            else:
                pred = {}
                predicted_start = None
                predicted_start_str = None
            prediction_source = "event_live"
            live_finished = True
        elif cs and cs.scheduled_start:
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
        elif (ce.event_name, ce.height_group, ce_day) in block_starts:
            pred = predict_run_from_block(
                block_first_run=block_starts[(ce.event_name, ce.height_group, ce_day)],
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
            "day": ce_day,
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
            "prediction_source": prediction_source,
            "event_started_at": event_started_at,
            "ring_offset_str": ring_offset_str,
            "live_finished": live_finished,
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
                "prediction_source": "scheduled",
                "event_started_at": None,
                "ring_offset_str": None,
                "live_finished": False,
            })
            continue

        # Fan out to every run this dog has of this class (one card per run,
        # across days and/or coded rounds). Sort by day then run_position so
        # same-day rounds keep their running order.
        day_entries = sorted(
            cat_by_key.get((_strip_event_code(ce.event_name), ce.cat_number), [ce]),
            key=lambda c: (getattr(c, "day", 1) or 1, c.run_position or 0),
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
