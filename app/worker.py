"""RQ job functions executed inside the worker container."""
import asyncio
import logging
import os
import re as _re
from datetime import date, datetime, timedelta

from sqlalchemy import func

from app.database import SessionLocal
from app.models import Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule, TrialLunchBreak
from app import crypto
from app.queue import set_sync_status, get_queue, get_redis
from app.trial_dates import trial_model_active_on, update_trial_end_date

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker")
DOC_REFRESH_JOB_TIMEOUT = 900
LIVE_POLL_INTERVAL = 45
LIVE_POLL_JOB_TIMEOUT = 120
LIVE_SWEEP_STALE_MINUTES = 5


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
            synced_ext_ids = {ut["external_id"] for ut in user_trials}

            # TopDog drops in-progress trials from /entries once they start.
            # Keep session entries for active trials the user is already linked to.
            preserved_entries: list[dict] = []
            for se in db.query(SessionEntry).filter(SessionEntry.session_uuid == session_uuid).all():
                trial = db.query(Trial).filter(Trial.id == se.trial_id).first()
                if not trial or trial.external_id in synced_ext_ids:
                    continue
                update_trial_end_date(trial, db)
                if not trial_model_active_on(trial):
                    continue
                preserved_entries.append({
                    "trial_id": se.trial_id,
                    "dog_name": se.dog_name,
                    "height_group": se.height_group,
                    "event_name": se.event_name,
                    "cat_number": se.cat_number,
                    "position_override": se.position_override,
                    "time_per_dog_override": se.time_per_dog_override,
                })

            if not user_trials and not preserved_entries:
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
                        end_date=detail.get("end_date") or ut.get("end_date"),
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
                    if ut.get("end_date"):
                        trial.end_date = ut["end_date"]
                    if detail.get("end_date"):
                        trial.end_date = detail["end_date"]
                    trial.scraped_at = now
                update_trial_end_date(trial, db)
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
            for pe in preserved_entries:
                db.add(SessionEntry(
                    session_uuid=session_uuid,
                    trial_id=pe["trial_id"],
                    dog_name=pe["dog_name"],
                    height_group=pe["height_group"],
                    event_name=pe["event_name"],
                    cat_number=pe["cat_number"],
                    position_override=pe["position_override"],
                    time_per_dog_override=pe["time_per_dog_override"],
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
                update_trial_end_date(trial, db)
                _resolve_catalogue_links(trial, db)
            db.commit()

            queue = get_queue()
            for trial in user_trial_rows:
                if trial_model_active_on(trial):
                    log.info(
                        "sync_session_job: enqueuing live tracking for trial %s",
                        trial.external_id,
                    )
                    queue.enqueue(
                        "app.worker.start_live_tracking_job",
                        trial.id,
                        job_timeout=LIVE_POLL_JOB_TIMEOUT,
                    )
            for trial in user_trial_rows:
                has_cat_rows = db.query(CatalogueEntry.id).filter(
                    CatalogueEntry.trial_id == trial.id
                ).first()
                cat_url_is_entries = bool(
                    trial.catalogue_doc_url
                    and trial.catalogue_doc_url.rstrip("/").endswith("/entries")
                )
                # Re-scrape if we have no rows yet, or if the stored URL is the
                # /entries HTML fallback (an xlsx may have been published since).
                needs_cat = (not has_cat_rows) or cat_url_is_entries
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


def _merge_catalogue_entries(db, trial_id: int, entries: list[dict]) -> None:
    """Replace catalogue rows for the days present in *entries*; leave other days untouched."""
    if not entries:
        return
    days = {e.get("day", 1) for e in entries}
    ce_ids = [
        row[0]
        for row in db.query(CatalogueEntry.id).filter(
            CatalogueEntry.trial_id == trial_id,
            CatalogueEntry.day.in_(days),
        ).all()
    ]
    if ce_ids:
        db.query(SessionEntry).filter(
            SessionEntry.trial_id == trial_id,
            SessionEntry.catalogue_entry_id.in_(ce_ids),
        ).update({"catalogue_entry_id": None}, synchronize_session=False)
    db.query(CatalogueEntry).filter(
        CatalogueEntry.trial_id == trial_id,
        CatalogueEntry.day.in_(days),
    ).delete(synchronize_session=False)
    for e in entries:
        db.add(CatalogueEntry(trial_id=trial_id, **e))


def _merge_lunch_breaks(
    db, trial_id: int, breaks: list[dict], fill_missing_only: bool = False
) -> None:
    """Replace lunch-break rows for the days present in *breaks*; leave other days untouched."""
    if not breaks:
        return
    days = {b["day"] for b in breaks}
    if fill_missing_only:
        existing = {
            (r.day, r.ring)
            for r in db.query(TrialLunchBreak).filter(TrialLunchBreak.trial_id == trial_id).all()
        }
        breaks = [b for b in breaks if (b["day"], b["ring"]) not in existing]
        if not breaks:
            return
    else:
        db.query(TrialLunchBreak).filter(
            TrialLunchBreak.trial_id == trial_id,
            TrialLunchBreak.day.in_(days),
        ).delete(synchronize_session=False)
    for b in breaks:
        db.add(TrialLunchBreak(trial_id=trial_id, **b))


async def _extract_and_merge_lunch_breaks(
    db,
    trial_id: int,
    pdf_data: bytes,
    trial_external_id: str | None,
    fill_missing_only: bool = False,
) -> None:
    from app.scraper.openrouter_timetable import extract_lunch_breaks_from_pdf

    try:
        breaks = await extract_lunch_breaks_from_pdf(
            pdf_data, trial_external_id=trial_external_id
        )
    except Exception as e:
        log.warning(
            "lunch break extraction failed for trial %s: %s",
            trial_external_id or trial_id,
            e,
            exc_info=True,
        )
        return
    if not breaks:
        return
    _merge_lunch_breaks(db, trial_id, breaks, fill_missing_only=fill_missing_only)
    days = sorted({b["day"] for b in breaks})
    log.info(
        "openrouter_timetable: stored trial=%s breaks=%d days=%s",
        trial_external_id or trial_id,
        len(breaks),
        days,
    )


def _merge_class_schedules(db, trial_id: int, schedules: list[dict]) -> None:
    """Replace class-schedule rows for the days present in *schedules*; leave other days untouched."""
    if not schedules:
        return
    explicit_days = {s["day"] for s in schedules if s.get("day") is not None}
    if explicit_days:
        has_day_agnostic_rows = any(s.get("day") is None for s in schedules)
        db.query(ClassSchedule).filter(
            ClassSchedule.trial_id == trial_id,
            ClassSchedule.day.in_(explicit_days),
        ).delete(synchronize_session=False)
        if has_day_agnostic_rows:
            db.query(ClassSchedule).filter(
                ClassSchedule.trial_id == trial_id,
                ClassSchedule.day.is_(None),
            ).delete(synchronize_session=False)
        for s in schedules:
            db.add(ClassSchedule(trial_id=trial_id, **s))
        return
    db.query(ClassSchedule).filter(
        ClassSchedule.trial_id == trial_id,
    ).delete(synchronize_session=False)
    for s in schedules:
        db.add(ClassSchedule(trial_id=trial_id, **s))


def refresh_trial_docs_job(trial_id: int, session_uuid: str | None = None, friends_mode: bool = False) -> None:
    """Download and parse catalogue and schedule data for a trial.

    Preferred source: TopDog's authenticated /trials/{id}/my_day dashboard,
    which gives both catalogue order and ring schedule in one HTML doc that
    reflects post-scratch state. Falls back to the legacy public xlsx/PDF
    catalogue + auth'd schedule scrape when /my_day is unavailable.

    Auth resolves session creds first, then TOPDOG_USER/TOPDOG_PW env vars."""
    async def _run():
        from app.scraper.catalogue import (
            download_and_parse_catalogue,
            download_and_parse_catalogue_entries,
            download_catalogue_pdf,
            parse_catalogue_pdf_bytes,
        )
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

            async def refresh_trial_detail_metadata() -> None:
                # Pick up catalogue/schedule URLs that may have appeared since
                # the trial was first added. Multi-day trials often expose a
                # full catalogue after /my_day starts showing only the first day.
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

            await refresh_trial_detail_metadata()

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
                my_day_days = {e["day"] for e in my_day_payload["catalogue_entries"]}
                _merge_catalogue_entries(db, trial_id, my_day_payload["catalogue_entries"])
                _merge_class_schedules(db, trial_id, my_day_payload.get("class_schedules") or [])
                if my_day_payload.get("start_time"):
                    trial.start_time = my_day_payload["start_time"]
                trial.scraped_at = datetime.utcnow()
                db.commit()
                update_trial_end_date(trial, db)
                _resolve_catalogue_links(trial, db)

                # my_day may only cover the current/next day (e.g. Saturday
                # before the trial starts). Merge any additional days from the
                # full catalogue PDF without touching days my_day already updated.
                if trial.catalogue_doc_url and not trial.catalogue_doc_url.rstrip("/").endswith("/entries"):
                    try:
                        pdf_data = None
                        try:
                            pdf_data = await download_catalogue_pdf(trial.catalogue_doc_url)
                        except Exception as e:
                            log.warning(
                                "refresh_trial_docs_job: catalogue PDF download failed: %s", e
                            )
                        if pdf_data:
                            cat_entries = await parse_catalogue_pdf_bytes(
                                pdf_data,
                                filename=trial.catalogue_doc_url.rsplit("/", 1)[-1] or "catalogue.pdf",
                                trial_external_id=trial.external_id,
                                catalogue_url=trial.catalogue_doc_url,
                            )
                        else:
                            cat_entries = await download_and_parse_catalogue(
                                trial.catalogue_doc_url,
                                trial_external_id=trial.external_id,
                            )
                        extra_entries = [e for e in cat_entries if e["day"] not in my_day_days]
                        if extra_entries:
                            extra_days = sorted({e["day"] for e in extra_entries})
                            log.info(
                                "refresh_trial_docs_job: my_day covered days %s; "
                                "merging catalogue PDF days %s",
                                sorted(my_day_days),
                                extra_days,
                            )
                            _merge_catalogue_entries(db, trial_id, extra_entries)
                            db.commit()
                            update_trial_end_date(trial, db)
                            _resolve_catalogue_links(trial, db)
                        if pdf_data:
                            try:
                                await _extract_and_merge_lunch_breaks(
                                    db,
                                    trial_id,
                                    pdf_data,
                                    trial.external_id,
                                    fill_missing_only=True,
                                )
                                db.commit()
                            except Exception as e:
                                log.warning(
                                    "refresh_trial_docs_job: lunch break extraction failed: %s", e
                                )
                    except Exception as e:
                        log.warning("refresh_trial_docs_job: catalogue supplement failed: %s", e)

                return

            # ----- Legacy fallback path -----
            if trial.catalogue_doc_url:
                pdf_data = None
                try:
                    if trial.catalogue_doc_url.rstrip("/").endswith("/entries"):
                        if friends_mode:
                            log.info(
                                "refresh_trial_docs_job: friends_mode — skipping HTML entries summary for trial %s",
                                trial.external_id,
                            )
                            entries = []
                        else:
                            log.info("refresh_trial_docs_job: fetching HTML entries from %s", trial.catalogue_doc_url)
                            entries = await download_and_parse_catalogue_entries(trial.catalogue_doc_url)
                    else:
                        log.info("refresh_trial_docs_job: fetching catalogue from %s", trial.catalogue_doc_url)
                        try:
                            pdf_data = await download_catalogue_pdf(trial.catalogue_doc_url)
                        except Exception as e:
                            log.warning(
                                "refresh_trial_docs_job: catalogue PDF download failed: %s", e
                            )
                            pdf_data = None
                        if pdf_data:
                            entries = await parse_catalogue_pdf_bytes(
                                pdf_data,
                                filename=trial.catalogue_doc_url.rsplit("/", 1)[-1] or "catalogue.pdf",
                                trial_external_id=trial.external_id,
                                catalogue_url=trial.catalogue_doc_url,
                            )
                        else:
                            entries = await download_and_parse_catalogue(
                                trial.catalogue_doc_url,
                                trial_external_id=trial.external_id,
                            )
                    log.info("refresh_trial_docs_job: %d catalogue entries parsed", len(entries))
                except Exception as e:
                    log.warning("refresh_trial_docs_job: catalogue download/parse failed: %s", e)
                    entries = []
                if entries:
                    _merge_catalogue_entries(db, trial_id, entries)
                    db.commit()
                    update_trial_end_date(trial, db)
                    _resolve_catalogue_links(trial, db)
                if pdf_data:
                    try:
                        await _extract_and_merge_lunch_breaks(
                            db,
                            trial_id,
                            pdf_data,
                            trial.external_id,
                            fill_missing_only=True,
                        )
                        db.commit()
                    except Exception as e:
                        log.warning(
                            "refresh_trial_docs_job: lunch break extraction failed: %s", e
                        )

            if trial.schedule_doc_url:
                if cookies is None:
                    log.info("refresh_trial_docs_job: skipping schedule for trial %s — no auth", trial_id)
                else:
                    try:
                        classes = await download_and_parse_schedule(trial.schedule_doc_url, cookies=cookies)
                        _merge_class_schedules(db, trial_id, classes)
                        db.commit()
                        update_trial_end_date(trial, db)
                    except Exception as e:
                        log.warning("refresh_trial_docs_job: schedule download/parse failed: %s", e)

            trial.scraped_at = datetime.utcnow()
            db.commit()
        finally:
            db.close()

    asyncio.run(_run())


def collect_friend_data_job(trial_id: int, session_uuid: str | None = None) -> None:
    """my_day-first refresh for the Friends tab; never downgrades to HTML summary."""
    refresh_trial_docs_job(trial_id, session_uuid, friends_mode=True)


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
        update_trial_end_date(trial, db)
        db.commit()
        log.info("upload_catalogue_job: %d entries stored for trial %s", len(entries), trial.external_id)
        if "pdf" in content_type or data[:5] == b"%PDF-":
            try:
                asyncio.run(
                    _extract_and_merge_lunch_breaks(
                        db, trial_id, data, trial.external_id, fill_missing_only=False
                    )
                )
                db.commit()
            except Exception as e:
                log.warning(
                    "upload_catalogue_job: lunch break extraction failed for trial %s: %s",
                    trial.external_id,
                    e,
                    exc_info=True,
                )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _live_trial_day(trial: Trial, db=None) -> int:
    """Return catalogue day number: 1 by default, or today relative to start_date for multi-day trials."""
    day = 1
    if (
        trial.start_date
        and trial.end_date
        and trial.end_date > trial.start_date
    ):
        day = (date.today() - trial.start_date).days + 1
        if day < 1:
            day = 1
        max_day = (trial.end_date - trial.start_date).days + 1
        day = min(day, max_day)

    if db is not None:
        max_cat_day = (
            db.query(func.max(CatalogueEntry.day))
            .filter(CatalogueEntry.trial_id == trial.id)
            .scalar()
        )
        if max_cat_day:
            day = min(day, max_cat_day)

    return day


def _live_ring_snapshots_key(trial_id: int) -> str:
    return f"live_rings:{trial_id}"


def _load_live_ring_snapshots(trial_id: int) -> dict:
    from app.live_tracking import deserialize_ring_snapshots

    try:
        raw = get_redis().get(_live_ring_snapshots_key(trial_id))
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode()
        return deserialize_ring_snapshots(raw)
    except Exception as e:
        log.warning(
            "poll_live_trial_job: failed to load ring snapshots for trial %d: %s",
            trial_id,
            e,
        )
        return {}


def _store_live_ring_snapshots(trial_id: int, snapshots: dict) -> None:
    from app.live_tracking import serialize_ring_snapshots

    try:
        get_redis().set(_live_ring_snapshots_key(trial_id), serialize_ring_snapshots(snapshots))
    except Exception as e:
        log.warning(
            "poll_live_trial_job: failed to store ring snapshots for trial %d: %s",
            trial_id,
            e,
        )


def _derive_live_status(rings: list[dict]) -> str:
    if not rings:
        return "done"
    statuses = [r.get("status") for r in rings]
    if any(s in ("Running", "Height Change", "Not Running") for s in statuses):
        return "live"
    if all(s == "Complete" for s in statuses):
        return "done"
    return "idle"


def _enqueue_live_poll(trial_id: int, *, delay_seconds: int = 0) -> None:
    """Enqueue a live poll job, deduplicating via a stable job_id per trial."""
    from rq.exceptions import DuplicateJobError

    queue = get_queue()
    job_id = f"live_poll:{trial_id}"
    kwargs = {"job_timeout": LIVE_POLL_JOB_TIMEOUT, "job_id": job_id}
    try:
        if delay_seconds:
            queue.enqueue_in(
                timedelta(seconds=delay_seconds),
                "app.worker.poll_live_trial_job",
                trial_id,
                **kwargs,
            )
        else:
            queue.enqueue(
                "app.worker.poll_live_trial_job",
                trial_id,
                **kwargs,
            )
    except DuplicateJobError:
        log.debug("live poll job %s already queued for trial %d", job_id, trial_id)


def poll_live_trial_job(trial_id: int) -> None:
    """Fetch TopDog ring status and update EventLiveTiming rows for a trial."""
    async def _run():
        from app.scraper.live import fetch_ring_status
        from app.live_tracking import apply_ring_snapshots

        db = SessionLocal()
        try:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if not trial:
                log.warning("poll_live_trial_job: trial %d not found", trial_id)
                return

            log.info("poll_live_trial_job: polling trial %s (id=%d)", trial.external_id, trial_id)
            payload = await fetch_ring_status(trial.external_id)
            rings = payload.get("rings") or []
            observed_at = payload.get("observed_at") or datetime.utcnow()

            prev = _load_live_ring_snapshots(trial_id)
            day = _live_trial_day(trial, db)
            next_prev = apply_ring_snapshots(db, trial_id, day, prev, rings, observed_at)
            _store_live_ring_snapshots(trial_id, next_prev)

            trial.live_synced_at = datetime.utcnow()
            trial.live_status = _derive_live_status(rings)
            db.commit()

            log.info(
                "poll_live_trial_job: trial %s live_status=%s (%d rings)",
                trial.external_id,
                trial.live_status,
                len(rings),
            )

            if trial.live_status == "live":
                _enqueue_live_poll(trial_id, delay_seconds=LIVE_POLL_INTERVAL)
        finally:
            db.close()

    asyncio.run(_run())


def start_live_tracking_job(trial_id: int) -> None:
    """Kick off (or refresh) live ring polling for a trial."""
    log.info("start_live_tracking_job: enqueuing poll for trial %d", trial_id)
    _enqueue_live_poll(trial_id)


def sweep_live_trials_job() -> None:
    """Ensure active trials with session entries have a recent live poller."""
    db = SessionLocal()
    try:
        trial_ids = [row[0] for row in db.query(SessionEntry.trial_id).distinct().all()]
        stale_before = datetime.utcnow() - timedelta(minutes=LIVE_SWEEP_STALE_MINUTES)
        queue = get_queue()
        enqueued = 0
        for trial_id in trial_ids:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if not trial or not trial_model_active_on(trial):
                continue
            if trial.live_status == "done":
                continue
            if trial.live_synced_at is not None and trial.live_synced_at >= stale_before:
                continue
            log.info(
                "sweep_live_trials_job: enqueuing live tracking for trial %s (id=%d)",
                trial.external_id,
                trial.id,
            )
            queue.enqueue(
                "app.worker.start_live_tracking_job",
                trial.id,
                job_timeout=LIVE_POLL_JOB_TIMEOUT,
            )
            enqueued += 1
        log.info("sweep_live_trials_job: enqueued %d trial(s)", enqueued)
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


