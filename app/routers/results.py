"""Past trial results — web-image-only.

Renders historical run rows and per-dog stats from `trial_results` joined
through `dogs` ↔ `session_entries`. No scraper imports.
"""
import logging
import os
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, Request, HTTPException, Header, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, case, and_
from sqlalchemy.orm import Session as DBSession

from app.database import get_db
from app.models import (
    Session, SessionEntry, Trial, TrialResult, Dog,
    normalise_name, normalise_handler,
)

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

PAGE_SIZE = 50


def _get_session(uuid: str, db: DBSession) -> Session:
    session = db.query(Session).filter(Session.uuid == uuid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _session_dog_ids(session: Session, db: DBSession) -> list[int]:
    """Resolve which Dog rows correspond to this session's entries.

    Joins are by normalised (name, handler). Handler is unknown for
    SessionEntry-only dogs, so we look those up by name only when the
    dog has just one Dog row in the table.
    """
    raw_names = (
        db.query(SessionEntry.dog_name)
        .filter(SessionEntry.session_uuid == session.uuid)
        .filter(SessionEntry.dog_name.isnot(None))
        .distinct()
        .all()
    )
    norm_names = {normalise_name(n) for (n,) in raw_names}
    norm_names.discard(None)
    if not norm_names:
        return []
    rows = db.query(Dog.id).filter(Dog.name_normalised.in_(norm_names)).all()
    return [r[0] for r in rows]


def _apply_filters(q, filters: dict):
    if filters.get("dog_id"):
        q = q.filter(TrialResult.dog_id == filters["dog_id"])
    if filters.get("class_slug"):
        q = q.filter(TrialResult.class_slug == filters["class_slug"])
    if filters.get("height_group"):
        q = q.filter(TrialResult.height_group == filters["height_group"])
    if filters.get("status") == "clean":
        q = q.filter(TrialResult.total_faults == 0, TrialResult.status.is_(None))
    elif filters.get("status") == "sub_sct":
        q = q.filter(
            TrialResult.time_seconds.isnot(None),
            TrialResult.sct_seconds.isnot(None),
            TrialResult.time_seconds <= TrialResult.sct_seconds,
        )
    elif filters.get("status") == "dq":
        q = q.filter(TrialResult.status == "DQ")
    elif filters.get("status") == "abs":
        q = q.filter(TrialResult.status == "ABS")
    if filters.get("date_from"):
        q = q.filter(Trial.start_date >= filters["date_from"])
    if filters.get("date_to"):
        q = q.filter(Trial.start_date <= filters["date_to"])
    return q


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _per_dog_stats(db: DBSession, dog_ids: list[int]) -> list[dict]:
    """Aggregate stats per dog using portable SQL only."""
    if not dog_ids:
        return []

    # Per-dog aggregate row.
    completed_expr = case(
        (and_(TrialResult.status.is_(None), TrialResult.time_seconds.isnot(None)), 1),
        else_=0,
    )
    clean_expr = case(
        (and_(TrialResult.status.is_(None), TrialResult.total_faults == 0), 1),
        else_=0,
    )
    sub_sct_expr = case(
        (
            and_(
                TrialResult.status.is_(None),
                TrialResult.time_seconds.isnot(None),
                TrialResult.sct_seconds.isnot(None),
                TrialResult.time_seconds <= TrialResult.sct_seconds,
            ),
            1,
        ),
        else_=0,
    )
    dq_expr = case((TrialResult.status == "DQ", 1), else_=0)
    abs_expr = case((TrialResult.status == "ABS", 1), else_=0)

    rows = (
        db.query(
            Dog.id.label("dog_id"),
            Dog.name.label("name"),
            Dog.handler_name.label("handler_name"),
            func.count(TrialResult.id).label("total"),
            func.sum(completed_expr).label("completed"),
            func.sum(clean_expr).label("clean"),
            func.sum(sub_sct_expr).label("sub_sct"),
            func.sum(dq_expr).label("dq"),
            func.sum(abs_expr).label("abs_"),
            func.min(Trial.start_date).label("first_date"),
            func.max(Trial.start_date).label("last_date"),
            func.count(func.distinct(TrialResult.trial_id)).label("trial_count"),
        )
        .join(TrialResult, TrialResult.dog_id == Dog.id)
        .join(Trial, Trial.id == TrialResult.trial_id)
        .filter(Dog.id.in_(dog_ids))
        .group_by(Dog.id, Dog.name, Dog.handler_name)
        .all()
    )

    out = []
    for r in rows:
        completed = r.completed or 0
        out.append({
            "dog_id": r.dog_id,
            "name": r.name,
            "handler_name": r.handler_name,
            "total": r.total or 0,
            "completed": completed,
            "clean": r.clean or 0,
            "sub_sct": r.sub_sct or 0,
            "dq": r.dq or 0,
            "abs": r.abs_ or 0,
            "clean_rate": (r.clean / completed) if completed else None,
            "sub_sct_rate": (r.sub_sct / completed) if completed else None,
            "dq_rate": (r.dq / r.total) if r.total else None,
            "first_date": r.first_date,
            "last_date": r.last_date,
            "trial_count": r.trial_count or 0,
        })
    out.sort(key=lambda d: d["name"].lower())
    return out


def _per_class_height_stats(db: DBSession, dog_id: int) -> list[dict]:
    completed_expr = case(
        (and_(TrialResult.status.is_(None), TrialResult.time_seconds.isnot(None)), 1),
        else_=0,
    )
    clean_expr = case(
        (and_(TrialResult.status.is_(None), TrialResult.total_faults == 0), 1),
        else_=0,
    )
    sub_sct_expr = case(
        (
            and_(
                TrialResult.status.is_(None),
                TrialResult.time_seconds.isnot(None),
                TrialResult.sct_seconds.isnot(None),
                TrialResult.time_seconds <= TrialResult.sct_seconds,
            ),
            1,
        ),
        else_=0,
    )
    delta_expr = case(
        (
            and_(
                TrialResult.status.is_(None),
                TrialResult.time_seconds.isnot(None),
                TrialResult.sct_seconds.isnot(None),
            ),
            TrialResult.time_seconds - TrialResult.sct_seconds,
        ),
        else_=None,
    )

    rows = (
        db.query(
            TrialResult.class_slug,
            TrialResult.class_label,
            TrialResult.height_group,
            func.count(TrialResult.id).label("total"),
            func.sum(completed_expr).label("completed"),
            func.sum(clean_expr).label("clean"),
            func.sum(sub_sct_expr).label("sub_sct"),
            func.min(TrialResult.time_seconds).label("fastest"),
            func.avg(delta_expr).label("avg_delta"),
        )
        .filter(TrialResult.dog_id == dog_id)
        .group_by(TrialResult.class_slug, TrialResult.class_label, TrialResult.height_group)
        .order_by(TrialResult.class_label, TrialResult.height_group)
        .all()
    )
    return [
        {
            "class_slug": r.class_slug,
            "class_label": r.class_label,
            "height_group": r.height_group,
            "total": r.total or 0,
            "completed": r.completed or 0,
            "clean": r.clean or 0,
            "sub_sct": r.sub_sct or 0,
            "fastest": r.fastest,
            "avg_delta": float(r.avg_delta) if r.avg_delta is not None else None,
        }
        for r in rows
    ]


def _runs_query(db: DBSession, dog_ids: list[int], filters: dict):
    q = (
        db.query(TrialResult, Trial)
        .join(Trial, Trial.id == TrialResult.trial_id)
        .filter(TrialResult.dog_id.in_(dog_ids))
    )
    q = _apply_filters(q, filters)
    return q.order_by(Trial.start_date.desc().nullslast(), TrialResult.id.desc())


def _filter_options(db: DBSession, dog_ids: list[int]) -> dict:
    if not dog_ids:
        return {"classes": [], "heights": []}
    classes = (
        db.query(TrialResult.class_slug, TrialResult.class_label)
        .filter(TrialResult.dog_id.in_(dog_ids))
        .group_by(TrialResult.class_slug, TrialResult.class_label)
        .order_by(TrialResult.class_label)
        .all()
    )
    heights = (
        db.query(TrialResult.height_group)
        .filter(TrialResult.dog_id.in_(dog_ids))
        .group_by(TrialResult.height_group)
        .order_by(TrialResult.height_group)
        .all()
    )
    return {
        "classes": [{"slug": s, "label": l} for s, l in classes],
        "heights": [h for (h,) in heights],
    }


@router.get("/s/{uuid}/results", response_class=HTMLResponse)
def results_page(
    uuid: str,
    request: Request,
    page: int = 1,
    dog_id: int | None = None,
    class_slug: str | None = None,
    height_group: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    dog_ids = _session_dog_ids(session, db)

    # Mark visited so a future "new since last visit" highlight can use this.
    session.last_results_view_at = datetime.utcnow()
    db.commit()

    filters = {
        "dog_id": dog_id,
        "class_slug": class_slug,
        "height_group": height_group,
        "status": status,
        "date_from": _parse_date(date_from),
        "date_to": _parse_date(date_to),
    }

    stats = _per_dog_stats(db, dog_ids) if dog_ids else []
    options = _filter_options(db, dog_ids)

    runs: list[tuple] = []
    total_runs = 0
    if dog_ids:
        base = _runs_query(db, dog_ids, filters)
        total_runs = base.count()
        offset = max(0, (page - 1) * PAGE_SIZE)
        runs = base.offset(offset).limit(PAGE_SIZE).all()

    has_next = (page * PAGE_SIZE) < total_runs

    qs_parts = []
    qs_map = {
        "dog_id": dog_id,
        "class_slug": class_slug,
        "height_group": height_group,
        "status": status,
        "date_from": date_from,
        "date_to": date_to,
    }
    for k, v in qs_map.items():
        if v not in (None, ""):
            qs_parts.append(f"&{k}={v}")
    pagination_qs = "".join(qs_parts)

    ctx = {
        "session": session,
        "uuid": uuid,
        "dogs_known": bool(dog_ids),
        "stats": stats,
        "runs": runs,
        "page": page,
        "has_next": has_next,
        "total_runs": total_runs,
        "filters": qs_map,
        "options": options,
        "pagination_url": f"/s/{uuid}/results",
        "pagination_qs": pagination_qs,
    }

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "partials/results_rows.html", ctx)
    return templates.TemplateResponse(request, "results.html", ctx)


@router.get("/s/{uuid}/results/dog/{dog_id}", response_class=HTMLResponse)
def dog_results_page(
    uuid: str,
    dog_id: int,
    request: Request,
    page: int = 1,
    db: DBSession = Depends(get_db),
):
    session = _get_session(uuid, db)
    dog_ids = _session_dog_ids(session, db)
    if dog_id not in dog_ids:
        raise HTTPException(status_code=404, detail="Dog not in this session")

    dog = db.query(Dog).filter(Dog.id == dog_id).first()
    if not dog:
        raise HTTPException(status_code=404, detail="Dog not found")

    summary = _per_dog_stats(db, [dog_id])
    breakdown = _per_class_height_stats(db, dog_id)

    base = (
        db.query(TrialResult, Trial)
        .join(Trial, Trial.id == TrialResult.trial_id)
        .filter(TrialResult.dog_id == dog_id)
        .order_by(Trial.start_date.desc().nullslast(), TrialResult.id.desc())
    )
    total_runs = base.count()
    offset = max(0, (page - 1) * PAGE_SIZE)
    runs = base.offset(offset).limit(PAGE_SIZE).all()
    has_next = (page * PAGE_SIZE) < total_runs

    return templates.TemplateResponse(request, "dog_results.html", {
        "session": session,
        "uuid": uuid,
        "dog": dog,
        "summary": summary[0] if summary else None,
        "breakdown": breakdown,
        "runs": runs,
        "page": page,
        "has_next": has_next,
        "total_runs": total_runs,
        "pagination_url": f"/s/{uuid}/results/dog/{dog_id}",
        "pagination_qs": "",
    })


# --- Admin endpoints (token-gated) ----------------------------------------

def _check_admin(token: str | None) -> None:
    expected = os.getenv("RESULTS_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Admin token not configured")
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@router.post("/admin/results/backfill")
def admin_backfill(
    years: int = Form(3),
    token: str | None = Form(None),
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
):
    _check_admin(token or x_admin_token)
    try:
        from app.queue import get_queue
        job = get_queue().enqueue("app.worker.backfill_results_job", years, job_timeout=1800)
        log.info("Results backfill enqueued: years=%s job=%s", years, job.id)
    except Exception as e:
        log.error("Failed to enqueue results backfill: %s", e)
        raise HTTPException(status_code=500, detail=f"Enqueue failed: {e}")
    return JSONResponse({"job_id": job.id, "status": "enqueued", "years": years})


@router.get("/admin/results/status")
def admin_status(
    token: str | None = None,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
    db: DBSession = Depends(get_db),
):
    _check_admin(token or x_admin_token)

    counts = (
        db.query(Trial.results_status, func.count(Trial.id))
        .filter(Trial.discipline == 1)
        .group_by(Trial.results_status)
        .all()
    )
    last = (
        db.query(func.max(Trial.results_synced_at))
        .filter(Trial.discipline == 1)
        .scalar()
    )
    return JSONResponse({
        "counts_by_status": {(s or "unscraped"): n for s, n in counts},
        "last_synced_at": last.isoformat() if last else None,
        "total_trial_results": db.query(func.count(TrialResult.id)).scalar() or 0,
        "total_dogs": db.query(func.count(Dog.id)).scalar() or 0,
    })
