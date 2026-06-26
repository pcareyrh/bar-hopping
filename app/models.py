import re
import uuid
from datetime import datetime, date, time
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Date, Time, Float,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


def _new_uuid():
    return str(uuid.uuid4())


HEIGHT_GROUPS = (200, 300, 400, 500, 600)


class Session(Base):
    __tablename__ = "sessions"

    uuid = Column(String, primary_key=True, default=_new_uuid)
    created_at = Column(DateTime, default=datetime.utcnow)
    topdog_email = Column(String, nullable=True)       # Fernet-encrypted
    topdog_password = Column(String, nullable=True)    # Fernet-encrypted
    topdog_synced_at = Column(DateTime, nullable=True)
    avg_time_per_dog = Column(Integer, default=90)     # legacy fallback
    tpd_200 = Column(Integer, default=90)
    tpd_300 = Column(Integer, default=90)
    tpd_400 = Column(Integer, default=90)
    tpd_500 = Column(Integer, default=90)
    tpd_600 = Column(Integer, default=90)
    tpd_jumping_200 = Column(Integer, default=90)
    tpd_jumping_300 = Column(Integer, default=90)
    tpd_jumping_400 = Column(Integer, default=90)
    tpd_jumping_500 = Column(Integer, default=90)
    tpd_jumping_600 = Column(Integer, default=90)
    default_setup_mins = Column(Integer, default=10)
    default_walk_mins = Column(Integer, default=10)
    entries = relationship("SessionEntry", back_populates="session", cascade="all, delete-orphan")
    friends = relationship("SessionFriend", back_populates="session", cascade="all, delete-orphan")

    def tpd_for(self, height_group: int | None, event_name: str | None = None) -> int:
        is_jumping = "jumping" in (event_name or "").lower()
        if height_group in HEIGHT_GROUPS:
            col = f"tpd_jumping_{height_group}" if is_jumping else f"tpd_{height_group}"
            value = getattr(self, col, None)
            if value is not None:
                return value
        return self.avg_time_per_dog or 90


class Trial(Base):
    __tablename__ = "trials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    start_date = Column(Date, nullable=True)
    start_time = Column(Time, nullable=True)
    end_date = Column(Date, nullable=True)
    venue = Column(String, nullable=True)
    state = Column(String, nullable=True)
    discipline = Column(Integer, nullable=True)
    schedule_doc_url = Column(String, nullable=True)
    catalogue_doc_url = Column(String, nullable=True)
    scraped_at = Column(DateTime, nullable=True)
    lunch_break_at = Column(Time, nullable=True)
    lunch_break_mins = Column(Integer, nullable=True)
    live_status = Column(String, nullable=True)  # idle|live|done
    live_synced_at = Column(DateTime, nullable=True)

    catalogue_entries = relationship("CatalogueEntry", back_populates="trial", cascade="all, delete-orphan")
    class_schedules = relationship("ClassSchedule", back_populates="trial", cascade="all, delete-orphan")
    session_entries = relationship("SessionEntry", back_populates="trial")
    session_friends = relationship("SessionFriend", back_populates="trial", cascade="all, delete-orphan")
    lunch_breaks = relationship("TrialLunchBreak", back_populates="trial", cascade="all, delete-orphan")
    event_live_timings = relationship("EventLiveTiming", back_populates="trial")
    event_duration_stats = relationship("EventDurationStat", back_populates="trial")


class CatalogueEntry(Base):
    __tablename__ = "catalogue_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    day = Column(Integer, nullable=False, default=1)
    event_name = Column(String, nullable=False)
    cat_number = Column(String, nullable=False)
    height_group = Column(Integer, nullable=False)
    run_position = Column(Integer, nullable=False)
    height_group_total = Column(Integer, nullable=False)
    nfc = Column(Boolean, default=False)
    dog_name = Column(String, nullable=True)
    handler_name = Column(String, nullable=True)
    ring_number = Column(String, nullable=True)

    trial = relationship("Trial", back_populates="catalogue_entries")

    __table_args__ = (UniqueConstraint("trial_id", "event_name", "cat_number", "day"),)


class ClassSchedule(Base):
    __tablename__ = "class_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    day = Column(Integer, nullable=True)  # None = applies to any day (legacy / single-day schedules)
    ring_number = Column(String, nullable=False)
    class_name = Column(String, nullable=False)
    scheduled_start = Column(Time, nullable=True)
    ring_setup_mins = Column(Integer, nullable=True)
    walk_mins = Column(Integer, nullable=True)

    trial = relationship("Trial", back_populates="class_schedules")


class TrialLunchBreak(Base):
    __tablename__ = "trial_lunch_breaks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    day = Column(Integer, nullable=False)
    ring = Column(String, nullable=False)
    lunch_break_at = Column(Time, nullable=True)
    lunch_break_mins = Column(Integer, nullable=False, default=45)

    trial = relationship("Trial", back_populates="lunch_breaks")

    __table_args__ = (UniqueConstraint("trial_id", "day", "ring"),)


class SessionEntry(Base):
    __tablename__ = "session_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_uuid = Column(String, ForeignKey("sessions.uuid"), nullable=False)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    dog_name = Column(String, nullable=True)
    height_group = Column(Integer, nullable=True)
    event_name = Column(String, nullable=True)
    cat_number = Column(String, nullable=True)
    catalogue_entry_id = Column(Integer, ForeignKey("catalogue_entries.id"), nullable=True)
    ring_number = Column(String, nullable=True)
    position_override = Column(Integer, nullable=True)
    time_per_dog_override = Column(Integer, nullable=True)

    session = relationship("Session", back_populates="entries")
    trial = relationship("Trial", back_populates="session_entries")
    catalogue_entry = relationship("CatalogueEntry")


class EventLiveTiming(Base):
    __tablename__ = "event_live_timings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False, index=True)
    day = Column(Integer, nullable=False, default=1)
    ring_id = Column(String, nullable=False)  # TopDog internal e.g. "351"
    ring_number = Column(String, nullable=False)  # bare "1"
    event_name = Column(String, nullable=False)
    height_group = Column(Integer, nullable=False)
    status = Column(String, nullable=True)  # Running/Complete/Height Change/Not Running
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    pause_s = Column(Integer, nullable=False, default=0)  # accumulated pause seconds
    duration_s = Column(Integer, nullable=True)
    start_confidence = Column(String, nullable=True)  # high | low
    observed_at = Column(DateTime, nullable=True)

    trial = relationship("Trial", back_populates="event_live_timings")

    __table_args__ = (UniqueConstraint("trial_id", "day", "ring_number", "event_name", "height_group"),)


class EventDurationStat(Base):
    __tablename__ = "event_duration_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False, index=True)
    event_name = Column(String, nullable=False)
    height_group = Column(Integer, nullable=False)
    sample_count = Column(Integer, nullable=False, default=0)
    median_duration_s = Column(Integer, nullable=True)
    last_duration_s = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=True)

    trial = relationship("Trial", back_populates="event_duration_stats")

    __table_args__ = (UniqueConstraint("trial_id", "event_name", "height_group"),)


class SessionFriend(Base):
    __tablename__ = "session_friends"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_uuid = Column(String, ForeignKey("sessions.uuid"), nullable=False)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    handler_name = Column(String, nullable=True)
    cat_number = Column(String, nullable=True)
    label = Column(String, nullable=True)
    pin_key = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("Session", back_populates="friends")
    trial = relationship("Trial", back_populates="session_friends")

    __table_args__ = (UniqueConstraint("session_uuid", "trial_id", "pin_key"),)


def normalize_handler_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"^[\s·]+", "", name.strip())
    return re.sub(r"\s+", " ", cleaned).casefold()


def friend_pin_key(*, handler_name: str | None = None, cat_number: str | None = None) -> str:
    if handler_name:
        return f"handler:{normalize_handler_name(handler_name)}"
    return f"cat:{cat_number or ''}"

