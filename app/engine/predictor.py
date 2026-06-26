"""Time prediction engine for dog agility runs."""
from datetime import datetime, date, time, timedelta
from typing import Optional

AEST_OFFSET = timedelta(hours=10)


def predict_run_from_block(
    *,
    block_first_run: datetime,
    run_position: int,
    avg_time_per_dog: int,
    position_override: Optional[int] = None,
    time_per_dog_override: Optional[int] = None,
) -> dict:
    """Predict a single dog's start given the datetime the first dog in its
    (class, height) block runs."""
    effective_position = position_override if position_override is not None else run_position
    effective_tpd = time_per_dog_override if time_per_dog_override is not None else avg_time_per_dog
    predicted_start = block_first_run + timedelta(seconds=(effective_position - 1) * effective_tpd)
    return {
        "first_run_start": block_first_run,
        "predicted_start": predicted_start,
        "effective_position": effective_position,
        "effective_tpd": effective_tpd,
    }


def predict_run_from_event(
    *,
    event_started_at: datetime,
    run_position: int,
    avg_time_per_dog: int,
    position_override: Optional[int] = None,
    time_per_dog_override: Optional[int] = None,
) -> dict:
    """Like predict_run_from_block but anchor is measured event start."""
    effective_position = position_override if position_override is not None else run_position
    effective_tpd = time_per_dog_override if time_per_dog_override is not None else avg_time_per_dog
    predicted_start = event_started_at + timedelta(seconds=(effective_position - 1) * effective_tpd)
    return {
        "first_run_start": event_started_at,
        "predicted_start": predicted_start,
        "effective_position": effective_position,
        "effective_tpd": effective_tpd,
    }


def predict_run(
    *,
    scheduled_start: time,
    ring_setup_mins: int,
    walk_mins: int,
    run_position: int,
    avg_time_per_dog: int,
    trial_date: Optional[date] = None,
    position_override: Optional[int] = None,
    time_per_dog_override: Optional[int] = None,
) -> dict:
    """
    Calculate predicted start time for a single run.

    Returns dict with keys:
        first_run_start (datetime), predicted_start (datetime),
        effective_position (int), effective_tpd (int)
    """
    effective_position = position_override if position_override is not None else run_position
    effective_tpd = time_per_dog_override if time_per_dog_override is not None else avg_time_per_dog

    base_date = trial_date or date.today()
    base_dt = datetime.combine(base_date, scheduled_start)

    first_run_start = base_dt + timedelta(minutes=ring_setup_mins + walk_mins)
    predicted_start = first_run_start + timedelta(seconds=(effective_position - 1) * effective_tpd)

    return {
        "first_run_start": first_run_start,
        "predicted_start": predicted_start,
        "effective_position": effective_position,
        "effective_tpd": effective_tpd,
    }


def format_predicted_time(dt: datetime) -> str:
    return dt.strftime("%-I:%M %p")


def _conflict_target(entry: dict) -> dict:
    return {
        "dog_name": entry.get("dog_name"),
        "event_name": entry.get("event_name"),
        "predicted_start_str": entry.get("predicted_start_str"),
        "is_friend": entry.get("is_friend", False),
        "handler_name": entry.get("handler_name"),
    }


def flag_conflicts(predictions: list[dict], buffer_mins: int = 5) -> list[dict]:
    """Mark entries whose predicted times overlap within buffer_mins of another entry.

    Entries sharing the same catalogue_entry_id (i.e. duplicates of the same
    dog in the same class) are not considered conflicts with each other.

    Sets ``conflict_with_yours`` when a clash is with one of the user's runs,
    and ``conflicts_with`` with details for each overlapping entry (yours first).
    """
    buffer = timedelta(minutes=buffer_mins)
    for i, a in enumerate(predictions):
        a["conflict"] = False
        a["conflict_with_yours"] = False
        a["conflicts_with"] = []
        if a["predicted_start"] is None:
            continue
        a_cat_id = a.get("catalogue_entry_id")
        for j, b in enumerate(predictions):
            if i == j or b["predicted_start"] is None:
                continue
            # Skip duplicate entries for the same catalogue entry.
            b_cat_id = b.get("catalogue_entry_id")
            if a_cat_id and b_cat_id and a_cat_id == b_cat_id:
                continue
            diff = abs(a["predicted_start"] - b["predicted_start"])
            if diff <= buffer:
                a["conflict"] = True
                a["conflicts_with"].append(_conflict_target(b))
                if not b.get("is_friend"):
                    a["conflict_with_yours"] = True
        a["conflicts_with"].sort(key=lambda c: c["is_friend"])
    return predictions
