from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text

from app.database import engine, Base
from app.models import HEIGHT_GROUPS
from app.routers import sessions, trials, schedule

Base.metadata.create_all(bind=engine)


def _migrate_per_height_tpd():
    """Add tpd_<height> columns to sessions if missing, backfilling from
    avg_time_per_dog. SQLite's ALTER TABLE ADD COLUMN is safe and idempotent
    via the column-existence check."""
    existing = {c["name"] for c in inspect(engine).get_columns("sessions")}
    with engine.begin() as conn:
        for h in HEIGHT_GROUPS:
            col = f"tpd_{h}"
            if col not in existing:
                conn.execute(text(f"ALTER TABLE sessions ADD COLUMN {col} INTEGER"))
                conn.execute(text(
                    f"UPDATE sessions SET {col} = COALESCE(avg_time_per_dog, 90)"
                ))


_migrate_per_height_tpd()

app = FastAPI(title="Bar Hopping — Dog Agility Planner")

templates = Jinja2Templates(directory="app/templates")

app.include_router(sessions.router)
app.include_router(trials.router)
app.include_router(schedule.router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")
