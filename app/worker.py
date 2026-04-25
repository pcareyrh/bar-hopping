"""RQ job functions executed inside the worker container."""
import asyncio
import logging
from datetime import datetime

from app.database import SessionLocal
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule
from app import crypto
from app.queue import set_sync_status, get_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")


def sync_session_job(session_uuid: str) -> None:
    """Authenticate with TopDog, discover the user's upcoming trials from
    /entries, upsert Trial rows (fetching metadata for any new ones), and
    populate SessionEntry rows."""
    async def _run():
        from app.scraper.auth import sync_user_entries
        from app.scraper.trials import scrape_trial_details_batch

        log.info("sync_session_job: starting for %s", session_uuid)
        set_sync_status(session_uuid, "Connecting to TopDog…")

        db = SessionLocal()
        try:
            session = db.query(Session).filter(Session.uuid == session_uuid).first()
            if not session:
                raise ValueError(f"Session {session_uuid} not found")

            email = crypto.decrypt(session.topdog_email)
            password = crypto.decrypt(session.topdog_password)

            set_sync_status(session_uuid, "Fetching your upcoming trials…")
            user_trials = await sync_user_entries(email, password)
            log.info("sync_session_job: /entries returned %d trials", len(user_trials))

            if not user_trials:
                set_sync_status(session_uuid, "No upcoming trials found.")
                session.topdog_synced_at = datetime.utcnow()
                db.commit()
                return

            # Fetch metadata for trials we haven't seen before
            existing_ids = {
                t.external_id for t in db.query(Trial.external_id).filter(
                    Trial.external_id.in_([ut["external_id"] for ut in user_trials])
                ).all()
            }
            new_ids = [ut["external_id"] for ut in user_trials if ut["external_id"] not in existing_ids]

            details_by_id: dict[str, dict] = {}
            if new_ids:
                def on_detail(i, total):
                    log.info("sync_session_job: trial detail %d/%d", i, total)
                    set_sync_status(session_uuid, "Fetching trial details…", i, total)

                details = await scrape_trial_details_batch(new_ids, on_progress=on_detail)
                details_by_id = {d["external_id"]: d for d in details}

            now = datetime.utcnow()
            for ut in user_trials:
                ext_id = ut["external_id"]
                trial = db.query(Trial).filter(Trial.external_id == ext_id).first()
                detail = details_by_id.get(ext_id, {})
                if trial is None:
                    trial = Trial(
                        external_id=ext_id,
                        name=detail.get("name") or ut["name"],
                        start_date=detail.get("start_date") or ut.get("start_date"),
                        venue=detail.get("venue"),
                        schedule_doc_url=detail.get("schedule_doc_url"),
                        catalogue_doc_url=detail.get("catalogue_doc_url"),
                        scraped_at=now,
                    )
                    db.add(trial)
                    db.flush()
                else:
                    # Keep name/date fresh from the /entries pane
                    trial.name = ut["name"] or trial.name
                    if ut.get("start_date"):
                        trial.start_date = ut["start_date"]
                    trial.scraped_at = now
            db.commit()

            set_sync_status(session_uuid, "Saving entries…")
            db.query(SessionEntry).filter(SessionEntry.session_uuid == session_uuid).delete()
            for ut in user_trials:
                trial = db.query(Trial).filter(Trial.external_id == ut["external_id"]).first()
                if not trial:
                    continue
                for e in ut["entries"]:
                    db.add(SessionEntry(
                        session_uuid=session_uuid,
                        trial_id=trial.id,
                        dog_name=e.get("dog_name"),
                        height_group=e.get("height_group"),
                        event_name=e.get("event_name"),
                        cat_number=e.get("cat_number"),
                        position_override=None,
                        time_per_dog_override=None,
                    ))
            session.topdog_synced_at = datetime.utcnow()
            db.commit()

            user_trial_rows = (
                db.query(Trial)
                .join(SessionEntry, SessionEntry.trial_id == Trial.id)
                .filter(SessionEntry.session_uuid == session_uuid)
                .distinct()
                .all()
            )
            for i, trial in enumerate(user_trial_rows, start=1):
                set_sync_status(session_uuid, "Linking catalogue entries…", i, len(user_trial_rows))
                _resolve_catalogue_links(trial, db)

            queue = get_queue()
            for trial in user_trial_rows:
                needs_cat = trial.catalogue_doc_url and not db.query(CatalogueEntry.id).filter(
                    CatalogueEntry.trial_id == trial.id
                ).first()
                needs_sched = trial.schedule_doc_url and not db.query(ClassSchedule.id).filter(
                    ClassSchedule.trial_id == trial.id
                ).first()
                if needs_cat or needs_sched:
                    log.info("sync_session_job: enqueuing doc refresh for trial %s", trial.external_id)
                    queue.enqueue("app.worker.refresh_trial_docs_job", trial.id, session_uuid, job_timeout=300)

            log.info("sync_session_job: done for %s", session_uuid)
        finally:
            db.close()

    asyncio.run(_run())


def refresh_trial_docs_job(trial_id: int, session_uuid: str | None = None) -> None:
    """Download and parse catalogue and schedule documents for a trial.

    Catalogue is public. Schedule requires authentication, so a session_uuid
    must be supplied to pull decrypted TopDog credentials and obtain a
    logged-in cookie jar."""
    async def _run():
        from app.scraper.catalogue import download_and_parse_catalogue
        from app.scraper.schedule import download_and_parse_schedule
        from app.scraper.auth import get_authed_cookies

        db = SessionLocal()
        try:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if not trial:
                return

            if trial.catalogue_doc_url:
                entries = await download_and_parse_catalogue(trial.catalogue_doc_url)
                # Unlink session entries so we can replace catalogue rows without FK violations.
                db.query(SessionEntry).filter(SessionEntry.trial_id == trial_id).update(
                    {"catalogue_entry_id": None}, synchronize_session=False
                )
                db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).delete()
                for e in entries:
                    db.add(CatalogueEntry(trial_id=trial_id, **e))
                db.commit()
                _resolve_catalogue_links(trial, db)

            if trial.schedule_doc_url:
                cookies: dict[str, str] | None = None
                if session_uuid:
                    session = db.query(Session).filter(Session.uuid == session_uuid).first()
                    if session:
                        try:
                            email = crypto.decrypt(session.topdog_email)
                            password = crypto.decrypt(session.topdog_password)
                            cookies = await get_authed_cookies(email, password)
                        except Exception as e:
                            log.warning("refresh_trial_docs_job: schedule auth failed: %s", e)

                if cookies is None:
                    log.info("refresh_trial_docs_job: skipping schedule for trial %s — no auth", trial_id)
                else:
                    try:
                        classes = await download_and_parse_schedule(trial.schedule_doc_url, cookies=cookies)
                        db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial_id).delete()
                        for c in classes:
                            db.add(ClassSchedule(trial_id=trial_id, **c))
                        db.commit()
                    except Exception as e:
                        log.warning("refresh_trial_docs_job: schedule download/parse failed: %s", e)

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
