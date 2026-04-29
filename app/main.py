import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.database import engine, Base, SessionLocal
from app.models import HEIGHT_GROUPS, Dog, SessionEntry, normalise_name, normalise_handler

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
from app.routers import sessions, trials, schedule, results

Base.metadata.create_all(bind=engine)


def _add_column_if_missing(conn, table: str, column: str, ddl: str, backfill_sql: str | None = None) -> None:
    existing = {c["name"] for c in inspect(conn).get_columns(table)}
    if column in existing:
        return
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    if backfill_sql:
        conn.execute(text(backfill_sql))


def _migrate() -> None:
    """Idempotent additive migrations for both SQLite and Postgres."""
    with engine.begin() as conn:
        # Per-height time-per-dog columns on sessions, backfilled from avg_time_per_dog.
        for h in HEIGHT_GROUPS:
            col = f"tpd_{h}"
            _add_column_if_missing(
                conn, "sessions", col, "INTEGER",
                backfill_sql=f"UPDATE sessions SET {col} = COALESCE(avg_time_per_dog, 90) WHERE {col} IS NULL",
            )

        # Past-results extensions on sessions and trials.
        _add_column_if_missing(conn, "sessions", "last_results_view_at", "TIMESTAMP")
        _add_column_if_missing(conn, "trials", "discipline", "INTEGER")
        _add_column_if_missing(conn, "trials", "results_synced_at", "TIMESTAMP")
        _add_column_if_missing(conn, "trials", "results_status", "VARCHAR")

        # Backfill total_faults=0 for completed timed runs where faults were
        # stored as NULL (TopDog renders blank, not "0", for clean runs).
        conn.execute(text(
            "UPDATE trial_results SET total_faults = 0"
            " WHERE status IS NULL AND time_seconds IS NOT NULL AND total_faults IS NULL"
        ))

        # Indexes on existing tables (create_all only handles new tables).
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trials_results_status ON trials(results_status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trials_start_date ON trials(start_date)"
        ))


def _seed_dogs_from_session_entries() -> None:
    """One-shot: populate the dogs table from existing SessionEntry rows.

    Only fires when dogs is empty so re-runs are no-ops.
    """
    db = SessionLocal()
    try:
        if db.query(Dog.id).first() is not None:
            return
        rows = (
            db.query(SessionEntry.dog_name)
            .filter(SessionEntry.dog_name.isnot(None))
            .distinct()
            .all()
        )
        seen: set[tuple[str, str]] = set()
        for (raw_name,) in rows:
            name_norm = normalise_name(raw_name)
            if not name_norm:
                continue
            handler_norm = normalise_handler(None)
            key = (name_norm, handler_norm)
            if key in seen:
                continue
            seen.add(key)
            db.add(Dog(
                name=raw_name,
                name_normalised=name_norm,
                handler_name=None,
                handler_normalised=handler_norm,
            ))
        db.commit()
    finally:
        db.close()


try:
    log.info("Running migrations")
    _migrate()
    log.info("Seeding dogs from session entries")
    _seed_dogs_from_session_entries()
    log.info("Startup complete")
except Exception:
    log.exception("Startup failed")
    raise

app = FastAPI(title="Bar Hopping — Dog Agility Planner")

templates = Jinja2Templates(directory="app/templates")

app.include_router(sessions.router)
app.include_router(trials.router)
app.include_router(schedule.router)
app.include_router(results.router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")
