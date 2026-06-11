"""Parse schedule documents (PDF or HTML) from TopDog."""
import io
import re
import httpx
from datetime import time as dtime
from bs4 import BeautifulSoup


async def download_and_parse_schedule(url: str, cookies: dict[str, str] | None = None) -> list[dict]:
    """Fetch the schedule doc and parse it. TopDog's /schedule/get endpoint
    redirects unauthenticated requests to /users/sign_in, so callers must
    pass session cookies obtained from app.scraper.auth.get_authed_cookies."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60, cookies=cookies or {}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    # If follow_redirects landed us back on a login page, the cookies were missing/expired.
    if "/users/sign_in" in str(resp.url):
        raise ValueError(f"schedule download redirected to sign_in — missing auth cookies ({url})")
    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return parse_schedule_pdf(resp.content)
    return parse_schedule_html(resp.text)


def parse_schedule_pdf(data: bytes) -> list[dict]:
    try:
        import pdfplumber
    except ImportError:
        return []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        # Parse the whole document at once so day/ring headers carry across
        # page boundaries (multi-day schedules span many pages).
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    return _parse_schedule_text(text)


def parse_schedule_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    return _parse_schedule_text(text)


def _parse_schedule_text(text: str) -> list[dict]:
    """
    Heuristically extract ring, class name, and start time from raw text.
    Patterns vary by club; this handles common formats.
    """
    results = []
    lines = text.splitlines()

    current_ring = None
    current_day = None  # None = applies to any day; set when a day header is seen
    day_order: dict[str, int] = {}  # weekday name -> sequential day number
    for line in lines:
        line = line.strip()

        # Explicit day header: "Day 1", "DAY 2".
        day_m = re.match(r"(?i)^Day\s+(\d+)\b", line)
        if day_m:
            current_day = int(day_m.group(1))
            continue

        # Weekday header: "Saturday", "Sunday 23 June", etc. Assign sequential
        # day numbers in order of first appearance to match catalogue day indexing.
        wd_m = re.match(
            r"(?i)^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", line
        )
        if wd_m:
            wd = wd_m.group(1).lower()
            if wd not in day_order:
                day_order[wd] = len(day_order) + 1
            current_day = day_order[wd]
            continue

        # Ring header: "Ring 1", "Ring 2", "RING 1"
        ring_m = re.match(r"(?i)^Ring\s*(\d+)", line)
        if ring_m:
            current_ring = ring_m.group(1)
            continue

        # Time + class: "8:00 AM  Masters Agility" or "08:00 Masters Jumping"
        time_m = re.match(
            r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+(.+)", line, re.I
        )
        if time_m:
            time_str = time_m.group(1).strip()
            class_name = time_m.group(2).strip()
            parsed_time = _parse_time(time_str)
            if parsed_time and _looks_like_class(class_name):
                results.append({
                    "day": current_day,
                    "ring_number": current_ring or "1",
                    "class_name": class_name,
                    "scheduled_start": parsed_time,
                })

    return results


def _parse_time(s: str) -> dtime | None:
    s = s.strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            from datetime import datetime
            dt = datetime.strptime(s.upper(), fmt)
            return dt.time()
        except ValueError:
            pass
    return None


def _looks_like_class(s: str) -> bool:
    return any(kw in s for kw in ("Agility", "Jumping", "Gamblers", "Snooker", "Tunnelers", "Weaving"))
