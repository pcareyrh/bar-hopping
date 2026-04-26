"""RQ job functions executed inside the worker container."""
import asyncio
import logging
from datetime import datetime, timedelta, date

from sqlalchemy import insert, or_, and_

from app.database import SessionLocal
from app.models import (
    Session, Trial, SessionEntry, CatalogueEntry, ClassSchedule,
    Dog, TrialResult, normalise_name, normalise_handler, HANDLER_PLACEHOLDER,
)
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

            # Link this session's dogs into the dogs table and back-fill matches.
            queue.enqueue("app.worker.link_session_dogs_job", session_uuid, job_timeout=300)

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


# ---------------------------------------------------------------------------
# Past-results jobs
# ---------------------------------------------------------------------------


def backfill_results_job(years: int = 3) -> None:
    """One-shot: discover all NSW agility trials in the last N years and
    enqueue per-trial scrape jobs for any without ok results."""
    async def _run():
        from app.scraper.results import list_nsw_agility_trials, make_client, DISCIPLINE_AGILITY

        log.info("backfill_results_job: starting (years=%d)", years)
        set_sync_status("backfill", f"Discovering NSW agility trials (last {years}y)…")

        since = date.today() - timedelta(days=365 * years)
        async with make_client() as client:
            trials = await list_nsw_agility_trials(client, since=since)
        log.info("backfill_results_job: discovered %d trials", len(trials))

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            for t in trials:
                ext_id = t["external_id"]
                row = db.query(Trial).filter(Trial.external_id == ext_id).first()
                if row is None:
                    db.add(Trial(
                        external_id=ext_id,
                        name=t["name"],
                        start_date=t["start_date"],
                        state=t.get("state"),
                        discipline=DISCIPLINE_AGILITY,
                        scraped_at=now,
                    ))
                else:
                    if row.discipline is None:
                        row.discipline = DISCIPLINE_AGILITY
                    if not row.state and t.get("state"):
                        row.state = t["state"]
                    if not row.start_date and t.get("start_date"):
                        row.start_date = t["start_date"]
            db.commit()

            to_scrape = (
                db.query(Trial.id)
                .filter(
                    Trial.discipline == DISCIPLINE_AGILITY,
                    Trial.start_date >= since,
                    or_(Trial.results_status.is_(None), Trial.results_status != "ok"),
                )
                .all()
            )
            queue = get_queue()
            for (tid,) in to_scrape:
                queue.enqueue("app.worker.scrape_trial_results_job", tid, job_timeout=600)
            log.info("backfill_results_job: enqueued %d scrape jobs", len(to_scrape))
            set_sync_status("backfill", f"Enqueued {len(to_scrape)} trial scrapes.", 0, len(to_scrape))
        finally:
            db.close()

    asyncio.run(_run())


def scrape_trial_results_job(trial_id: int) -> None:
    """Scrape one trial's full results into trial_results.

    Idempotent: skipped if `results_status='ok'`. On success, fires off a
    match_results_to_dogs_job for this trial.
    """
    async def _run():
        from app.scraper.results import (
            make_client, fetch_event_subtrials, fetch_subtrial_results,
        )

        db = SessionLocal()
        try:
            trial = db.query(Trial).filter(Trial.id == trial_id).first()
            if trial is None:
                log.warning("scrape_trial_results_job: trial %s not found", trial_id)
                return
            if trial.results_status == "ok":
                log.info("scrape_trial_results_job: trial %s already ok, skipping", trial.external_id)
                return

            event_id = trial.external_id
            try:
                async with make_client() as client:
                    subtrials = await fetch_event_subtrials(client, event_id)
                    if not subtrials:
                        trial.results_status = "none"
                        trial.results_synced_at = datetime.utcnow()
                        db.commit()
                        return

                    sem = asyncio.Semaphore(4)

                    async def fetch_one(sub_id: str, label: str):
                        async with sem:
                            await asyncio.sleep(0.1)
                            return await fetch_subtrial_results(client, event_id, sub_id, label)

                    results_per_sub = await asyncio.gather(
                        *[fetch_one(sub_id, label) for sub_id, label in subtrials],
                        return_exceptions=True,
                    )
            except Exception as e:
                log.warning("scrape_trial_results_job: trial %s fetch failed: %s", event_id, e)
                trial.results_status = f"error:{str(e)[:80]}"
                trial.results_synced_at = datetime.utcnow()
                db.commit()
                return

            all_runs: list[dict] = []
            errors: list[str] = []
            for (sub_id, label), runs in zip(subtrials, results_per_sub):
                if isinstance(runs, Exception):
                    errors.append(f"{sub_id}:{type(runs).__name__}")
                    continue
                all_runs.extend(runs)

            if errors and not all_runs:
                trial.results_status = f"error:subtrial-fetch:{','.join(errors[:3])}"[:200]
                trial.results_synced_at = datetime.utcnow()
                db.commit()
                return

            db.query(TrialResult).filter(TrialResult.trial_id == trial.id).delete()
            db.flush()
            if all_runs:
                payload = []
                now = datetime.utcnow()
                for r in all_runs:
                    payload.append({
                        "trial_id": trial.id,
                        "sub_trial_external_id": r["sub_trial_external_id"],
                        "sub_trial_label": r.get("sub_trial_label"),
                        "class_slug": r["class_slug"],
                        "class_label": r["class_label"],
                        "height_group": r["height_group"],
                        "sct_seconds": r.get("sct_seconds"),
                        "course_length_m": r.get("course_length_m"),
                        "judge_name": r.get("judge_name"),
                        "dog_id": None,
                        "dog_name_raw": r["dog_name_raw"],
                        "handler_name_raw": r.get("handler_name_raw"),
                        "time_seconds": r.get("time_seconds"),
                        "total_faults": r.get("total_faults"),
                        "status": r.get("status"),
                        "nfc": bool(r.get("nfc")),
                        "row_index": r["row_index"],
                        "scraped_at": now,
                    })
                # Bulk insert avoids per-row overhead on the ~150-row average trial.
                db.execute(insert(TrialResult), payload)

            trial.results_status = "ok" if not errors else f"ok:partial:{len(errors)}"
            trial.results_synced_at = datetime.utcnow()
            db.commit()
            log.info("scrape_trial_results_job: trial %s ok (%d runs, %d errors)",
                     event_id, len(all_runs), len(errors))
        finally:
            db.close()

    asyncio.run(_run())

    # Match newly-inserted rows against known dogs (separate session — keep it tidy).
    try:
        get_queue().enqueue("app.worker.match_results_to_dogs_job", trial_id, job_timeout=120)
    except Exception as e:
        log.warning("scrape_trial_results_job: failed to enqueue match: %s", e)


def match_results_to_dogs_job(trial_id: int | None = None) -> None:
    """Back-link trial_results.dog_id by normalised (name, handler).

    If trial_id is given, scope to that trial. Otherwise scope to all rows
    where dog_id IS NULL. Never auto-creates dogs — only known user dogs match.
    """
    db = SessionLocal()
    try:
        q = db.query(TrialResult).filter(TrialResult.dog_id.is_(None))
        if trial_id is not None:
            q = q.filter(TrialResult.trial_id == trial_id)

        # Pull all known dogs once into a lookup.
        dogs = db.query(Dog).all()
        by_full: dict[tuple[str, str], int] = {}
        by_name: dict[str, list[int]] = {}
        for d in dogs:
            by_full[(d.name_normalised, d.handler_normalised)] = d.id
            by_name.setdefault(d.name_normalised, []).append(d.id)

        matched = 0
        for tr in q.yield_per(500):
            name_n = normalise_name(tr.dog_name_raw)
            if not name_n:
                continue
            handler_n = normalise_handler(tr.handler_name_raw)

            dog_id = by_full.get((name_n, handler_n))
            if dog_id is None:
                # Fall back to name-only match if exactly one Dog row matches.
                candidates = by_name.get(name_n, [])
                if len(candidates) == 1:
                    dog_id = candidates[0]

            if dog_id is not None:
                tr.dog_id = dog_id
                matched += 1

        db.commit()
        log.info("match_results_to_dogs_job: matched %d rows (trial_id=%s)", matched, trial_id)
    finally:
        db.close()


def link_session_dogs_job(session_uuid: str) -> None:
    """Upsert a Dog row per distinct dog_name in this session's entries,
    then back-link any historical results to those new dogs.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(SessionEntry.dog_name)
            .filter(SessionEntry.session_uuid == session_uuid)
            .filter(SessionEntry.dog_name.isnot(None))
            .distinct()
            .all()
        )
        added = 0
        for (raw_name,) in rows:
            name_n = normalise_name(raw_name)
            if not name_n:
                continue
            handler_n = normalise_handler(None)
            existing = (
                db.query(Dog.id)
                .filter(Dog.name_normalised == name_n)
                .filter(Dog.handler_normalised == handler_n)
                .first()
            )
            if existing:
                continue
            db.add(Dog(
                name=raw_name,
                name_normalised=name_n,
                handler_name=None,
                handler_normalised=handler_n,
            ))
            added += 1
        db.commit()
        log.info("link_session_dogs_job: %s — %d new dogs", session_uuid, added)
    finally:
        db.close()

    # Back-link unmatched results regardless of whether new dogs were added —
    # the user may have edited handler info that newly resolves an old row.
    try:
        get_queue().enqueue("app.worker.match_results_to_dogs_job", None, job_timeout=600)
    except Exception as e:
        log.warning("link_session_dogs_job: failed to enqueue match: %s", e)


def weekly_results_refresh_job(years: int = 3, grace_days: int = 60) -> None:
    """Append-only weekly refresh.

    - Discover any new NSW agility trials within the rolling N-year window.
    - Enqueue scrape for trials with no successful scrape yet, or trials
      with status='none'/error and start_date within the grace window.
    - Trials already at results_status='ok' are NEVER re-scraped.
    """
    async def _run():
        from app.scraper.results import list_nsw_agility_trials, make_client, DISCIPLINE_AGILITY

        since = date.today() - timedelta(days=365 * years)
        grace_cutoff = date.today() - timedelta(days=grace_days)

        async with make_client() as client:
            discovered = await list_nsw_agility_trials(client, since=since)
        log.info("weekly_results_refresh_job: discovered %d trials", len(discovered))

        db = SessionLocal()
        try:
            now = datetime.utcnow()
            for t in discovered:
                ext_id = t["external_id"]
                row = db.query(Trial).filter(Trial.external_id == ext_id).first()
                if row is None:
                    db.add(Trial(
                        external_id=ext_id,
                        name=t["name"],
                        start_date=t["start_date"],
                        state=t.get("state"),
                        discipline=DISCIPLINE_AGILITY,
                        scraped_at=now,
                    ))
                else:
                    if row.discipline is None:
                        row.discipline = DISCIPLINE_AGILITY
                    if not row.state and t.get("state"):
                        row.state = t["state"]
            db.commit()

            queue = get_queue()
            count = 0
            # 1) Trials never scraped — scrape if they're in the window.
            never = (
                db.query(Trial.id)
                .filter(
                    Trial.discipline == DISCIPLINE_AGILITY,
                    Trial.start_date >= since,
                    Trial.results_status.is_(None),
                )
                .all()
            )
            for (tid,) in never:
                queue.enqueue("app.worker.scrape_trial_results_job", tid, job_timeout=600)
                count += 1

            # 2) Previously 'none' or 'error:*' AND within the grace window.
            retry = (
                db.query(Trial.id)
                .filter(
                    Trial.discipline == DISCIPLINE_AGILITY,
                    Trial.start_date >= grace_cutoff,
                    Trial.results_status.isnot(None),
                    Trial.results_status != "ok",
                )
                .all()
            )
            for (tid,) in retry:
                queue.enqueue("app.worker.scrape_trial_results_job", tid, job_timeout=600)
                count += 1

            log.info("weekly_results_refresh_job: enqueued %d scrape jobs", count)
        finally:
            db.close()

    asyncio.run(_run())
