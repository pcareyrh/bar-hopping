# Bar Hopping

Personalised time predictions for dog agility runs at NSW TopDog trials.

Connect your [topdogevents.com.au](https://www.topdogevents.com.au) account and the app finds your dogs' entries across upcoming NSW Agility trials, then generates a predicted run schedule based on your catalogue position, ring setup time, and course walk.

## Features

- **Automatic entry sync** — authenticates as you on TopDog to discover all your dogs' entries
- **Time predictions** — calculates your estimated run time from scheduled start + setup + walk + queue position
- **FINAL catalogue parsing** — parses the `.xlsx` catalogue to get your actual running order (row position, not cat# order)
- **Inline adjustments** — override your position or timing values on the day; predictions update instantly via HTMX
- **Shareable links** — each planning session gets a UUID link you can bookmark or share across devices
- **Conflict detection** — flags runs where two dogs are predicted to clash within 5 minutes

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12 + FastAPI |
| Scraping | Playwright + BeautifulSoup4 |
| Catalogue | openpyxl (FINAL `.xlsx`) |
| Schedule | pdfplumber / BeautifulSoup4 |
| Database | SQLAlchemy + SQLite |
| Frontend | Jinja2 + HTMX + Tailwind CSS (CDN) |
| Container | Docker + docker-compose |

## Deployment

**1. Generate an encryption key**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**2. Create a `.env` file**

```bash
cp .env.example .env
# paste your key into ENCRYPTION_KEY=
```

**3. Start the app**

```bash
docker compose up -d
```

App is available at `http://localhost:8000`.

The SQLite database is persisted in `./data/` via a Docker volume — it survives container restarts.

## Container image

The image is published to GitHub Container Registry on every push to `main`:

```
ghcr.io/pcareyrh/bar-hopping:main
```

To deploy the pre-built image instead of building locally:

```yaml
# docker-compose.yml
services:
  web:
    image: ghcr.io/pcareyrh/bar-hopping:main
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - DATABASE_URL=sqlite:////app/data/barhopping.db
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
```

## How predictions work

```
first_run_start = scheduled_start + ring_setup_mins + walk_mins
predicted_start = first_run_start + (run_position - 1) × avg_time_per_dog
```

- `run_position` comes from the FINAL catalogue (row order within height group, not cat# numeric order)
- Defaults: 90 s/dog, 10 min setup, 10 min walk — all adjustable in Settings
- Per-run overrides let you correct for queues that change on the day

## Credential security

TopDog credentials are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption before being written to the database. The encryption key lives only in your `.env` file. Credentials are never logged, returned to the browser, or forwarded anywhere — they are used solely server-side by Playwright.

## URL structure

```
/                              Home — create session or resume via link
/s/{uuid}/setup                Enter TopDog credentials and sync
/s/{uuid}/trials               NSW Agility trials list
/s/{uuid}/trials/{id}          Trial detail — your dogs, ring schedule
/s/{uuid}/trials/{id}/schedule Predicted run schedule
/s/{uuid}/settings             Adjust global timing defaults
```
