"""Scrape trial detail pages from topdogevents.com.au.

Trial discovery is user-scoped (see app.scraper.auth.sync_user_entries) because
the public /trials listing's state/discipline query filters are ignored by the
site. This module just fetches metadata (venue, date, doc links) for a known
trial id.
"""
import re
from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})")
JUDGING_RE = re.compile(r"Judging starts?\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM))", re.I)
MONTH_MAP = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "May": "May", "Jun": "June", "Jul": "July", "Aug": "August",
    "Sep": "September", "Sept": "September", "Oct": "October",
    "Nov": "November", "Dec": "December",
}


async def fetch_trial_detail(external_id: str) -> dict:
    """Fetch and parse a trial detail page using httpx (no browser required).

    Used by refresh_trial_docs_job to update catalogue/schedule URLs for
    existing trials.
    """
    url = f"{BASE_URL}/trials/{external_id}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return _parse_trial_detail(external_id, resp.text)


async def scrape_trial_detail(external_id: str) -> dict:
    """Fetch a single trial detail page."""
    return await fetch_trial_detail(external_id)


async def scrape_trial_details_batch(external_ids: list[str], on_progress=None) -> list[dict]:
    """Fetch many trial detail pages reusing a single httpx client."""
    results: list[dict] = []
    if not external_ids:
        return results

    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        total = len(external_ids)
        for i, external_id in enumerate(external_ids, start=1):
            if on_progress:
                on_progress(i, total)
            try:
                url = f"{BASE_URL}/trials/{external_id}"
                resp = await client.get(url)
                resp.raise_for_status()
                results.append(_parse_trial_detail(external_id, resp.text))
            except Exception:
                results.append({"external_id": external_id})
    return results


def _parse_trial_detail(external_id: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {"external_id": external_id}

    h1 = soup.find("h1")
    result["name"] = h1.get_text(strip=True) if h1 else f"Trial {external_id}"

    date_el = soup.select_one(".page-header h4")
    if date_el:
        start_date, end_date = _parse_dates(date_el.get_text())
        if start_date:
            result["start_date"] = start_date
        if end_date:
            result["end_date"] = end_date

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = href if href.startswith("http") else BASE_URL + href
        if href.endswith("/schedule/get"):
            result["schedule_doc_url"] = full
        elif href.endswith("/catalogue/get"):
            result["catalogue_doc_url"] = full

    # If the trial is closed but has no xlsx catalogue, the entries summary
    # page serves as the catalogue source in HTML format.
    if "catalogue_doc_url" not in result and soup.find(attrs={"title": "This trial is closed"}):
        result["catalogue_doc_url"] = f"{BASE_URL}/trials/{external_id}/entries"

    loc = soup.select_one("#location")
    if loc:
        venue_name = loc.select_one("h6")
        address = loc.find("p")
        parts = []
        if venue_name:
            parts.append(venue_name.get_text(" ", strip=True))
        if address:
            parts.append(address.get_text(" ", strip=True))
        if parts:
            result["venue"] = " — ".join(parts)

    m = JUDGING_RE.search(soup.get_text(" "))
    if m:
        for fmt in ("%I:%M %p", "%I:%M%p"):
            try:
                result["start_time"] = datetime.strptime(m.group(1).strip().upper(), fmt).time()
                break
            except ValueError:
                pass

    return result


def _parse_dates(text: str) -> tuple[date | None, date | None]:
    dates: list[date] = []
    for m in DATE_RE.finditer(text or ""):
        d, mon, y = m.groups()
        mon_full = MONTH_MAP.get(mon, mon)
        try:
            dates.append(datetime.strptime(f"{d} {mon_full} {y}", "%d %B %Y").date())
        except ValueError:
            continue
    if not dates:
        return None, None
    return min(dates), max(dates)
