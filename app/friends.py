"""Friends tab: lookup, persistence, and prediction for other handlers."""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session as DBSession

from app.models import (
    CatalogueEntry,
    Session,
    SessionFriend,
    Trial,
    normalize_handler_name,
    friend_pin_key,
)
from app.engine.predictor import format_predicted_time
from app.routers.schedule import (
    _strip_event_code,
    _ring_label,
    _match_class_schedule,
    _compute_catalogue_blocks,
    predict_catalogue_entry,
    build_catalogue_index,
    fan_out_catalogue_entries,
    DEFAULT_TRIAL_START,
)

_CAT_RE = re.compile(r"^\d{2,4}(NFC)?$", re.I)


def is_real_catalogue_entry(ce: CatalogueEntry) -> bool:
    """True when this row represents an individual competitor (not a summary sentinel)."""
    if ce.cat_number and ce.cat_number.startswith("~"):
        return False
    if ce.run_position == 0 and not ce.handler_name and not ce.dog_name:
        return False
    return bool(ce.handler_name or ce.dog_name)


def friend_data_state(trial: Trial, db: DBSession) -> dict:
    """Classify catalogue coverage for friend lookup."""
    rows = (
        db.query(CatalogueEntry)
        .filter(CatalogueEntry.trial_id == trial.id)
        .all()
    )
    if not rows:
        return {
            "state": "none",
            "days": {},
            "available_days": [],
            "pending_days": [],
            "scraped_at": trial.scraped_at,
        }

    day_real: dict[int, int] = defaultdict(int)
    day_summary: dict[int, int] = defaultdict(int)
    for ce in rows:
        day = getattr(ce, "day", 1) or 1
        if is_real_catalogue_entry(ce):
            day_real[day] += 1
        else:
            day_summary[day] += 1

    all_days = sorted(set(day_real) | set(day_summary))
    days_info = {}
    available_days = []
    pending_days = []
    for d in all_days:
        real = day_real.get(d, 0)
        summary = day_summary.get(d, 0)
        if real > 0:
            days_info[d] = "available"
            available_days.append(d)
        elif summary > 0:
            days_info[d] = "summary_only"
            pending_days.append(d)
        else:
            days_info[d] = "none"
            pending_days.append(d)

    if not available_days:
        state = "summary_only" if day_summary else "none"
    elif pending_days:
        state = "partial"
    else:
        state = "available"

    return {
        "state": state,
        "days": days_info,
        "available_days": available_days,
        "pending_days": pending_days,
        "scraped_at": trial.scraped_at,
    }


def _handler_matches(ce: CatalogueEntry, normalized_query: str) -> bool:
    if not ce.handler_name:
        return False
    return normalize_handler_name(ce.handler_name) == normalized_query


def search_handlers(trial_id: int, query: str, db: DBSession) -> list[dict]:
    """Distinct handlers matching a name query (for disambiguation)."""
    q = normalize_handler_name(query)
    if not q:
        return []

    by_handler: dict[str, dict] = {}
    for ce in db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).all():
        if not is_real_catalogue_entry(ce) or not ce.handler_name:
            continue
        norm = normalize_handler_name(ce.handler_name)
        if q not in norm and norm not in q:
            continue
        bucket = by_handler.setdefault(norm, {
            "handler_name": ce.handler_name,
            "normalized": norm,
            "dogs": set(),
            "cat_numbers": set(),
        })
        if ce.dog_name:
            bucket["dogs"].add(ce.dog_name)
        bucket["cat_numbers"].add(ce.cat_number)

    return [
        {
            "handler_name": v["handler_name"],
            "dog_names": sorted(v["dogs"]),
            "dog_count": len(v["dogs"]),
            "cat_numbers": sorted(v["cat_numbers"]),
        }
        for v in sorted(by_handler.values(), key=lambda x: x["handler_name"])
    ]


def resolve_cat_number(trial_id: int, cat_number: str, db: DBSession) -> CatalogueEntry | None:
    norm_cat = cat_number.upper().replace("nfc", "NFC")
    return (
        db.query(CatalogueEntry)
        .filter(
            CatalogueEntry.trial_id == trial_id,
            CatalogueEntry.cat_number == norm_cat,
        )
        .filter(CatalogueEntry.cat_number.notlike("~%"))
        .first()
    )


def catalogue_entries_for_friend(friend: SessionFriend, trial_id: int, db: DBSession) -> list[CatalogueEntry]:
    """All catalogue rows for a pinned friend (handler-level or cat# fallback)."""
    q = db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id)
    if friend.handler_name:
        norm = normalize_handler_name(friend.handler_name)
        rows = [ce for ce in q.all() if is_real_catalogue_entry(ce) and _handler_matches(ce, norm)]
        return rows
    if friend.cat_number:
        return [
            ce for ce in q.filter(CatalogueEntry.cat_number == friend.cat_number).all()
            if is_real_catalogue_entry(ce)
        ]
    return []


def add_friend(
    *,
    session_uuid: str,
    trial_id: int,
    query: str,
    db: DBSession,
    handler_name: str | None = None,
) -> tuple[SessionFriend | None, str | None]:
    """Add a friend by CAT# or handler name. Returns (friend, error_message)."""
    query = (query or "").strip()
    if handler_name:
        query = handler_name.strip()

    if not query:
        return None, "Enter a CAT# or handler name."

    data = friend_data_state(db.query(Trial).filter(Trial.id == trial_id).first(), db)
    if data["state"] in ("none", "summary_only"):
        return None, "Friend lookup needs the trial running order. Tap Find friends' runs to collect it."

    pin_handler: str | None = None
    pin_cat: str | None = None
    label: str | None = None

    if handler_name or not _CAT_RE.match(query):
        matches = search_handlers(trial_id, query if not handler_name else handler_name, db)
        if not matches:
            return None, f"No competitor matched '{query}'."
        if len(matches) > 1 and not handler_name:
            return None, "ambiguous"
        chosen = matches[0]
        pin_handler = chosen["handler_name"]
        label = pin_handler
        key = friend_pin_key(handler_name=pin_handler)
    else:
        ce = resolve_cat_number(trial_id, query, db)
        if not ce:
            return None, f"No competitor matched CAT# {query}."
        pin_cat = ce.cat_number
        pin_handler = ce.handler_name
        label = ce.handler_name or f"CAT# {ce.cat_number}"
        key = (
            friend_pin_key(handler_name=pin_handler)
            if pin_handler
            else friend_pin_key(cat_number=pin_cat)
        )

    existing = db.query(SessionFriend).filter(
        SessionFriend.session_uuid == session_uuid,
        SessionFriend.trial_id == trial_id,
        SessionFriend.pin_key == key,
    ).first()
    if existing:
        return existing, None

    friend = SessionFriend(
        session_uuid=session_uuid,
        trial_id=trial_id,
        handler_name=pin_handler,
        cat_number=pin_cat if not pin_handler else None,
        label=label,
        pin_key=key,
    )
    db.add(friend)
    db.commit()
    db.refresh(friend)
    return friend, None


def remove_friend(friend_id: int, session_uuid: str, trial_id: int, db: DBSession) -> bool:
    friend = db.query(SessionFriend).filter(
        SessionFriend.id == friend_id,
        SessionFriend.session_uuid == session_uuid,
        SessionFriend.trial_id == trial_id,
    ).first()
    if not friend:
        return False
    db.delete(friend)
    db.commit()
    return True


def build_friend_predictions(
    session: Session,
    trial: Trial,
    db: DBSession,
    *,
    day_blocks: list[dict] | None = None,
    lunch_breaks: dict | None = None,
) -> list[dict]:
    """Predicted runs for all pinned friends, grouped with metadata."""
    friends = (
        db.query(SessionFriend)
        .filter(SessionFriend.session_uuid == session.uuid, SessionFriend.trial_id == trial.id)
        .order_by(SessionFriend.created_at)
        .all()
    )
    if not friends:
        return []

    if day_blocks is None:
        if lunch_breaks is None:
            from app.models import TrialLunchBreak
            lb_records = db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial.id).all()
            lunch_breaks = {(r.day, r.ring): (r.lunch_break_at, r.lunch_break_mins) for r in lb_records}
        day_blocks = _compute_catalogue_blocks(
            trial, db,
            base_start=trial.start_time or DEFAULT_TRIAL_START,
            setup_mins=session.default_setup_mins,
            walk_mins=session.default_walk_mins,
            tpd_for_height=session.tpd_for,
            lunch_breaks=lunch_breaks,
        )

    block_starts = {
        (b["event_name"], b["height_group"], b.get("day", 1)): b["first_run"]
        for b in day_blocks
    }
    from app.models import ClassSchedule
    all_class_schedules = db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial.id).all()
    cat_by_key = build_catalogue_index(trial.id, db)

    groups: list[dict] = []

    for friend in friends:
        anchor_rows = catalogue_entries_for_friend(friend, trial.id, db)
        if not anchor_rows:
            groups.append({
                "friend": friend,
                "predictions": [],
                "pending": True,
            })
            continue

        seen_keys: set[tuple[str, str]] = set()
        friend_preds: list[dict] = []
        for anchor in anchor_rows:
            key = (_strip_event_code(anchor.event_name), anchor.cat_number)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            for ce in fan_out_catalogue_entries(anchor, cat_by_key):
                pred = predict_catalogue_entry(
                    ce=ce,
                    session=session,
                    trial=trial,
                    all_class_schedules=all_class_schedules,
                    block_starts=block_starts,
                    entry_id=friend.id,
                    dog_name=ce.dog_name,
                    ring_number=getattr(ce, "ring_number", None),
                    is_friend=True,
                    handler_name=ce.handler_name or friend.label,
                    friend_id=friend.id,
                )
                friend_preds.append(pred)

        friend_preds.sort(key=lambda p: (p["predicted_start"] is None, p["predicted_start"] or ""))
        groups.append({
            "friend": friend,
            "predictions": friend_preds,
            "pending": not friend_preds,
        })

    return groups


def format_scraped_at(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.strftime("%-d %b %H:%M")
