import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    # In containerised deploys (Nomad / docker-compose) DATABASE_URL is injected
    # by env or a Consul template. If we're running inside /app and the var is
    # missing, that's a deploy timing issue (e.g. bar-hopping-db not yet healthy
    # when the template rendered). Fail fast with a clear message instead of
    # silently falling back to a SQLite path that doesn't exist in the slim
    # web image — Nomad will restart the task once the template re-renders.
    if Path("/app/app/main.py").exists():
        sys.stderr.write(
            "FATAL: DATABASE_URL is not set. Bar Hopping is running inside a "
            "container but no database URL was provided. If you're on Nomad, "
            "the bar-hopping-db service may not have registered yet — exiting "
            "so the task is restarted once the Consul template re-renders. "
            "Otherwise, set DATABASE_URL on the web task.\n"
        )
        raise SystemExit(2)
    # Local dev fallback. Ensure ./data exists so a fresh checkout just works.
    Path("./data").mkdir(parents=True, exist_ok=True)
    DATABASE_URL = "sqlite:///./data/barhopping.db"

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
