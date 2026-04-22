"""Parse schedule documents (PDF or HTML) from TopDog."""
import io
import re
import httpx
from datetime import time as dtime
from bs4 import BeautifulSoup


async def download_and_parse_schedule(url: str) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return parse_schedule_pdf(resp.content)
    return parse_schedule_html(resp.text)


def parse_schedule_pdf(data: bytes) -> list[dict]:
    try:
        import pdfplumber
    except ImportError:
        return []

    results = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            results.extend(_parse_schedule_text(text))
    return results


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
    for line in lines:
        line = line.strip()

        # Ring header: "Ring 1", "Ring 2", "RING 1"
        ring_m = re.match(r"(?i)^Ring\s*(\d+)", line)
        if ring_m:
            current_ring = ring_m.group(1)
            continue

        # Time + class: "8:00 AM  Masters Agility" or "08:00 Masters Jumping"
        time_m = re.match(
            r"(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+(.+)", line, re.I
        )
        if time_m and current_ring:
            time_str = time_m.group(1).strip()
            class_name = time_m.group(2).strip()
            parsed_time = _parse_time(time_str)
            if parsed_time and _looks_like_class(class_name):
                results.append({
                    "ring_number": current_ring,
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
