"""Transition logic for live ring board snapshots (worker-only)."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone

from app.engine.predictor import AEST_OFFSET
from app.models import EventDurationStat, EventLiveTiming

_PAUSE_STATUSES = frozenset({"Height Change", "Not Running"})
_SNAPSHOT_DATETIME_FIELDS = frozenset({"updated", "pause_started_at"})


def _to_naive_aest(dt: datetime) -> datetime:
    """Convert to naive AEST wall clock for schedule/predictor parity."""
    if dt.tzinfo is not None:
        utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        utc = dt
    return utc + AEST_OFFSET


def serialize_ring_snapshots(snapshots: dict[str, dict]) -> str:
    """JSON-safe ring snapshot state for Redis."""
    out: dict[str, dict] = {}
    for ring_id, snap in snapshots.items():
        serialized = dict(snap)
        for field in _SNAPSHOT_DATETIME_FIELDS:
            value = serialized.get(field)
            if isinstance(value, datetime):
                serialized[field] = value.isoformat()
        out[ring_id] = serialized
    return json.dumps(out)


def deserialize_ring_snapshots(raw: str) -> dict[str, dict]:
    """Restore ring snapshot state from Redis JSON."""
    data = json.loads(raw)
    out: dict[str, dict] = {}
    for ring_id, snap in data.items():
        deserialized = dict(snap)
        for field in _SNAPSHOT_DATETIME_FIELDS:
            value = deserialized.get(field)
            if isinstance(value, str):
                deserialized[field] = datetime.fromisoformat(value)
        out[ring_id] = deserialized
    return out


def segment_key(
    ring_number: str,
    event_name: str,
    height_group: int | None,
    day: int = 1,
) -> tuple:
    return (ring_number, event_name, height_group or 0, day)


def _transition_at(curr: dict, observed_at: datetime) -> datetime:
    return _to_naive_aest(curr.get("updated") or observed_at)


def _same_event(prev: dict | None, curr: dict) -> bool:
    if prev is None:
        return False
    return (
        prev.get("event_name") == curr.get("event_name")
        and prev.get("height_group") == curr.get("height_group")
    )


def _event_changed(prev: dict | None, curr: dict) -> bool:
    if prev is None:
        return False
    return not _same_event(prev, curr)


def _get_timing(
    db,
    trial_id: int,
    day: int,
    ring_number: str,
    event_name: str,
    height_group: int | None,
) -> EventLiveTiming | None:
    return (
        db.query(EventLiveTiming)
        .filter_by(
            trial_id=trial_id,
            day=day,
            ring_number=ring_number,
            event_name=event_name,
            height_group=height_group or 0,
        )
        .first()
    )


def _get_or_create_timing(
    db,
    trial_id: int,
    day: int,
    curr: dict,
) -> EventLiveTiming:
    height_group = curr.get("height_group") or 0
    row = _get_timing(
        db,
        trial_id,
        day,
        curr["ring_number"],
        curr["event_name"],
        height_group,
    )
    if row is None:
        row = EventLiveTiming(
            trial_id=trial_id,
            day=day,
            ring_id=curr["ring_id"],
            ring_number=curr["ring_number"],
            event_name=curr["event_name"],
            height_group=height_group,
            pause_s=0,
        )
        db.add(row)
    else:
        row.ring_id = curr["ring_id"]
    return row


def _accumulate_pause(row: EventLiveTiming, pause_started_at: datetime | None, at: datetime) -> None:
    if pause_started_at is None:
        return
    delta = int((at - pause_started_at).total_seconds())
    if delta > 0:
        row.pause_s = (row.pause_s or 0) + delta


def _update_duration_stat(
    db,
    trial_id: int,
    event_name: str,
    height_group: int,
    last_duration_s: int | None,
    at: datetime,
) -> None:
    if last_duration_s is None:
        return

    durations = [
        r.duration_s
        for r in db.query(EventLiveTiming)
        .filter_by(trial_id=trial_id, event_name=event_name, height_group=height_group)
        .all()
        if r.duration_s is not None
    ]
    if not durations:
        return

    stat = (
        db.query(EventDurationStat)
        .filter_by(trial_id=trial_id, event_name=event_name, height_group=height_group)
        .first()
    )
    if stat is None:
        stat = EventDurationStat(
            trial_id=trial_id,
            event_name=event_name,
            height_group=height_group,
        )
        db.add(stat)

    stat.sample_count = len(durations)
    stat.median_duration_s = int(statistics.median(durations))
    stat.last_duration_s = last_duration_s
    stat.updated_at = at


def _close_segment(
    db,
    trial_id: int,
    row: EventLiveTiming,
    at: datetime,
    pause_started_at: datetime | None,
) -> None:
    if row.finished_at is not None:
        return

    _accumulate_pause(row, pause_started_at, at)
    row.finished_at = at
    row.observed_at = at
    if row.started_at is not None:
        elapsed = int((at - row.started_at).total_seconds())
        row.duration_s = max(elapsed - (row.pause_s or 0), 0)
    else:
        row.duration_s = None

    _update_duration_stat(
        db,
        trial_id,
        row.event_name,
        row.height_group,
        row.duration_s,
        at,
    )


def _start_confidence(prev: dict | None, event_changed: bool) -> str:
    if prev is None:
        return "low"
    if event_changed:
        return "high"
    return "high"


def _apply_ring_snapshot(
    db,
    trial_id: int,
    day: int,
    prev: dict | None,
    curr: dict,
    observed_at: datetime,
) -> dict:
    at = _transition_at(curr, observed_at)
    pause_started_at = prev.get("pause_started_at") if prev else None
    event_changed = _event_changed(prev, curr)

    if event_changed and prev is not None:
        old_row = _get_timing(
            db,
            trial_id,
            day,
            prev["ring_number"],
            prev["event_name"],
            prev.get("height_group"),
        )
        if old_row is not None:
            prev_pause = pause_started_at if prev.get("status") in _PAUSE_STATUSES else None
            _close_segment(db, trial_id, old_row, at, prev_pause)
        pause_started_at = None

    row = _get_or_create_timing(db, trial_id, day, curr)

    if event_changed or prev is None:
        if curr["status"] == "Running" and row.started_at is None:
            row.started_at = at
            row.start_confidence = _start_confidence(prev, event_changed)
    elif (
        prev.get("status") in _PAUSE_STATUSES
        and curr["status"] == "Running"
        and _same_event(prev, curr)
    ):
        _accumulate_pause(row, pause_started_at, at)
        pause_started_at = None
        if row.started_at is None:
            row.started_at = at
            row.start_confidence = "high"

    if curr["status"] == "Complete" and row.finished_at is None:
        _close_segment(db, trial_id, row, at, pause_started_at)
        pause_started_at = None

    if curr["status"] in _PAUSE_STATUSES:
        if pause_started_at is None:
            pause_started_at = at
    elif curr["status"] != "Complete":
        pause_started_at = None

    row.status = curr["status"]
    row.observed_at = _to_naive_aest(observed_at)

    snapshot = dict(curr)
    if pause_started_at is not None:
        snapshot["pause_started_at"] = pause_started_at
    return snapshot


def apply_ring_snapshots(
    db,
    trial_id: int,
    day: int,
    prev_rings: dict[str, dict],
    curr_rings: list[dict],
    observed_at: datetime,
) -> dict[str, dict]:
    """Compare prev vs curr ring snapshots and upsert EventLiveTiming rows."""
    next_rings: dict[str, dict] = {}

    for curr in curr_rings:
        ring_id = curr["ring_id"]
        prev = prev_rings.get(ring_id)
        next_rings[ring_id] = _apply_ring_snapshot(
            db,
            trial_id,
            day,
            prev,
            curr,
            observed_at,
        )

    return next_rings
