# AGENTS.md

## Cursor Cloud specific instructions

Bar Hopping is a Python 3.12 / FastAPI app that predicts dog-agility run times. See `README.md` for the product overview, URL map, and the production `docker compose` flow. The notes below are the non-obvious bits for developing here without Docker.

### Services

| Service | Command (from repo root) | Notes |
|---|---|---|
| Web (FastAPI) | `set -a && . ./.env && set +a && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload` | Serves UI on port 8000. |
| Worker (RQ) | `set -a && . ./.env && set +a && .venv/bin/rq worker default` | Runs TopDog scraping jobs; needs Redis + Playwright chromium. |
| Redis | `sudo service redis-server start` | Required by the worker/queue and the credential-sync flow. Not auto-started. |
| Tests | `.venv/bin/python -m pytest -q` | Pure unit tests (in-memory SQLite); no DB/Redis/network needed. |

There is no separate lint tool configured; `.venv/bin/python -m py_compile $(find app scripts migrations -name '*.py')` is used as a syntax check.

### Non-obvious caveats

- **Use the virtualenv.** Dependencies live in `.venv` (the update script creates/refreshes it). Prefix commands with `.venv/bin/...` or activate it; the system Python does not have the deps.
- **No Postgres needed for local dev.** `app/database.py` falls back to `sqlite:///./data/barhopping.db` when `DATABASE_URL` is unset. `docker-compose.yml` uses Postgres + Redis, but native dev runs fine on SQLite. The web app and worker share that SQLite file only if both are started from the repo root.
- **Env vars are not auto-loaded.** The app does not read `.env` itself. Source it first: `set -a && . ./.env && set +a`. `.env` is gitignored; create it from `.env.example`. `ENCRYPTION_KEY` (a Fernet key) is required only for the credential-sync flow but should be set so the worker can run. Generate one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **End-to-end TopDog sync needs real credentials.** The worker's `sync_session_job` logs into `topdogevents.com.au` via Playwright. Without real TopDog credentials there is no live trial/catalogue data, so the trials list stays empty. The schedule-prediction and parsing logic is fully covered by the pytest suite instead.
- **Schema migrations are additive and run automatically** on web startup (`_migrate()` in `app/main.py`) for both SQLite and Postgres — no separate migration command for normal dev.
