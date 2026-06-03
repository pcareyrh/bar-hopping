"""Scrape the TopDog /trials/{id}/my_day dashboard.

The /my_day page is an authenticated per-trial dashboard that lists every
ring/class block in order, with full per-dog entry lists in current run
order. It replaces the older xlsx/PDF catalogue + schedule scraping with a
single HTML format that is stable across clubs and reflects post-scratch
order.

Returned shape from `fetch_my_day`:

    {
        "catalogue_entries": [
            {event_name, cat_number, day, height_group, run_position,
             height_group_total, nfc, dog_name, handler_name},
            ...
        ],
        "class_schedules": [
            {ring_number, class_name, scheduled_start, ring_setup_mins,
             walk_mins},
            ...
        ],
        "start_time": <datetime.time or None>,
    }
"""
import asyncio
import logging
import re
from datetime import time as dtime, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

log = logging.getLogger(__name__)

_DAY_LABEL_RE = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I)
_TIME_PAREN_RE = re.compile(r"\(\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*\)", re.I)
_HEIGHT_RE = re.compile(r"(200|300|400|500|600)")
_CLASS_HREF_RE = re.compile(r"/trials/(\d+)/my_day/(\d+)/(\d+)")


class MyDayUnavailable(Exception):
    """Raised when /my_day is not available for this trial (404 or older trial)."""


class MyDayAuthRequired(Exception):
    """Raised when the request was redirected to the sign-in page."""


def _day_num(label: str | None) -> int:
    if not label:
        return 1
    s = label.lower()
    if "sun" in s:
        return 2
    if "mon" in s:
        return 3
    if "tue" in s:
        return 4
    if "wed" in s:
        return 5
    if "thu" in s:
        return 6
    if "fri" in s:
        return 7
    return 1


def _parse_time(s: str | None) -> dtime | None:
    if not s:
        return None
    s = s.strip().lower().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            continue
    return None


def _height_from_cat(cat_number: str) -> int | None:
    m = re.match(r"^(\d)", cat_number)
    if not m:
        return None
    h = int(m.group(1)) * 100
    return h if h in (200, 300, 400, 500, 600) else None


def parse_my_day_index(html: str) -> list[dict]:
    """Parse the /my_day landing page into a list of session blocks.

    Each session block has:
        {day_label, day_num, start_time, rings: [
            {ring_name, ring_id, classes: [
                {class_name, class_id, day_id, ring_id, href, is_mine}
            ]}
        ]}
    """
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("#my-day-index")
    if not container:
        return []

    sessions: list[dict] = []
    current: dict | None = None
    current_ring: dict | None = None

    for el in container.find_all(True, recursive=True):
        classes = set(el.get("class") or [])

        if "my-day-event-header" in classes:
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            day_m = _DAY_LABEL_RE.search(text)
            day_label = day_m.group(1) if day_m else text
            # session label = full header (e.g. "Saturday AM")
            session_label = text.split("(")[0].strip()
            time_m = _TIME_PAREN_RE.search(text)
            start_time = _parse_time(time_m.group(1)) if time_m else None
            current = {
                "day_label": session_label,
                "day_num": _day_num(day_label),
                "start_time": start_time,
                "rings": [],
            }
            current_ring = None
            sessions.append(current)
            continue

        if "my-day-ring-row" in classes:
            ring_name_el = el.select_one(".my-day-ring-name")
            ring_name = ring_name_el.get_text(strip=True) if ring_name_el else "Ring 1"
            # Determine ring_id from the first badge href in this row.
            first_badge = el.select_one("a.my-day-class-badge[href]")
            ring_id = None
            if first_badge:
                href = first_badge.get("href", "")
                rm = re.search(r"ring_id=(\d+)", href)
                if rm:
                    ring_id = rm.group(1)
            current_ring = {
                "ring_name": ring_name,
                "ring_id": ring_id,
                "classes": [],
            }
            if current is not None:
                current["rings"].append(current_ring)

            for badge in el.select("a.my-day-class-badge[href]"):
                href = badge.get("href", "")
                m = _CLASS_HREF_RE.search(href)
                if not m:
                    continue
                _trial, day_id, class_id = m.groups()
                badge_classes = set(badge.get("class") or [])
                is_mine = "my-day-class-badge--mine" in badge_classes
                long_name = badge.select_one(".d-none.d-sm-inline")
                class_name = long_name.get_text(strip=True) if long_name else \
                    re.sub(r"\s+", " ", badge.get_text(" ", strip=True))
                current_ring["classes"].append({
                    "class_name": class_name,
                    "class_id": class_id,
                    "day_id": day_id,
                    "ring_id": ring_id,
                    "href": href,
                    "is_mine": is_mine,
                })

    return sessions


def parse_my_day_detail(html: str) -> list[dict]:
    """Parse one class-detail page into a flat ordered list of entries.

    Each entry: {cat_number, dog_name, handler_name, height_group, nfc}.
    Order matches the on-page run order, with entries outside the
    "more-upcoming" collapse (the immediately upcoming runs) first.
    """
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict] = []

    progress = soup.select_one("#my-day-progress") or soup
    seen = set()

    # Walk in document order. Track current height from height-separator
    # markers, fall back to cat#-derived height if none seen yet.
    for el in progress.find_all("div", class_=["my-day-height-separator", "my-day-entry-row"]):
        klass = set(el.get("class") or [])
        if "my-day-height-separator" in klass:
            # height separators are also used as the More upcoming link wrapper;
            # only treat as height marker when it contains a numeric height.
            text = el.get_text(" ", strip=True)
            hm = _HEIGHT_RE.search(text)
            if hm:
                el_height = int(hm.group(1))
                el._current_height = el_height  # ignored, just for clarity
            continue
        if "my-day-entry-row" not in klass:
            continue
        eid = el.get("id") or ""
        if eid in seen:
            continue
        seen.add(eid)

        badge = el.select_one(".badge")
        cat_raw = badge.get_text(strip=True) if badge else ""
        cm = re.match(r"^(\d{2,4})(NFC)?$", cat_raw, re.I)
        if not cm:
            continue
        cat_number = cm.group(1) + ("NFC" if cm.group(2) else "")
        nfc = cm.group(2) is not None

        strong = el.select_one("strong")
        dog_name = strong.get_text(strip=True) if strong else None
        handler_el = el.select_one(".text-muted")
        handler_name = None
        if handler_el:
            handler_name = re.sub(r"^[\s·]+", "", handler_el.get_text(" ", strip=True)).strip() or None

        height_group = _height_from_cat(cat_number)
        if height_group is None:
            continue

        entries.append({
            "cat_number": cat_number,
            "dog_name": dog_name,
            "handler_name": handler_name,
            "height_group": height_group,
            "nfc": nfc,
        })

    return entries


async def fetch_my_day(external_id: str, cookies: dict[str, str]) -> dict[str, Any]:
    """Fetch the my_day index and every class-detail page for a trial.

    Returns dicts shaped to populate CatalogueEntry / ClassSchedule rows
    directly. Raises MyDayUnavailable on 404, MyDayAuthRequired on
    redirect to /users/sign_in.
    """
    base = f"{BASE_URL}/trials/{external_id}/my_day"
    async with httpx.AsyncClient(cookies=cookies, follow_redirects=True, timeout=60) as c:
        resp = await c.get(base)
        if resp.status_code == 404:
            raise MyDayUnavailable(f"my_day not available for trial {external_id}")
        if "/users/sign_in" in str(resp.url):
            raise MyDayAuthRequired(f"my_day requires authentication (redirected to {resp.url})")
        resp.raise_for_status()

        sessions = parse_my_day_index(resp.text)
        if not sessions:
            raise MyDayUnavailable(f"my_day index empty for trial {external_id}")

        # Flatten all classes (with metadata) into a stable ordered list so
        # asyncio.gather can return results in matching order.
        class_tasks: list[tuple] = []  # (day_num, start_time, ring_name, class_idx, cls)
        for session in sessions:
            for ring in session["rings"]:
                for class_idx, cls in enumerate(ring["classes"]):
                    class_tasks.append((
                        session["day_num"],
                        session["start_time"],
                        ring["ring_name"],
                        class_idx,
                        cls,
                    ))

        # Fetch all class-detail pages concurrently (max 4 in flight).
        sem = asyncio.Semaphore(4)

        async def _fetch_detail(url: str) -> str | None:
            async with sem:
                try:
                    dr = await c.get(url)
                    dr.raise_for_status()
                    return dr.text
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    log.warning("my_day: failed to fetch %s: %s", url, e)
                    return None

        detail_htmls = await asyncio.gather(*[
            _fetch_detail(BASE_URL + t[4]["href"]) for t in class_tasks
        ])

        catalogue_entries: list[dict] = []
        class_schedules: list[dict] = []
        seen_cat: set[tuple[str, str]] = set()  # (event_name, cat_number)

        for (day_num, start_time, ring_name, class_idx, cls), html in zip(class_tasks, detail_htmls):
            if html is None:
                continue
            class_name = cls["class_name"]
            entries = parse_my_day_detail(html)

            # Group by height, preserve in-page order for run_position.
            by_h: dict[int, list[dict]] = {}
            for e in entries:
                by_h.setdefault(e["height_group"], []).append(e)
            for height, group in by_h.items():
                non_nfc_total = sum(1 for e in group if not e["nfc"])
                for pos, e in enumerate(group, start=1):
                    key = (class_name, e["cat_number"])
                    if key in seen_cat:
                        continue
                    seen_cat.add(key)
                    catalogue_entries.append({
                        "event_name": class_name,
                        "cat_number": e["cat_number"],
                        "day": day_num,
                        "height_group": height,
                        "run_position": pos,
                        "height_group_total": non_nfc_total,
                        "nfc": e["nfc"],
                        "dog_name": e["dog_name"],
                        "handler_name": e["handler_name"],
                        "ring_number": ring_name,
                    })

            class_schedules.append({
                "ring_number": ring_name,
                "class_name": class_name,
                "scheduled_start": start_time if class_idx == 0 else None,
                "ring_setup_mins": None,
                "walk_mins": None,
            })

        earliest = min(
            (s["start_time"] for s in sessions if s.get("start_time")),
            default=None,
        )

        return {
            "catalogue_entries": catalogue_entries,
            "class_schedules": class_schedules,
            "start_time": earliest,
        }
