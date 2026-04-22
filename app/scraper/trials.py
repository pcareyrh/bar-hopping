"""Scrape the trials list and trial detail pages from topdogevents.com.au."""
import re
from datetime import datetime, date, timedelta
from typing import Optional
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

# Cache trials for 4 hours
CACHE_TTL_HOURS = 4


async def scrape_trials_list() -> list[dict]:
    """Return list of upcoming NSW Agility trials as dicts."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE_URL}/trials?discipline=Agility&state=NSW", wait_until="networkidle")
        content = await page.content()
        await browser.close()

    return _parse_trials_list(content)


def _parse_trials_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    trials = []

    for link in soup.select("a[href*='/trials/']"):
        href = link.get("href", "")
        m = re.search(r"/trials/(\d+)", href)
        if not m:
            continue
        external_id = m.group(1)
        name = link.get_text(strip=True)
        if not name:
            continue
        trials.append({"external_id": external_id, "name": name})

    return trials


async def scrape_trial_detail(external_id: str) -> dict:
    """Fetch trial detail page and return structured data."""
    url = f"{BASE_URL}/trials/{external_id}"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle")
        content = await page.content()
        await browser.close()

    return _parse_trial_detail(external_id, content)


def _parse_trial_detail(external_id: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {"external_id": external_id}

    # Trial name
    h1 = soup.find("h1")
    result["name"] = h1.get_text(strip=True) if h1 else f"Trial {external_id}"

    # Dates — look for date patterns
    text = soup.get_text()
    date_m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text)
    if date_m:
        try:
            result["start_date"] = datetime.strptime(date_m.group(1), "%d %B %Y").date()
        except ValueError:
            pass

    # Venue / state
    venue_el = soup.find(class_=re.compile(r"venue|location|address", re.I))
    result["venue"] = venue_el.get_text(strip=True) if venue_el else None
    result["state"] = "NSW"

    # Document links (schedule / catalogue)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        lower = href.lower()
        text_lower = a.get_text(strip=True).lower()
        if "schedule" in lower or "schedule" in text_lower:
            result["schedule_doc_url"] = href if href.startswith("http") else BASE_URL + href
        elif "catalogue" in lower or "catalogue" in text_lower or "catalog" in text_lower:
            result["catalogue_doc_url"] = href if href.startswith("http") else BASE_URL + href

    return result
