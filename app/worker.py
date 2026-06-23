"""RQ job functions executed inside the worker container."""
import asyncio
import logging
import os
import re as _re
from datetime import datetime

from app.database import SessionLocal
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule
from app import crypto
from app.queue import set_sync_status, get_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")
DOC_REFRESH_JOB_TIMEOUT = 900


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
                seen_entries: set[tuple] = set()
                for e in ut["entries"]:
                    dedup_key = (
                        trial.id,
                        e.get("dog_name"),
                        e.get("event_name"),
                        e.get("cat_number"),
                        e.get("height_group"),
                    )
                    if dedup_key in seen_entries:
                        log.debug("Skipping duplicate entry: %s", dedup_key)
                        continue
                    seen_entries.add(dedup_key)
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
                has_cat_rows = db.query(CatalogueEntry.id).filter(
                    CatalogueEntry.trial_id == trial.id
                ).first()
                cat_url_is_entries = bool(
                    trial.catalogue_doc_url
                    and trial.catalogue_doc_url.rstrip("/").endswith("/entries")
                )
                # Re-scrape if we have no URL/no rows yet, or if the stored URL is
                # the /entries HTML fallback (an xlsx may have been published since).
                needs_cat = (trial.catalogue_doc_url and not has_cat_rows) or cat_url_is_entries
                needs_sched = trial.schedule_doc_url and not db.query(ClassSchedule.id).filter(
                    ClassSchedule.trial_id == trial.id
                ).first()
                if needs_cat or needs_sched:
                    log.info("sync_session_job: enqueuing doc refresh for trial %s", trial.external_id)
                    queue.enqueue(
                        "app.worker.refresh_trial_docs_job",
                        trial.id,
                        session_uuid,
                        job_timeout=DOC_REFRESH_JOB_TIMEOUT,
                    )

            log.info("sync_session_job: done for %s", session_uuid)
        finally:
            db.close()

    asyncio.run(_run())


async def _resolve_auth_cookies(db, session_uuid: str | None) -> dict[str, str] | None:
    """Get a logged-in TopDog cookie jar.

    Tries the SessionEntry's encrypted credentials first; falls back to
    the worker-wide TOPDOG_USER / TOPDOG_PW env vars (loaded from .env).
    Returns None if no working creds are available.
    """
    from app.scraper.auth import get_authed_cookies

    if session_uuid:
        session = db.query(Session).filter(Session.uuid == session_uuid).first()
        if session and session.topdog_email and session.topdog_password:
            try:
                email = crypto.decrypt(session.topdog_email)
                password = crypto.decrypt(session.topdog_password)
                return await get_authed_cookies(email, password)
            except Exception as e:
                log.warning("auth: session creds failed, falling back to env: %s", e)

    env_user = os.getenv("TOPDOG_USER")
    env_pw = os.getenv("TOPDOG_PW")
    if env_user and env_pw:
        try:
            return await get_authed_cookies(env_user, env_pw)
        except Exception as e:
            log.warning("auth: env creds failed: %s", e)

    return None


def refresh_trial_docs_job(trial_id: int, session_uuid: str | None = None) -> None:
    """Download and parse catalogue and schedule data for a trial.

    Preferred source: TopDog's authenticated /trials/{id}/my_day dashboard,
    which gives both catalogue order and ring schedule in one HTML doc that
    reflects post-scratch state. Falls back to the legacy public xlsx/PDF
    catalogue + auth'd schedule scrape when /my_day is unavailable.

    Auth resolves session creds first, then TOPDOG_USER/TOPDOG_PW env vars."""
    async def _run():
        from app.scraper.catalogue import download_and_parse_catalogue, download_and_parse_catalogue_entries
        from app.scraper.schedule import download_and_parse_schedule
        from app.scraper.trials import fetch_trial_detail
        from app.scraper.my_day import fetch_my_day, MyDayUnavailable, MyDayAuthRequired

        db = SessionLocal()
        try:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if not trial:
                return

            log.info("refresh_trial_docs_job: trial %s (id=%d) catalogue_doc_url=%s",
                     trial.external_id, trial_id, trial.catalogue_doc_url)

            cookies = await _resolve_auth_cookies(db, session_uuid)

            my_day_payload = None
            if cookies is not None:
                try:
                    log.info("refresh_trial_docs_job: fetching my_day for trial %s", trial.external_id)
                    my_day_payload = await fetch_my_day(trial.external_id, cookies)
                    log.info("refresh_trial_docs_job: my_day yielded %d entries, %d classes",
                             len(my_day_payload["catalogue_entries"]),
                             len(my_day_payload["class_schedules"]))
                except MyDayUnavailable as e:
                    log.info("refresh_trial_docs_job: my_day unavailable for %s (%s) — falling back to legacy",
                             trial.external_id, e)
                except MyDayAuthRequired as e:
                    log.warning("refresh_trial_docs_job: my_day auth failed for %s: %s", trial.external_id, e)
                except Exception as e:
                    log.warning("refresh_trial_docs_job: my_day fetch failed for %s: %s", trial.external_id, e)

            if my_day_payload and my_day_payload["catalogue_entries"]:
                # Replace catalogue + schedule in one shot from my_day.
                db.query(SessionEntry).filter(SessionEntry.trial_id == trial_id).update(
                    {"catalogue_entry_id": None}, synchronize_session=False
                )
                db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).delete()
                for e in my_day_payload["catalogue_entries"]:
                    db.add(CatalogueEntry(trial_id=trial_id, **e))
                db.query(ClassSchedule).filter(ClassSchedule.trial_id == trial_id).delete()
                for c in my_day_payload["class_schedules"]:
                    db.add(ClassSchedule(trial_id=trial_id, **c))
                if my_day_payload.get("start_time"):
                    trial.start_time = my_day_payload["start_time"]
                trial.scraped_at = datetime.utcnow()
                db.commit()
                _resolve_catalogue_links(trial, db)

                # my_day may only cover the current/next day (e.g. Saturday
                # before the trial starts). If the catalogue PDF has additional
                # days, supplement with those entries so multi-day trials show
                # the full schedule.
                if trial.catalogue_doc_url and not trial.catalogue_doc_url.rstrip("/").endswith("/entries"):
                    my_day_days = set(e["day"] for e in my_day_payload["catalogue_entries"])
                    try:
                        cat_entries = await download_and_parse_catalogue(
                            trial.catalogue_doc_url,
                            trial_external_id=trial.external_id,
                        )
                        cat_days = set(e["day"] for e in cat_entries)
                        missing_days = cat_days - my_day_days
                        if missing_days:
                            log.info("refresh_trial_docs_job: my_day covered days %s; "
                                     "catalogue PDF has extra days %s — supplementing",
                                     sorted(my_day_days), sorted(missing_days))
                            for e in cat_entries:
                                if e["day"] in missing_days:
                                    db.add(CatalogueEntry(trial_id=trial_id, **e))
                            db.commit()
                            _resolve_catalogue_links(trial, db)
                    except Exception as e:
                        log.warning("refresh_trial_docs_job: catalogue supplement failed: %s", e)

                return

            # ----- Legacy fallback path -----
            # Re-scrape the trial detail page to pick up catalogue/schedule URLs
            # that may have appeared since the trial was first added (e.g. entries
            # closed after initial sync, or format changed to HTML entries page).
            try:
                log.info("refresh_trial_docs_job: fetching trial detail for %s", trial.external_id)
                detail = await fetch_trial_detail(trial.external_id)
                log.info("refresh_trial_docs_job: detail catalogue_doc_url=%s schedule_doc_url=%s",
                         detail.get("catalogue_doc_url"), detail.get("schedule_doc_url"))
                new_cat = detail.get("catalogue_doc_url")
                if new_cat and new_cat != trial.catalogue_doc_url:
                    current_is_entries = bool(
                        trial.catalogue_doc_url
                        and trial.catalogue_doc_url.rstrip("/").endswith("/entries")
                    )
                    new_is_entries = new_cat.rstrip("/").endswith("/entries")
                    if not trial.catalogue_doc_url or (current_is_entries and not new_is_entries):
                        trial.catalogue_doc_url = new_cat
                        log.info("refresh_trial_docs_job: updated catalogue_doc_url to %s", trial.catalogue_doc_url)
                if detail.get("schedule_doc_url") and not trial.schedule_doc_url:
                    trial.schedule_doc_url = detail["schedule_doc_url"]
                if detail.get("start_time"):
                    trial.start_time = detail["start_time"]
                    log.info("refresh_trial_docs_job: updated start_time to %s", trial.start_time)
                db.commit()
            except Exception as e:
                log.warning("refresh_trial_docs_job: trial detail re-scrape failed: %s", e)

            if trial.catalogue_doc_url:
                try:
                    if trial.catalogue_doc_url.rstrip("/").endswith("/entries"):
                        log.info("refresh_trial_docs_job: fetching HTML entries from %s", trial.catalogue_doc_url)
                        entries = await download_and_parse_catalogue_entries(trial.catalogue_doc_url)
                    else:
                        log.info("refresh_trial_docs_job: fetching catalogue from %s", trial.catalogue_doc_url)
                        entries = await download_and_parse_catalogue(
                            trial.catalogue_doc_url,
                            trial_external_id=trial.external_id,
                        )
                    log.info("refresh_trial_docs_job: %d catalogue entries parsed", len(entries))
                except Exception as e:
                    log.warning("refresh_trial_docs_job: catalogue download/parse failed: %s", e)
                    entries = []
                if entries:
                    db.query(SessionEntry).filter(SessionEntry.trial_id == trial_id).update(
                        {"catalogue_entry_id": None}, synchronize_session=False
                    )
                    db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).delete()
                    for e in entries:
                        db.add(CatalogueEntry(trial_id=trial_id, **e))
                    db.commit()
                    _resolve_catalogue_links(trial, db)

            if trial.schedule_doc_url:
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


def upload_catalogue_job(trial_id: int, data: bytes, content_type: str) -> None:
    import io
    from app.scraper.catalogue import parse_catalogue_pdf_bytes_sync, parse_catalogue_xlsx

    db = SessionLocal()
    try:
        trial = db.query(Trial).filter(Trial.id == trial_id).first()
        if not trial:
            log.warning("upload_catalogue_job: trial %d not found", trial_id)
            return

        try:
            if "pdf" in content_type or data[:5] == b"%PDF-":
                entries = parse_catalogue_pdf_bytes_sync(
                    data,
                    filename=f"trial-{trial.external_id}.pdf",
                    trial_external_id=trial.external_id,
                )
            else:
                entries = parse_catalogue_xlsx(io.BytesIO(data))
        except Exception:
            log.warning("upload_catalogue_job: parse failed for trial %s", trial.external_id, exc_info=True)
            return

        if not entries:
            log.warning("upload_catalogue_job: 0 entries parsed for trial %s", trial.external_id)
            return

        # PDF finals/running-order pages can re-list the same entries; keep first occurrence.
        seen_keys: set[tuple] = set()
        deduped: list[dict] = []
        for e in entries:
            key = (e["event_name"], e["cat_number"], e.get("day", 1))
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(e)
        if len(deduped) < len(entries):
            log.info(
                "upload_catalogue_job: dropped %d duplicate entries for trial %s",
                len(entries) - len(deduped),
                trial.external_id,
            )
        entries = deduped

        db.query(SessionEntry).filter(SessionEntry.trial_id == trial_id).update(
            {"catalogue_entry_id": None}, synchronize_session=False
        )
        db.query(CatalogueEntry).filter(CatalogueEntry.trial_id == trial_id).delete()
        for e in entries:
            db.add(CatalogueEntry(trial_id=trial_id, **e))
        db.flush()
        _resolve_catalogue_links(trial, db)
        db.commit()
        log.info("upload_catalogue_job: %d entries stored for trial %s", len(entries), trial.external_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _resolve_catalogue_links(trial: Trial, db) -> None:
    # Strip a trailing " (CODE)" / " (CODE1)" — trial 1482's catalogue tags AM/PM
    # session codes onto the event_name to keep them distinct, but the user's
    # /entries page omits the suffix.
    def _norm(name: str | None) -> str:
        return _re.sub(r"\s*\([A-Z]{2,4}\d*\)\s*$", "", name or "").strip()

    session_entries = db.query(SessionEntry).filter(SessionEntry.trial_id == trial.id).all()
    for se in session_entries:
        # Primary: exact cat_number + event_name match (xlsx catalogue format).
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
                continue
            # cat_number is unique within a trial's catalogue; fall back to a
            # cat-only match and verify event names agree after stripping codes.
            ce = next(
                (c for c in db.query(CatalogueEntry).filter(
                    CatalogueEntry.trial_id == trial.id,
                    CatalogueEntry.cat_number == se.cat_number,
                ).order_by(CatalogueEntry.id).all()
                 if _norm(c.event_name) == _norm(se.event_name)),
                None,
            )
            if ce:
                se.catalogue_entry_id = ce.id
                continue

        # Fallback: match by event_name + height_group for HTML entries-format
        # catalogue (sentinel cat_number starts with '~'). Gives height_group_total
        # without individual run order.
        if se.event_name and se.height_group:
            ce = (
                db.query(CatalogueEntry)
                .filter(
                    CatalogueEntry.trial_id == trial.id,
                    CatalogueEntry.event_name == se.event_name,
                    CatalogueEntry.height_group == se.height_group,
                    CatalogueEntry.cat_number.like("~%"),
                )
                .first()
            )
            if ce:
                se.catalogue_entry_id = ce.id
    db.commit()


