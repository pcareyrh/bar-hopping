import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app._version import VERSION
from app.database import engine, Base, SessionLocal
from app.models import HEIGHT_GROUPS

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
from app.routers import sessions, trials, schedule

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
    # TODO: orphaned results-feature schema (columns last_results_view_at,
    # results_synced_at, results_status; tables dogs, trial_results) still
    # exists in deployed databases. Run migrations/cleanup_results_schema.py
    # after backing up trial_results data — see migrations/README.md.
    with engine.begin() as conn:
        # Per-height time-per-dog columns on sessions, backfilled from avg_time_per_dog.
        for h in HEIGHT_GROUPS:
            col = f"tpd_{h}"
            _add_column_if_missing(
                conn, "sessions", col, "INTEGER",
                backfill_sql=f"UPDATE sessions SET {col} = COALESCE(avg_time_per_dog, 90) WHERE {col} IS NULL",
            )

        # Per-height Jumping tpd columns, backfilled from matching Agility values.
        for h in HEIGHT_GROUPS:
            col = f"tpd_jumping_{h}"
            _add_column_if_missing(
                conn, "sessions", col, "INTEGER",
                backfill_sql=f"UPDATE sessions SET {col} = COALESCE(tpd_{h}, avg_time_per_dog, 90) WHERE {col} IS NULL",
            )

        _add_column_if_missing(conn, "trials", "discipline", "INTEGER")
        _add_column_if_missing(conn, "trials", "start_time", "TIME")
        _add_column_if_missing(conn, "trials", "lunch_break_at", "TIME")
        _add_column_if_missing(conn, "trials", "lunch_break_mins", "INTEGER")
        _add_column_if_missing(
            conn, "catalogue_entries", "day", "INTEGER",
            backfill_sql="UPDATE catalogue_entries SET day = 1 WHERE day IS NULL",
        )
        _add_column_if_missing(conn, "catalogue_entries", "ring_number", "VARCHAR")
        _add_column_if_missing(conn, "class_schedules", "day", "INTEGER")
        _add_column_if_missing(conn, "trials", "live_status", "VARCHAR")
        _add_column_if_missing(conn, "trials", "live_synced_at", "TIMESTAMP")

        # Widen unique constraint to include day (Nationals: same dog runs same event on multiple days).
        if conn.dialect.name == "postgresql":
            conn.execute(text(
                "ALTER TABLE catalogue_entries DROP CONSTRAINT IF EXISTS "
                "catalogue_entries_trial_id_event_name_cat_number_key"
            ))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS catalogue_entries_trial_id_event_name_cat_number_day_key "
                "ON catalogue_entries (trial_id, event_name, cat_number, day)"
            ))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trials_start_date ON trials(start_date)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_event_live_timings_trial_id ON event_live_timings(trial_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_event_duration_stats_trial_id ON event_duration_stats(trial_id)"
        ))



try:
    log.info("Bar Hopping version %s", VERSION)
    log.info("Running migrations")
    _migrate()
    log.info("Startup complete")
except Exception:
    log.exception("Startup failed")
    raise

app = FastAPI(title="Bar Hopping — Dog Agility Planner")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["APP_VERSION"] = VERSION

app.include_router(sessions.router)
app.include_router(trials.router)
app.include_router(schedule.router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")
