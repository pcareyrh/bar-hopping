"""Regression tests for multi-day / multi-round prediction fan-out.

A TopDog /entries page lists one row per run a dog has of a class, but those
rows are deduped into a single SessionEntry (the page carries no day/round
info to keep them apart). The prediction layer must re-expand that single
entry into one card per run by grouping catalogue rows on the *code-stripped*
event name — Nationals catalogues split one logical class into separately
coded runs, e.g. "Masters Agility (ADM1/ADM2)" across days and
"Open Agility (ADO1/2/3)" as three rounds on a single day.
"""
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Session, Trial, CatalogueEntry, SessionEntry
from app.routers.schedule import _build_predictions, _strip_event_code


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _cat(trial, **kw):
    base = dict(trial_id=trial.id, height_group=400, height_group_total=1, nfc=False)
    base.update(kw)
    return CatalogueEntry(**base)


def test_strip_event_code():
    assert _strip_event_code("Masters Agility (ADM1)") == "Masters Agility"
    assert _strip_event_code("Open Agility (ADO3)") == "Open Agility"
    assert _strip_event_code("Agility (AM)") == "Agility"
    assert _strip_event_code("Novice Snooker") == "Novice Snooker"
    assert _strip_event_code(None) == ""


def test_entry_fans_out_across_days_and_rounds(db):
    sess = Session(uuid="u1")
    trial = Trial(external_id="1307", name="Nationals", start_date=date(2026, 6, 23))
    db.add_all([sess, trial])
    db.flush()

    # cat 410 = Fika. Masters Agility split ADM1(day2)/ADM2(day3,4);
    # Open Agility = three rounds ADO1/2/3 all on day 5.
    ma1 = _cat(trial, day=2, event_name="Masters Agility (ADM1)", cat_number="410", run_position=1)
    ma2 = _cat(trial, day=3, event_name="Masters Agility (ADM2)", cat_number="410", run_position=1)
    ma3 = _cat(trial, day=4, event_name="Masters Agility (ADM2)", cat_number="410", run_position=1)
    oa1 = _cat(trial, day=5, event_name="Open Agility (ADO1)", cat_number="410", run_position=1)
    oa2 = _cat(trial, day=5, event_name="Open Agility (ADO2)", cat_number="410", run_position=2)
    oa3 = _cat(trial, day=5, event_name="Open Agility (ADO3)", cat_number="410", run_position=3)
    # A different dog sharing the class must NOT be pulled into 410's fan-out.
    other = _cat(trial, day=2, event_name="Masters Agility (ADM1)", cat_number="411", run_position=2)
    db.add_all([ma1, ma2, ma3, oa1, oa2, oa3, other])
    db.flush()

    # Deduped SessionEntries: bare class name, linked to the earliest run.
    db.add_all([
        SessionEntry(session_uuid=sess.uuid, trial_id=trial.id, dog_name="Fika",
                     height_group=400, event_name="Masters Agility", cat_number="410",
                     catalogue_entry_id=ma1.id),
        SessionEntry(session_uuid=sess.uuid, trial_id=trial.id, dog_name="Fika",
                     height_group=400, event_name="Open Agility", cat_number="410",
                     catalogue_entry_id=oa1.id),
    ])
    db.flush()

    preds = _build_predictions(sess, trial, db, day_blocks=[])

    ma = [p for p in preds if p["event_name"].startswith("Masters Agility")]
    oa = [p for p in preds if p["event_name"].startswith("Open Agility")]

    assert sorted(p["day"] for p in ma) == [2, 3, 4]
    assert sorted(p["day"] for p in oa) == [5, 5, 5]
    # Three distinct cards per class (unique catalogue_entry_id => unique card_id).
    assert len({p["card_id"] for p in ma}) == 3
    assert len({p["card_id"] for p in oa}) == 3
    # cat 411 (other dog) never appears.
    assert all(p["cat_number"] == "410" for p in preds)
    assert len(preds) == 6
