"""Background sync task: authenticate TopDog and populate SessionEntry rows."""
from datetime import datetime
from sqlalchemy.orm import Session as DBSession

from app.models import Session, Trial, SessionEntry
from app import crypto


async def run_sync(session: Session, db: DBSession):
    from app.scraper.auth import sync_user_entries

    email = crypto.decrypt(session.topdog_email)
    password = crypto.decrypt(session.topdog_password)

    trials = db.query(Trial).all()
    trial_ids = [t.external_id for t in trials]

    entries = await sync_user_entries(email, password, trial_ids)

    # Remove old entries for this session
    db.query(SessionEntry).filter(SessionEntry.session_uuid == session.uuid).delete()

    for e in entries:
        ext_id = e.get("trial_external_id")
        trial = db.query(Trial).filter(Trial.external_id == ext_id).first()
        if not trial:
            continue

        se = SessionEntry(
            session_uuid=session.uuid,
            trial_id=trial.id,
            dog_name=e.get("dog_name"),
            height_group=e.get("height_group"),
            event_name=e.get("event_name"),
            cat_number=e.get("cat_number"),
            ring_number=e.get("ring_number"),
            position_override=None,
            time_per_dog_override=None,
        )
        db.add(se)

    session.topdog_synced_at = datetime.utcnow()
    db.commit()

    # Try to link catalogue entries
    from app.routers.trials import _resolve_catalogue_links
    for trial in trials:
        _resolve_catalogue_links(trial, db)
