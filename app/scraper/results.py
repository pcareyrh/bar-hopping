"""Scrape past trial results from topdogevents.com.au.

Public endpoints only — no Devise login, no Playwright. Plain httpx + bs4.

WORKER-IMAGE ONLY. Do not import this module at top level from anywhere
under app/routers/ or app/main.py — the slim web image lacks httpx and bs4
and will crash uvicorn at startup.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.topdogevents.com.au"

DISCIPLINE_AGILITY = 1

CLASS_LABEL_FROM_SLUG = {
    "novice_agility": "Novice Agility",
    "excellent_agility": "Excellent Agility",
    "masters_agility": "Masters Agility",
    "open_agility": "Open Agility",
    "novice_jumping": "Novice Jumping",
    "excellent_jumping": "Excellent Jumping",
    "masters_jumping": "Masters Jumping",
    "open_jumping": "Open Jumping",
}

_HEIGHT_HEADER_RE = re.compile(
    r"^(?P<label>.+?)\s*-\s*(?P<height>200|300|400|500|600)\b",
    re.I,
)
_SCT_RE = re.compile(r"(?:Standard Course Time|SCT):\s*([\d.]+)", re.I)
_LENGTH_RE = re.compile(r"Course Length:\s*(\d+)", re.I)
_JUDGE_RE = re.compile(r"Judge:\s*(.+?)(?:\.|$)", re.I)

log = logging.getLogger("scraper.results")


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        timeout=timeout,
        limits=httpx.Limits(max_connections=4),
        headers={"User-Agent": "bar-hopping/1.0 (+https://bar-hopping.secure.carey.id)"},
    )


# ---- Trial discovery ------------------------------------------------------

async def list_nsw_agility_trials(
    client: httpx.AsyncClient,
    since: date | None = None,
    page_size: int = 200,
) -> list[dict]:
    """Paginate /results.json?discipline=1&state=NSW.

    Stops when a short page comes back (no `total` field on the endpoint).
    Filters client-side by `since` since the API ignores date filters.
    """
    all_rows: list[dict] = []
    offset = 0
    while True:
        r = await client.get(
            "/results.json",
            params={
                "discipline": DISCIPLINE_AGILITY,
                "state": "NSW",
                "limit": page_size,
                "offset": offset,
            },
        )
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
        await asyncio.sleep(0.1)

    out: list[dict] = []
    for row in all_rows:
        ext_id = str(row.get("id"))
        start_str = row.get("start_date")
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date() if start_str else None
        except ValueError:
            start_date = None
        if since and start_date and start_date < since:
            continue
        out.append({
            "external_id": ext_id,
            "name": row.get("name") or f"Trial {ext_id}",
            "start_date": start_date,
            "club_name": row.get("club_name"),
            "state": row.get("state") or "New South Wales",
        })
    return out


# ---- Per-trial sub-trial enumeration --------------------------------------

async def fetch_event_subtrials(client: httpx.AsyncClient, event_id: str) -> list[tuple[str, str]]:
    """Return [(sub_trial_id, label)] from the trial_selection <select>."""
    r = await client.get(f"/results/{event_id}")
    if r.status_code == 404:
        return []
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    select = soup.find("select", id="trial_selection")
    if not select:
        # No sub-trial selector — the page either has no results or only one
        # default sub-trial. Treat as none; the caller marks results_status="none".
        return []
    options = []
    for opt in select.find_all("option"):
        value = (opt.get("value") or "").strip()
        label = opt.get_text(" ", strip=True)
        if value:
            options.append((value, label))
    return options


# ---- Results parsing ------------------------------------------------------

async def fetch_subtrial_results(
    client: httpx.AsyncClient,
    event_id: str,
    sub_trial_id: str,
    sub_trial_label: str | None = None,
) -> list[dict]:
    r = await client.get(f"/results/{event_id}/trial/{sub_trial_id}")
    r.raise_for_status()
    return parse_subtrial_html(r.text, sub_trial_id, sub_trial_label)


def parse_subtrial_html(html: str, sub_trial_id: str, sub_trial_label: str | None) -> list[dict]:
    """Flatten one sub-trial page into a list of run dicts."""
    soup = BeautifulSoup(html, "html.parser")
    runs: list[dict] = []

    for card in soup.select("div.card[id^='d_']"):
        card_id = card.get("id", "")
        class_slug = card_id[2:] if card_id.startswith("d_") else card_id
        class_label = CLASS_LABEL_FROM_SLUG.get(class_slug, class_slug.replace("_", " ").title())

        table = card.find("table")
        if not table:
            continue

        current_height: int | None = None
        current_sct: float | None = None
        current_length: int | None = None
        current_judge: str | None = None
        row_index = 0

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds:
                continue

            # Height-group header: a single bg-nfc colspan=5 cell.
            header_td = tr.find("td", class_="bg-nfc")
            if header_td and header_td.get("colspan") == "5":
                current_height, current_sct, current_length, current_judge = _parse_header_cell(header_td)
                row_index = 0
                continue

            if current_height is None:
                continue

            run = _parse_run_row(tds)
            if run is None:
                continue

            row_index += 1
            run.update({
                "sub_trial_external_id": sub_trial_id,
                "sub_trial_label": sub_trial_label,
                "class_slug": class_slug,
                "class_label": class_label,
                "height_group": current_height,
                "sct_seconds": current_sct,
                "course_length_m": current_length,
                "judge_name": current_judge,
                "row_index": row_index,
            })
            runs.append(run)

    return runs


def _parse_header_cell(td) -> tuple[int | None, float | None, int | None, str | None]:
    text = td.get_text(" ", strip=True)
    height: int | None = None
    sct: float | None = None
    length: int | None = None
    judge: str | None = None

    m = _HEIGHT_HEADER_RE.search(text)
    if m:
        try:
            height = int(m.group("height"))
        except ValueError:
            pass

    m = _SCT_RE.search(text)
    if m:
        try:
            sct = float(m.group(1))
        except ValueError:
            pass

    m = _LENGTH_RE.search(text)
    if m:
        try:
            length = int(m.group(1))
        except ValueError:
            pass

    m = _JUDGE_RE.search(text)
    if m:
        judge = m.group(1).strip()

    return height, sct, length, judge


def _parse_run_row(tds) -> dict | None:
    """Parse a single results row.

    Layout: [place(blank/optional), 'Dog - Handler', blank, time, total_faults]
    DQ rows render the time cell with colspan=3 and text 'Disqualified'.
    """
    # The first cell is occasionally a placement marker. Look for the dog/handler
    # cell as the first td whose stripped text contains ' - ' (last occurrence).
    dog_cell = None
    dog_idx = None
    for i, td in enumerate(tds[:3]):
        txt = td.get_text(" ", strip=True)
        if " - " in txt:
            dog_cell = txt
            dog_idx = i
            break
    if dog_cell is None:
        return None

    dog_name_raw, handler_name_raw, nfc = _split_dog_handler(dog_cell)
    if not dog_name_raw:
        return None

    remaining = tds[dog_idx + 1:]

    status: str | None = None
    time_seconds: float | None = None
    total_faults: float | None = None

    # Disqualified rows: any remaining cell whose text contains 'Disqualified'.
    if any("disqualif" in td.get_text(" ", strip=True).lower() for td in remaining):
        status = "DQ"
        return {
            "dog_name_raw": dog_name_raw,
            "handler_name_raw": handler_name_raw,
            "time_seconds": None,
            "total_faults": None,
            "status": status,
            "nfc": nfc,
        }

    # Normal rows: pick numeric cells for time and total faults from the tail.
    numeric_texts = [td.get_text(" ", strip=True) for td in remaining]
    nums = [_to_float(t) for t in numeric_texts]
    nums = [n for n in nums if n is not None]

    if len(nums) >= 2:
        time_seconds, total_faults = nums[-2], nums[-1]
    elif len(nums) == 1:
        # Only one number — treat as total_faults if it looks small, else time.
        n = nums[0]
        if n < 25:
            total_faults = n
        else:
            time_seconds = n

    if time_seconds is None and total_faults is None:
        status = "ABS"

    # TopDog leaves the faults cell blank for clean (0-fault) runs. Infer 0.
    if status is None and time_seconds is not None and total_faults is None:
        total_faults = 0.0

    return {
        "dog_name_raw": dog_name_raw,
        "handler_name_raw": handler_name_raw,
        "time_seconds": time_seconds,
        "total_faults": total_faults,
        "status": status,
        "nfc": nfc,
    }


def _split_dog_handler(text: str) -> tuple[str, str | None, bool]:
    """Split on the LAST ' - ' so dogs whose names contain hyphens parse right."""
    nfc = False
    s = text.strip()
    # NFC suffix sometimes appears as " (NFC)" — strip it before splitting.
    nfc_match = re.search(r"\s*\(\s*nfc\s*\)\s*$", s, re.I)
    if nfc_match:
        nfc = True
        s = s[:nfc_match.start()].strip()

    idx = s.rfind(" - ")
    if idx == -1:
        return s, None, nfc
    dog = s[:idx].strip()
    handler = s[idx + 3:].strip() or None
    return dog, handler, nfc


def _to_float(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
