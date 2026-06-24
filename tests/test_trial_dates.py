from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, CatalogueEntry, ClassSchedule, Trial
from app.scraper.auth import _parse_dates, _parse_entries_page
from app.trial_dates import (
    trial_active_on,
    trial_dict_active_on,
    trial_model_active_on,
    update_trial_end_date,
)


def test_trial_active_on_future_single_day():
    today = date(2026, 6, 24)
    assert trial_active_on(date(2026, 6, 25), date(2026, 6, 25), today=today)


def test_trial_active_on_completed_single_day():
    today = date(2026, 6, 24)
    assert not trial_active_on(date(2026, 6, 23), date(2026, 6, 23), today=today)


def test_trial_active_on_multi_day_second_day():
    today = date(2026, 6, 24)
    assert trial_active_on(date(2026, 6, 23), date(2026, 6, 24), today=today)


def test_trial_active_on_started_without_end_date():
    today = date(2026, 6, 24)
    assert trial_active_on(date(2026, 6, 23), None, today=today)


def test_parse_entries_dates_single_and_range():
    start, end = _parse_dates("Saturday, 22 June 2026")
    assert start == date(2026, 6, 22)
    assert end == date(2026, 6, 22)

    start, end = _parse_dates("Saturday, 22 June 2026 - Sunday, 23 June 2026")
    assert start == date(2026, 6, 22)
    assert end == date(2026, 6, 23)


def test_parse_entries_page_includes_end_date():
    html = """
    <div class="tab-pane" id="t1307">
      <strong>Nationals</strong>
      <small class="text-muted">Saturday, 22 June 2026 - Sunday, 23 June 2026</small>
      <table><tbody>
        <tr><td>410</td><td>Fika</td><td>Masters Agility</td><td>400</td><td></td><td></td></tr>
      </tbody></table>
    </div>
    """
    trials = _parse_entries_page(html)
    assert len(trials) == 1
    assert trials[0]["start_date"] == date(2026, 6, 22)
    assert trials[0]["end_date"] == date(2026, 6, 23)


def test_trial_dict_active_on_uses_end_date():
    today = date(2026, 6, 24)
    active = {
        "start_date": date(2026, 6, 23),
        "end_date": date(2026, 6, 24),
    }
    finished = {
        "start_date": date(2026, 6, 22),
        "end_date": date(2026, 6, 23),
    }
    assert trial_dict_active_on(active, today=today)
    assert not trial_dict_active_on(finished, today=today)


def test_update_trial_end_date_from_catalogue_days():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    trial = Trial(
        external_id="1307",
        name="Multi Day",
        start_date=date(2026, 6, 23),
    )
    db.add(trial)
    db.flush()
    db.add_all([
        CatalogueEntry(
            trial_id=trial.id,
            day=1,
            event_name="Masters Agility",
            cat_number="410",
            height_group=400,
            run_position=1,
            height_group_total=1,
            nfc=False,
        ),
        CatalogueEntry(
            trial_id=trial.id,
            day=2,
            event_name="Masters Jumping",
            cat_number="411",
            height_group=400,
            run_position=1,
            height_group_total=1,
            nfc=False,
        ),
    ])
    db.commit()

    update_trial_end_date(trial, db)
    assert trial.end_date == date(2026, 6, 24)
    assert trial_model_active_on(trial, today=date(2026, 6, 24))

    db.close()
    engine.dispose()


def test_update_trial_end_date_from_schedule_days():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    trial = Trial(external_id="1308", name="Schedule Days", start_date=date(2026, 6, 23))
    db.add(trial)
    db.flush()
    db.add(ClassSchedule(
        trial_id=trial.id,
        day=3,
        ring_number="1",
        class_name="Novice Agility",
    ))
    db.commit()

    update_trial_end_date(trial, db)
    assert trial.end_date == date(2026, 6, 25)

    db.close()
    engine.dispose()


def test_update_trial_end_date_keeps_existing_later_end_date():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    trial = Trial(
        external_id="1309",
        name="Partial Days",
        start_date=date(2026, 6, 23),
        end_date=date(2026, 6, 25),
    )
    db.add(trial)
    db.flush()
    db.add(CatalogueEntry(
        trial_id=trial.id,
        day=2,
        event_name="Masters Agility",
        cat_number="410",
        height_group=400,
        run_position=1,
        height_group_total=1,
        nfc=False,
    ))
    db.commit()

    update_trial_end_date(trial, db)
    assert trial.end_date == date(2026, 6, 25)

    db.close()
    engine.dispose()
