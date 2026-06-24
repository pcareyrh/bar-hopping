"""Helpers for deciding whether a trial is still active on a given day."""
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from app.models import CatalogueEntry, ClassSchedule, Trial


def effective_end_date(start_date: date | None, end_date: date | None) -> date | None:
    """Return the last calendar day of a trial."""
    if end_date:
        return end_date
    return start_date


def trial_active_on(
    start_date: date | None,
    end_date: date | None,
    *,
    today: date | None = None,
) -> bool:
    """True when the trial has not finished before *today*."""
    today = today or date.today()
    if start_date is None:
        return True
    if end_date is None and start_date < today:
        # No end date but the trial has started — may be a multi-day event still
        # running. Keep it until catalogue/schedule data sets end_date.
        return True
    end = effective_end_date(start_date, end_date)
    return end >= today


def trial_model_active_on(trial: Trial, *, today: date | None = None) -> bool:
    return trial_active_on(trial.start_date, trial.end_date, today=today)


def trial_dict_active_on(trial: dict, *, today: date | None = None) -> bool:
    return trial_active_on(trial.get("start_date"), trial.get("end_date"), today=today)


def update_trial_end_date(trial: Trial, db: DBSession) -> None:
    """Infer end_date from catalogue/schedule day numbers when possible."""
    if not trial.start_date:
        return

    max_catalogue_day = db.query(func.max(CatalogueEntry.day)).filter(
        CatalogueEntry.trial_id == trial.id
    ).scalar()
    max_schedule_day = db.query(func.max(ClassSchedule.day)).filter(
        ClassSchedule.trial_id == trial.id
    ).scalar()
    max_day = max(
        (day for day in (max_catalogue_day, max_schedule_day) if day is not None),
        default=None,
    )

    if max_day and max_day > 1:
        inferred_end_date = trial.start_date + timedelta(days=max_day - 1)
        if trial.end_date is None or inferred_end_date > trial.end_date:
            trial.end_date = inferred_end_date
    elif trial.end_date is None:
        trial.end_date = trial.start_date
