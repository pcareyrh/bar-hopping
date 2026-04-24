"""RQ job functions executed inside the worker container."""
import asyncio
from datetime import datetime

from app.database import SessionLocal
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule
from app import crypto
from app.queue import set_sync_status


def sync_session_job(session_uuid: str) -> None:
    """Authenticate with TopDog and populate SessionEntry rows for a session."""
    async def _run():
        from app.scraper.auth import sync_user_entries

        set_sync_status(session_uuid, "Connecting to TopDog…")

        db = SessionLocal()
        try:
            session = db.query(Session).filter(Session.uuid == session_uuid).first()
            if not session:
                raise ValueError(f"Session {session_uuid} not found")

            email = crypto.decrypt(session.topdog_email)
            password = crypto.decrypt(session.topdog_password)
            trials = db.query(Trial).all()
            trial_ids = [t.external_id for t in trials]

            def on_trial(current: int, total: int):
                set_sync_status(session_uuid, "Fetching your entries from TopDog…", current, total)

            entries = await sync_user_entries(email, password, trial_ids, on_progress=on_trial)

            set_sync_status(session_uuid, "Saving entries…")
            db.query(SessionEntry).filter(SessionEntry.session_uuid == session_uuid).delete()
            for e in entries:
                ext_id = e.get("trial_external_id")
                trial = db.query(Trial).filter(Trial.external_id == ext_id).first()
                if not trial:
                    continue
                db.add(SessionEntry(
                    session_uuid=session_uuid,
                    trial_id=trial.id,
                    dog_name=e.get("dog_name"),
                    height_group=e.get("height_group"),
                    event_name=e.get("event_name"),
                    cat_number=e.get("cat_number"),
                    ring_number=e.get("ring_number"),
                    position_override=None,
                    time_per_dog_override=None,
                ))

            session.topdog_synced_at = datetime.utcnow()
            db.commit()

            all_trials = db.query(Trial).all()
            for i, trial in enumerate(all_trials, start=1):
                set_sync_status(session_uuid, "Linking catalogue entries…", i, len(all_trials))
                _resolve_catalogue_links(trial, db)
        finally:
            db.close()

    asyncio.run(_run())


def refresh_trials_job() -> None:
    """Scrape the TopDog trials list and upsert Trial rows."""
    async def _run():
        from app.scraper.trials import scrape_trials_list, scrape_trial_detail

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            trial_stubs = await scrape_trials_list()
            for stub in trial_stubs:
                existing = db.query(Trial).filter(Trial.external_id == stub["external_id"]).first()
                if not existing:
                    detail = await scrape_trial_detail(stub["external_id"])
                    db.add(Trial(
                        external_id=stub["external_id"],
                        name=detail.get("name", stub["name"]),
                        start_date=detail.get("start_date"),
                        venue=detail.get("venue"),
                        state="NSW",
                        schedule_doc_url=detail.get("schedule_doc_url"),
                        catalogue_doc_url=detail.get("catalogue_doc_url"),
                        scraped_at=now,
                    ))
                else:
                    existing.scraped_at = now
            db.commit()
        finally:
            db.close()

    asyncio.run(_run())


def refresh_trial_docs_job(trial_id: int) -> None:
    """Download and parse catalogue and schedule documents for a trial."""
    async def _run():
        from app.scraper.catalogue import download_and_parse_catalogue
        from app.scraper.schedule import download_and_parse_schedule

        db = SessionLocal()
        try:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if not trial:
                return

            if trial.catalogue_doc_url:
                entries = await download_and_parse_catalogue(trial.catalogue_doc_url)
                db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).delete()
                for e in entries:
                    db.add(CatalogueEntry(trial_id=trial_id, **e))
                db.commit()
                _resolve_catalogue_links(trial, db)

            if trial.schedule_doc_url:
                classes = await download_and_parse_schedule(trial.schedule_doc_url)
                db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial_id).delete()
                for c in classes:
                    db.add(ClassSchedule(trial_id=trial_id, **c))
                db.commit()

            trial.scraped_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()

    asyncio.run(_run())


def _resolve_catalogue_links(trial: Trial, db) -> None:
    session_entries = db.query(SessionEntry).filter(SessionEntry.trial_id == trial.id).all()
    for se in session_entries:
        if se.cat_number:
            ce = (
                db.query(CatalogueEntry)
                .filter(
                    CatalogueEntry.trial_id == trial.id,
                    CatalogueEntry.cat_number == se.cat_number,
                    CatalogueEntry.event_name == se.event_name,
                )
                .first()
            )
            if ce:
                se.catalogue_entry_id = ce.id
    db.commit()
