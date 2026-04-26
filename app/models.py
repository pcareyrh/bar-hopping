import re
import uuid
from datetime import datetime, date, time
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Date, Time, Float,
    ForeignKey, UniqueConstraint, Index, func,
)
from sqlalchemy.orm import relationship
from app.database import Base


def _new_uuid():
    return str(uuid.uuid4())


HEIGHT_GROUPS = (200, 300, 400, 500, 600)

HANDLER_PLACEHOLDER = "-"


def normalise_name(s: str | None) -> str | None:
    """Normalise a dog or handler name for matching.

    Lowercase, strip "(AI)" / "(ai)" parenthetical, drop punctuation,
    collapse whitespace. Returns None for empty input.
    """
    if not s:
        return None
    s = s.lower()
    s = re.sub(r"\(\s*ai\s*\)", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def normalise_handler(s: str | None) -> str:
    """Normalise a handler name, falling back to HANDLER_PLACEHOLDER.

    Always returns a non-empty string so the (name_normalised, handler_normalised)
    composite unique index treats unknown handlers as a single bucket rather
    than as distinct NULLs.
    """
    n = normalise_name(s)
    return n or HANDLER_PLACEHOLDER


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
    default_setup_mins = Column(Integer, default=10)
    default_walk_mins = Column(Integer, default=10)
    last_results_view_at = Column(DateTime, nullable=True)

    entries = relationship("SessionEntry", back_populates="session", cascade="all, delete-orphan")

    def tpd_for(self, height_group: int | None) -> int:
        if height_group in HEIGHT_GROUPS:
            value = getattr(self, f"tpd_{height_group}", None)
            if value is not None:
                return value
        return self.avg_time_per_dog or 90


class Trial(Base):
    __tablename__ = "trials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    venue = Column(String, nullable=True)
    state = Column(String, nullable=True)
    discipline = Column(Integer, nullable=True)         # TopDog discipline id (1=Agility)
    schedule_doc_url = Column(String, nullable=True)
    catalogue_doc_url = Column(String, nullable=True)
    scraped_at = Column(DateTime, nullable=True)
    results_synced_at = Column(DateTime, nullable=True)
    results_status = Column(String, nullable=True)      # ok | none | error:<short>

    catalogue_entries = relationship("CatalogueEntry", back_populates="trial", cascade="all, delete-orphan")
    class_schedules = relationship("ClassSchedule", back_populates="trial", cascade="all, delete-orphan")
    session_entries = relationship("SessionEntry", back_populates="trial")
    trial_results = relationship("TrialResult", back_populates="trial", cascade="all, delete-orphan")


class CatalogueEntry(Base):
    __tablename__ = "catalogue_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    event_name = Column(String, nullable=False)
    cat_number = Column(String, nullable=False)
    height_group = Column(Integer, nullable=False)
    run_position = Column(Integer, nullable=False)
    height_group_total = Column(Integer, nullable=False)
    nfc = Column(Boolean, default=False)
    dog_name = Column(String, nullable=True)
    handler_name = Column(String, nullable=True)

    trial = relationship("Trial", back_populates="catalogue_entries")

    __table_args__ = (UniqueConstraint("trial_id", "event_name", "cat_number"),)


class ClassSchedule(Base):
    __tablename__ = "class_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False)
    ring_number = Column(String, nullable=False)
    class_name = Column(String, nullable=False)
    scheduled_start = Column(Time, nullable=True)
    ring_setup_mins = Column(Integer, nullable=True)
    walk_mins = Column(Integer, nullable=True)

    trial = relationship("Trial", back_populates="class_schedules")


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


class Dog(Base):
    __tablename__ = "dogs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    name_normalised = Column(String, nullable=False, index=True)
    handler_name = Column(String, nullable=True)
    handler_normalised = Column(String, nullable=False, index=True)  # '-' placeholder when unknown
    first_seen_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("name_normalised", "handler_normalised"),)


class TrialResult(Base):
    __tablename__ = "trial_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trial_id = Column(Integer, ForeignKey("trials.id"), nullable=False, index=True)
    sub_trial_external_id = Column(String, nullable=False, index=True)
    sub_trial_label = Column(String, nullable=True)
    class_slug = Column(String, nullable=False, index=True)
    class_label = Column(String, nullable=False)
    height_group = Column(Integer, nullable=False, index=True)
    sct_seconds = Column(Float, nullable=True)
    course_length_m = Column(Integer, nullable=True)
    judge_name = Column(String, nullable=True)
    dog_id = Column(Integer, ForeignKey("dogs.id"), nullable=True, index=True)
    dog_name_raw = Column(String, nullable=False)
    handler_name_raw = Column(String, nullable=True)
    time_seconds = Column(Float, nullable=True)
    total_faults = Column(Float, nullable=True)
    status = Column(String, nullable=True)              # Q | DQ | ABS | None
    nfc = Column(Boolean, default=False)
    row_index = Column(Integer, nullable=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)

    trial = relationship("Trial", back_populates="trial_results")
    dog = relationship("Dog")

    __table_args__ = (
        UniqueConstraint(
            "trial_id", "sub_trial_external_id", "class_slug", "height_group", "row_index",
            name="uq_trial_result_row",
        ),
    )
