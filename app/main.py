from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from app.database import engine, Base
from app.routers import sessions, trials, schedule

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Bar Hopping — Dog Agility Planner")

templates = Jinja2Templates(directory="app/templates")

app.include_router(sessions.router)
app.include_router(trials.router)
app.include_router(schedule.router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html")
