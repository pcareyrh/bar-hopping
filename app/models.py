import uuid
from datetime import datetime, date, time
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, Date, Time,
    ForeignKey, UniqueConstraint, func,
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
    default_setup_mins = Column(Integer, default=10)
    default_walk_mins = Column(Integer, default=10)

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
    schedule_doc_url = Column(String, nullable=True)
    catalogue_doc_url = Column(String, nullable=True)
    scraped_at = Column(DateTime, nullable=True)

    catalogue_entries = relationship("CatalogueEntry", back_populates="trial", cascade="all, delete-orphan")
    class_schedules = relationship("ClassSchedule", back_populates="trial", cascade="all, delete-orphan")
    session_entries = relationship("SessionEntry", back_populates="trial")


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
