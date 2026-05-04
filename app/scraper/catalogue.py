"""Parse FINAL catalogue .xlsx files from TopDog, and HTML entries summary pages."""
import io
import re
import httpx
import openpyxl
from typing import BinaryIO
from bs4 import BeautifulSoup


def parse_catalogue_xlsx(file_obj: BinaryIO) -> list[dict]:
    """
    Parse a TopDog FINAL catalogue xlsx.

    Returns list of dicts:
        event_name, cat_number, height_group, run_position,
        height_group_total, nfc, dog_name, handler_name

    Note: we deliberately avoid read_only=True. TopDog's xlsx ships with a
    stale <dimension> tag in the sheet XML (claims 200 rows when the sheet
    actually has 400+), which openpyxl trusts in read-only mode and uses to
    stop iteration early — silently dropping later events like Masters
    Jumping / Open Jumping. Loading fully forces a real row count.
    """
    wb = openpyxl.load_workbook(file_obj, data_only=True)
    ws = wb.active
    return _parse_worksheet(ws)


async def download_and_parse_catalogue(url: str) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return parse_catalogue_xlsx(io.BytesIO(resp.content))


async def download_and_parse_catalogue_entries(url: str) -> list[dict]:
    """Fetch the /trials/{id}/entries HTML page and return synthetic catalogue dicts.

    Used when a trial is closed but has no xlsx catalogue download. Returns one
    entry per (day, event_name, height_group) with sentinel cat_number='~{Day}~{height}',
    run_position=0, and height_group_total from the displayed count.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return parse_catalogue_entries_html(resp.text)


def parse_catalogue_entries_html(html: str) -> list[dict]:
    """Parse a TopDog /trials/{id}/entries summary page as a catalogue substitute.

    The page shows entry counts per class per height group (not individual dogs).
    Synthetic CatalogueEntry dicts are created with:
      - cat_number = '~{DayAbbr}~{height}'  (sentinel, never a real cat#)
      - run_position = 0                      (sentinel = no run order known)
      - height_group_total from the page count

    _resolve_catalogue_links uses a fallback query to match SessionEntry rows
    by event_name + height_group against these sentinels, giving users
    height_group_total even without individual run order.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (event_name, cat_number) uniqueness guard

    current_day = "Sat"
    card_body = soup.select_one(".card-body")
    if not card_body:
        return []

    for el in card_body.children:
        if not hasattr(el, "get"):
            continue
        classes = set(el.get("class") or [])

        # Day separator: d-flex ... border-bottom
        if "border-bottom" in classes:
            h6 = el.find("h6")
            if h6:
                day_text = h6.get_text(strip=True).lower()
                current_day = "Sun" if "sun" in day_text else "Sat"
            continue

        # Class block: d-block text-dark rounded
        if "d-block" not in classes or "text-dark" not in classes:
            continue
        strong = el.find("strong")
        if not strong:
            continue
        event_name = strong.get_text(strip=True)

        for badge in el.select("span.badge-light"):
            nums = re.findall(r"\d+", badge.get_text())
            if len(nums) < 2:
                continue
            height = int(nums[0])
            count = int(nums[-1])
            if height not in (200, 300, 400, 500, 600):
                continue

            cat_number = f"~{current_day}~{height}"
            key = (event_name, cat_number)
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "event_name": event_name,
                "cat_number": cat_number,
                "height_group": height,
                "run_position": 0,
                "height_group_total": count,
                "nfc": False,
                "dog_name": None,
                "handler_name": None,
            })

    return results


def _normalize_event_name(s: str) -> str:
    """Catalogue xlsx uses e.g. "Agility Trial - Open Agility (ADO)"; the
    /entries page uses "Open Agility". Strip the prefix and abbreviation so
    the two match."""
    s = s.strip()
    s = re.sub(r"^Agility\s+Trial\s*-\s*", "", s, flags=re.I)
    s = re.sub(r"\s*\([A-Z]+\)\s*$", "", s)
    return s.strip()


def _parse_worksheet(ws) -> list[dict]:
    current_event: str | None = None
    current_height: int | None = None
    # Per (event, height) → list of (cat_number, nfc, dog_name, handler_name)
    height_groups: dict[tuple, list] = {}

    for row in ws.iter_rows(values_only=True):
        col_a = str(row[0]).strip() if row[0] is not None else ""
        col_b = row[1]  # Height integer or None

        # Event header row
        if "Agility Trial" in col_a:
            current_event = _normalize_event_name(col_a)
            current_height = None
            continue

        # Column header row
        if col_a == "Cat#":
            continue

        # Skip completely empty rows
        if not col_a:
            continue

        # Data row — col_a is cat_number
        if current_event is None:
            continue

        cat_number = col_a
        nfc = cat_number.upper().endswith("NFC")
        height = int(col_b) if isinstance(col_b, (int, float)) else current_height

        if height is None:
            continue

        current_height = height
        dog_name = str(row[2]).strip() if row[2] else None
        handler_name = str(row[4]).strip() if row[4] else None

        key = (current_event, height)
        if key not in height_groups:
            height_groups[key] = []
        height_groups[key].append((cat_number, nfc, dog_name, handler_name))

    # Build result list with positions and totals
    results = []
    for (event_name, height_group), entries in height_groups.items():
        non_nfc_total = sum(1 for _, nfc, _, _ in entries if not nfc)
        for pos, (cat_number, nfc, dog_name, handler_name) in enumerate(entries, start=1):
            results.append({
                "event_name": event_name,
                "cat_number": cat_number,
                "height_group": height_group,
                "run_position": pos,
                "height_group_total": non_nfc_total,
                "nfc": nfc,
                "dog_name": dog_name,
                "handler_name": handler_name,
            })

    return results
