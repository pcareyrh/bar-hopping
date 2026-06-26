"""Scrape the TopDog /trials/{id}/live ring-status board.

Public HTML page listing every ring's current event segment and status.
Event-level fields only — per-dog #last_run and #class_runs_left are ignored.
"""
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

log = logging.getLogger(__name__)

_RING_ID_RE = re.compile(r"^ring_(\d+)$", re.I)
_RING_NUM_RE = re.compile(r"\bRing\s*(\d+)\b", re.I)
_CLASS_HEIGHT_RE = re.compile(r"^(.+?)\s*\((\d{3})\)\s*$")


def parse_class_name(text: str) -> tuple[str, int | None]:
    """'Excellent Gamblers (400)' -> ('Excellent Gamblers', 400)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return "", None
    m = _CLASS_HEIGHT_RE.match(text)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return text, None


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        log.warning("live: invalid timestamp %r", raw)
        return None


def parse_ring_status(html: str) -> list[dict]:
    """Parse all `.ring-card` elements into event-level ring snapshots."""
    soup = BeautifulSoup(html, "html.parser")
    rings: list[dict] = []

    for card in soup.select(".ring-card"):
        card_id = card.get("id") or ""
        rm = _RING_ID_RE.match(card_id)
        if not rm:
            continue
        ring_id = rm.group(1)

        name_el = card.select_one(".live-item-name")
        ring_text = name_el.get_text(" ", strip=True) if name_el else ""
        rn_match = _RING_NUM_RE.search(ring_text)
        ring_number = rn_match.group(1) if rn_match else ring_text

        class_el = card.select_one("#class_name")
        class_text = class_el.get_text(" ", strip=True) if class_el else ""
        event_name, height_group = parse_class_name(class_text)

        status = (card.get("data-status") or "").strip()
        if not status:
            status_el = card.select_one("#status")
            status = status_el.get_text(" ", strip=True) if status_el else ""

        updated_el = card.select_one("#updated")
        updated_raw = updated_el.get("data-timestamp") if updated_el else None
        updated = _parse_timestamp(updated_raw)

        rings.append({
            "ring_id": ring_id,
            "ring_number": ring_number,
            "event_name": event_name,
            "height_group": height_group,
            "status": status,
            "updated": updated,
        })

    return rings


async def fetch_ring_status(trial_external_id: str) -> dict:
    """GET /trials/{id}/live and return a ring snapshot for the trial."""
    url = f"{BASE_URL}/trials/{trial_external_id}/live"
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        rings = parse_ring_status(resp.text)

    return {
        "trial_external_id": trial_external_id,
        "observed_at": datetime.now(timezone.utc),
        "rings": rings,
    }
