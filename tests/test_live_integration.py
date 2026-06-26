"""Integration test for live predictions against topdogevents.com.au.

Requires TOPDOG_USER and TOPDOG_PW (shell env or .env via tests/conftest.py).
Skipped when credentials are absent.

Verifies the full live pipeline:
  authenticate → discover trial with live board → fetch ring status →
  apply ring snapshots → persist timings → produce event-anchored predictions.

Run:
    set -a && . ./.env && set +a && .venv/bin/python -m pytest tests/test_live_integration.py -v
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.live_tracking import apply_ring_snapshots
from app.models import (
    CatalogueEntry,
    EventLiveTiming,
    Session,
    SessionEntry,
    Trial,
)
from app.routers.schedule import (
    _build_predictions,
    _event_timings_for_trial,
    predict_catalogue_entry,
)
from app.scraper.auth import sync_user_entries
from app.scraper.live import fetch_ring_status
from app.worker import (
    _derive_live_status as worker_derive_live_status,
    _load_live_ring_snapshots,
    _store_live_ring_snapshots,
)

# Public trial known to expose a live ring board when no user trial is active.
_FALLBACK_LIVE_TRIAL_ID = os.getenv("LIVE_INTEGRATION_FALLBACK_TRIAL_ID", "1307")


@pytest.fixture(scope="module")
def topdog_credentials() -> tuple[str, str]:
    user = os.getenv("TOPDOG_USER")
    password = os.getenv("TOPDOG_PW")
    if not user or not password:
        pytest.skip("TOPDOG_USER and TOPDOG_PW must be set for live integration tests")
    return user, password


async def _discover_live_payload(user: str, password: str) -> tuple[str, dict]:
    """Authenticate, then return the first trial external_id with a live ring board."""
    trials = await sync_user_entries(user, password)
    assert isinstance(trials, list)

    candidates = [t["external_id"] for t in trials if t.get("external_id")]
    if _FALLBACK_LIVE_TRIAL_ID not in candidates:
        candidates.append(_FALLBACK_LIVE_TRIAL_ID)

    last_error: Exception | None = None
    for external_id in candidates:
        try:
            payload = await fetch_ring_status(external_id)
            rings = payload.get("rings") or []
            if rings:
                return external_id, payload
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    pytest.skip("no TopDog trial with live ring board data found")


@pytest.fixture(scope="module")
def live_trial_payload(topdog_credentials) -> tuple[str, dict]:
    user, password = topdog_credentials
    return asyncio.run(_discover_live_payload(user, password))


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_topdog_credentials_sync_entries(topdog_credentials):
  user, password = topdog_credentials

  async def run():
      trials = await sync_user_entries(user, password)
      assert isinstance(trials, list)

  asyncio.run(run())


def test_fetch_live_ring_status_structure(live_trial_payload):
    external_id, payload = live_trial_payload
    assert payload["trial_external_id"] == external_id
    rings = payload["rings"]
    assert len(rings) >= 1

    ring = rings[0]
    assert ring["ring_id"]
    assert ring["ring_number"]
    assert ring["event_name"]
    assert ring["height_group"] is not None
    assert ring["status"] in {
        "Running", "Complete", "Height Change", "Not Running",
    }
    assert isinstance(ring["updated"], datetime)


def test_live_predictions_end_to_end(live_trial_payload, db, monkeypatch):
    external_id, payload = live_trial_payload
    rings = payload["rings"]
    observed_at = payload["observed_at"]
    assert isinstance(observed_at, datetime)

    running = next((r for r in rings if r["status"] == "Running"), rings[0])

    session = Session(uuid="live-integration")
    trial = Trial(
        external_id=external_id,
        name=f"Live integration {external_id}",
        start_date=date.today(),
        start_time=time(9, 0),
        end_date=date.today(),
    )
    db.add_all([session, trial])
    db.flush()

    coded_event = f"{running['event_name']} (LIVE)"
    ce = CatalogueEntry(
        trial_id=trial.id,
        day=1,
        event_name=coded_event,
        height_group=running["height_group"],
        height_group_total=12,
        cat_number="999",
        dog_name="Integration Dog",
        handler_name="Integration Handler",
        run_position=3,
        ring_number=running["ring_number"],
    )
    entry = SessionEntry(
        session_uuid=session.uuid,
        trial_id=trial.id,
        dog_name="Integration Dog",
        event_name=coded_event,
        height_group=running["height_group"],
        cat_number="999",
        ring_number=running["ring_number"],
        catalogue_entry=ce,
    )
    db.add_all([ce, entry])
    db.commit()

    next_snapshots = apply_ring_snapshots(db, trial.id, 1, {}, rings, observed_at)
    db.commit()

    assert next_snapshots
    timing_rows = db.query(EventLiveTiming).filter(EventLiveTiming.trial_id == trial.id).all()
    assert len(timing_rows) >= 1

    matched = next(
        (
            row for row in timing_rows
            if row.event_name == running["event_name"]
            and row.height_group == running["height_group"]
            and row.ring_number == running["ring_number"]
        ),
        None,
    )
    assert matched is not None, "expected EventLiveTiming for the running ring segment"
    if running["status"] == "Running":
        assert matched.started_at is not None
        assert matched.finished_at is None

    assert worker_derive_live_status(rings) == "live"

    event_timings = _event_timings_for_trial(trial, db)
    pred = predict_catalogue_entry(
        ce=ce,
        session=session,
        trial=trial,
        all_class_schedules=[],
        block_starts={},
        entry_id=entry.id,
        event_timings=event_timings,
        live_enabled=True,
    )
    assert pred["prediction_source"] == "event_live"
    assert pred["predicted_start"] is not None
    assert pred["predicted_start_str"]

    predictions = _build_predictions(
        session,
        trial,
        db,
        event_timings=event_timings,
    )
    user_preds = [p for p in predictions if p["entry_id"] == entry.id]
    assert user_preds
    assert any(p["prediction_source"] == "event_live" for p in user_preds)

    stored: dict[str, str] = {}

    class FakeRedis:
        def get(self, key):
            return stored.get(key)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr("app.worker.get_redis", lambda: FakeRedis())
    _store_live_ring_snapshots(trial.id, next_snapshots)
    loaded = _load_live_ring_snapshots(trial.id)
    assert loaded
    ring_key = str(running["ring_id"])
    assert ring_key in loaded
    assert loaded[ring_key]["event_name"] == running["event_name"]
    if "pause_started_at" in loaded[ring_key]:
        assert isinstance(loaded[ring_key]["pause_started_at"], datetime)
