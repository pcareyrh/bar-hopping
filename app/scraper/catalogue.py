"""Parse FINAL catalogue files (.xlsx or .pdf) from TopDog, and HTML entries summary pages."""
import io
import logging
import re
import httpx
import openpyxl
from typing import BinaryIO
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


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


def parse_catalogue_pdf(data: bytes) -> list[dict]:
    """Parse a TopDog FINAL catalogue PDF.

    Uses pdfplumber word positions to reliably split dog name from handler
    based on the fixed column layout used by TopDog catalogue PDFs.

    Returns list of dicts with the same schema as parse_catalogue_xlsx.
    """
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — cannot parse PDF catalogue")
        return []

    pages_lines = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages_lines.append(_extract_pdf_lines(page))
    return _parse_pdf_pages(pages_lines)


# Format A header (legacy): "Saturday - Novice Jumping (JD) Judge: ..."
_RE_HEADER_DAY_PREFIX = re.compile(
    r"^(Saturday|Sunday)\s*-\s*(.+?)\s*\([A-Z]+\)\s*Judge:", re.I
)
# Format B header: "Masters Agility (ADM) 300 Judge: ..."
_RE_HEADER_HEIGHT_INLINE = re.compile(
    r"^(.+?)\s*\([A-Z]+\)\s+(\d{3})\s+Judge:", re.I
)
# Format B day marker: "Ring 1 SATURDAY AM" / "Ring 2 SUNDAY"
_RE_RING_DAY = re.compile(r"^Ring\s+(\d+)\s+(SATURDAY|SUNDAY)\b", re.I)
# Format C (trial 1482 Saturday): "Saturday Ring 1 AM -Cam List - Novice  Judge: Cam List - AD1"
# pdfplumber word extraction can corrupt the "Judge:" segment ("ExcellenJu  dge"), so we
# anchor on day + ring at the start and the class code at the end.
_RE_HEADER_RING_TRAILING_CODE = re.compile(
    r"^(Saturday|Sunday)\s+Ring\s+(\d+)\s+(?:AM|PM)\b.*?[-\s]+([A-Z]{2,4}\d?)\s*$",
    re.I,
)
# Format D (trial 1482 Sunday): "Sunday RIng 1 - Robyn Jones/Cam List - Open Agility (ADO)"
# Note "RIng" typo in source; matched case-insensitively.
_RE_HEADER_RING_PAREN_CODE = re.compile(
    r"^(Saturday|Sunday)\s+R[Ii]ng\s+(\d+)\b.*?\(([A-Z]{2,4})\)\s*$",
    re.I,
)
# Format E ring/day header (ADC Pawlympics style):
#   "SATURDAY - RING 1 - AM Judge Cam List (NZ)"
#   "SUNDAY - RING 2 - Judges Cam List / Robyn Jones"
# The class code appears on the NEXT line as a standalone token (e.g. "AD1").
_RE_HEADER_RING_DAY_SESSION = re.compile(
    r"^(SATURDAY|SUNDAY)\s*-\s*RING\s+(\d+)\s*-\s*(.+)$",
    re.I,
)
# Standalone class code line (used by Format E as the line after the ring header).
_RE_STANDALONE_CLASS_CODE = re.compile(r"^([A-Z]{2,4}\d?)\s*$")
# Class code → canonical event name. Codes with a trailing digit (AD1/AD2/ADX1)
# denote separate sessions of the same class on the same day; the digit is
# preserved in event_name to keep them distinct.
_CLASS_CODE_TO_NAME = {
    "AD": "Novice Agility",
    "ADX": "Excellent Agility",
    "ADM": "Masters Agility",
    "ADO": "Open Agility",
    "JD": "Novice Jumping",
    "JDX": "Excellent Jumping",
    "JDM": "Masters Jumping",
    "JDO": "Open Jumping",
}


def _event_name_from_code(code: str) -> str | None:
    """Map a class code (e.g. AD1, ADX, JDO) to its canonical event name.

    A trailing session digit (1=AM, 2=PM on Saturday) is kept in the name so
    that AM/PM runs of the same class don't collide on (event_name, day).
    """
    m = re.match(r"^([A-Z]+?)(\d*)$", code)
    if not m:
        return None
    base, num = m.groups()
    name = _CLASS_CODE_TO_NAME.get(base)
    if not name:
        return None
    return f"{name} ({code})" if num else name


def _parse_pdf_pages(pages_lines: list[list[dict]]) -> list[dict]:
    """Parse pre-extracted pages of lines into catalogue entries.

    Handles five TopDog PDF header formats:
      A. "Saturday - Class (CODE) Judge: ..." (day in header)
      B. "Class (CODE) 300 Judge: ..." with separate "Ring N SATURDAY" markers
      C. "Saturday Ring N AM ... - CODE" (day, ring, and class code in header)
      D. "Sunday R[Ii]ng N - ... (CODE)" (day, ring, and class code in header)
      E. "SATURDAY - RING 1 - AM Judge ..." with class code on the NEXT line
    """
    results: list[dict] = []
    current_event: str | None = None
    current_header_height: int | None = None
    current_ring: str | None = None
    current_day = 1
    seen_events: set[str] = set()
    # Keyed by (event, height, ring) so the same class running in two rings
    # (or AM/PM sessions distinguished by event_name) gets independent buckets.
    height_groups: dict[tuple, list] = {}

    def _flush_and_reset_to_day(new_day: int) -> None:
        nonlocal height_groups, seen_events, current_day
        _flush_height_groups(results, height_groups, current_day)
        height_groups = {}
        seen_events = set()
        current_day = new_day

    for lines in pages_lines:
        for line in lines:
            full_text = line["text"]

            # Format E: standalone class code line (e.g. "AD1", "JDM2").
            # Must be checked early — before entry-row matching would skip it.
            code_m = _RE_STANDALONE_CLASS_CODE.match(full_text)
            if code_m:
                event = _event_name_from_code(code_m.group(1))
                if event:
                    current_event = event
                    current_header_height = None
                    seen_events.add(event)
                continue

            # Format E ring/day header — "SATURDAY - RING 1 - AM Judge ..."
            ring_day_m = _RE_HEADER_RING_DAY_SESSION.match(full_text)
            if ring_day_m:
                day_str = ring_day_m.group(1)
                ring_str = ring_day_m.group(2)
                day_num = 2 if "sun" in day_str.lower() else 1
                if day_num != current_day:
                    _flush_and_reset_to_day(day_num)
                current_ring = ring_str
                continue

            # Format B day marker — explicit Ring SAT/SUN line.
            ring_m = _RE_RING_DAY.match(full_text)
            if ring_m:
                current_ring = ring_m.group(1)
                day_num = 2 if ring_m.group(2).upper().startswith("SUN") else 1
                if day_num != current_day:
                    _flush_and_reset_to_day(day_num)
                continue

            # Format A header
            header_m = _RE_HEADER_DAY_PREFIX.match(full_text)
            if header_m:
                day_str = header_m.group(1).lower()
                event = header_m.group(2).strip()
                day_num = 2 if "sun" in day_str else 1
                if event in seen_events and day_num == current_day:
                    _flush_and_reset_to_day(current_day + 1)
                elif day_num != current_day:
                    _flush_and_reset_to_day(day_num)
                current_event = event
                current_header_height = None
                current_ring = None
                seen_events.add(event)
                continue

            # Format C/D headers carry day + ring + class code in one line.
            header_m = _RE_HEADER_RING_TRAILING_CODE.match(full_text) \
                or _RE_HEADER_RING_PAREN_CODE.match(full_text)
            if header_m:
                day_str, ring_str, code = header_m.groups()
                event = _event_name_from_code(code)
                if not event:
                    continue
                day_num = 2 if "sun" in day_str.lower() else 1
                if day_num != current_day:
                    _flush_and_reset_to_day(day_num)
                current_event = event
                current_header_height = None
                current_ring = ring_str
                seen_events.add(event)
                continue

            # Format B header — height is part of the header line.
            header_m = _RE_HEADER_HEIGHT_INLINE.match(full_text)
            if header_m:
                height = int(header_m.group(2))
                if height not in (200, 300, 400, 500, 600):
                    continue
                current_event = header_m.group(1).strip()
                current_header_height = height
                # Preserve current_ring set by the preceding Ring N SAT/SUN marker.
                seen_events.add(current_event)
                continue

            if full_text.startswith("Cat#") or "Height Change" in full_text:
                continue

            if current_event is None:
                continue
            entry_m = re.match(r"^(\d{2,4})(NFC)?\s", full_text)
            if not entry_m:
                continue

            cat_number = entry_m.group(1) + (entry_m.group(2) or "")
            nfc = entry_m.group(2) is not None
            if current_header_height is not None:
                height_group = current_header_height
            else:
                cat_digits = int(entry_m.group(1))
                height_group = (cat_digits // 100) * 100
                if height_group not in (200, 300, 400, 500, 600):
                    continue

            dog_name, handler_name = _split_dog_handler(line["words"])

            key = (current_event, height_group, current_ring)
            if key not in height_groups:
                height_groups[key] = []
            height_groups[key].append((cat_number, nfc, dog_name, handler_name))

    _flush_height_groups(results, height_groups, current_day)
    return results




def _extract_pdf_lines(page) -> list[dict]:
    """Extract words from a page and group into logical lines.

    Returns list of dicts with keys: 'text' (full line), 'words' (list of
    word dicts with 'text' and 'x0' keys).
    """
    words = page.extract_words(keep_blank_chars=True)
    if not words:
        return []

    # Group by y-position (tolerance of 3 units for same-line words)
    lines_by_y: dict[float, list] = {}
    for w in words:
        y = round(w["top"] / 3) * 3  # bucket into 3-unit bands
        if y not in lines_by_y:
            lines_by_y[y] = []
        lines_by_y[y].append({"text": w["text"], "x0": w["x0"]})

    result = []
    for y in sorted(lines_by_y.keys()):
        line_words = sorted(lines_by_y[y], key=lambda w: w["x0"])
        full_text = " ".join(w["text"] for w in line_words)
        result.append({"text": full_text, "words": line_words})
    return result


def _split_dog_handler(words: list[dict]) -> tuple[str | None, str | None]:
    """Split entry-row words into (dog_name, handler_name).

    pdfplumber's extract_words with keep_blank_chars groups whitespace within
    a column into a single word and splits on the larger inter-column gaps.
    TopDog entry rows therefore yield exactly 4 chunks: cat#, dog, handler,
    breed. Column x-positions shift between AM/PM sessions and ring pages, so
    we key off position not x.
    """
    if len(words) == 4:
        dog = words[1]["text"].strip() or None
        handler = words[2]["text"].strip() or None
        return dog, handler
    return None, None


async def download_and_parse_catalogue(url: str) -> list[dict]:
    """Download a catalogue (xlsx or PDF) and parse it."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf") or url.endswith("/get"):
        # TopDog's /catalogue/get endpoint returns PDF with application/pdf content-type.
        # Also sniff the first bytes for PDF magic number.
        if resp.content[:5] == b"%PDF-" or "pdf" in content_type:
            log.info("Parsing catalogue as PDF (%d bytes)", len(resp.content))
            return parse_catalogue_pdf(resp.content)
    log.info("Parsing catalogue as xlsx (%d bytes)", len(resp.content))
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

    current_day_label = "Sat"
    current_day_num = 1
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
                if "sun" in day_text:
                    current_day_label = "Sun"
                    current_day_num = 2
                else:
                    current_day_label = "Sat"
                    current_day_num = 1
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

            cat_number = f"~{current_day_label}~{height}"
            key = (event_name, cat_number)
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "event_name": event_name,
                "cat_number": cat_number,
                "day": current_day_num,
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


def _flush_height_groups(results: list, height_groups: dict, day: int) -> None:
    for key, entries in height_groups.items():
        # Key is either (event, height) (xlsx) or (event, height, ring) (PDF).
        if len(key) == 3:
            event_name, height_group, ring_number = key
        else:
            event_name, height_group = key
            ring_number = None
        non_nfc_total = sum(1 for _, nfc, _, _ in entries if not nfc)
        for pos, (cat_number, nfc, dog_name, handler_name) in enumerate(entries, start=1):
            results.append({
                "event_name": event_name,
                "cat_number": cat_number,
                "day": day,
                "height_group": height_group,
                "run_position": pos,
                "height_group_total": non_nfc_total,
                "nfc": nfc,
                "dog_name": dog_name,
                "handler_name": handler_name,
                "ring_number": ring_number,
            })


def _parse_worksheet(ws) -> list[dict]:
    current_event: str | None = None
    current_height: int | None = None
    current_day = 1
    # Track which events have appeared this day to detect day boundaries.
    seen_events: set[str] = set()
    height_groups: dict[tuple, list] = {}
    results: list[dict] = []

    for row in ws.iter_rows(values_only=True):
        col_a = str(row[0]).strip() if row[0] is not None else ""
        col_b = row[1]

        if "Agility Trial" in col_a:
            event = _normalize_event_name(col_a)
            # Same event appearing again means we crossed into the next day.
            if event in seen_events:
                _flush_height_groups(results, height_groups, current_day)
                height_groups = {}
                seen_events = set()
                current_day += 1
            current_event = event
            seen_events.add(event)
            current_height = None
            continue

        if col_a == "Cat#":
            continue
        if not col_a:
            continue
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

    _flush_height_groups(results, height_groups, current_day)
    return results
