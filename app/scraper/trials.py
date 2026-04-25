"""Scrape trial detail pages from topdogevents.com.au.

Trial discovery is user-scoped (see app.scraper.auth.sync_user_entries) because
the public /trials listing's state/discipline query filters are ignored by the
site. This module just fetches metadata (venue, date, doc links) for a known
trial id.
"""
import re
from datetime import datetime
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})")
MONTH_MAP = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "May": "May", "Jun": "June", "Jul": "July", "Aug": "August",
    "Sep": "September", "Sept": "September", "Oct": "October",
    "Nov": "November", "Dec": "December",
}


async def scrape_trial_detail(external_id: str) -> dict:
    """Fetch a single trial detail page (own browser — for one-off use)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        result = await _scrape_trial_detail_with_page(page, external_id)
        await browser.close()
    return result


async def scrape_trial_details_batch(external_ids: list[str], on_progress=None) -> list[dict]:
    """Fetch many trial detail pages reusing a single browser."""
    results: list[dict] = []
    if not external_ids:
        return results

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        total = len(external_ids)
        for i, external_id in enumerate(external_ids, start=1):
            if on_progress:
                on_progress(i, total)
            try:
                results.append(await _scrape_trial_detail_with_page(page, external_id))
            except Exception:
                results.append({"external_id": external_id})
        await browser.close()
    return results


async def _scrape_trial_detail_with_page(page, external_id: str) -> dict:
    url = f"{BASE_URL}/trials/{external_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    content = await page.content()
    return _parse_trial_detail(external_id, content)


def _parse_trial_detail(external_id: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {"external_id": external_id}

    h1 = soup.find("h1")
    result["name"] = h1.get_text(strip=True) if h1 else f"Trial {external_id}"

    date_el = soup.select_one(".page-header h4")
    if date_el:
        m = DATE_RE.search(date_el.get_text())
        if m:
            d, mon, y = m.groups()
            mon_full = MONTH_MAP.get(mon, mon)
            try:
                result["start_date"] = datetime.strptime(f"{d} {mon_full} {y}", "%d %B %Y").date()
            except ValueError:
                pass

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = href if href.startswith("http") else BASE_URL + href
        if href.endswith("/schedule/get"):
            result["schedule_doc_url"] = full
        elif href.endswith("/catalogue/get"):
            result["catalogue_doc_url"] = full

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

    return result
