"""Authenticate to TopDog and scrape upcoming trials + entries from /entries."""
import re
from datetime import datetime, date
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

HEIGHT_RE = re.compile(r"^\s*(200|300|400|500|600)\s*(mm)?\s*$", re.I)
CAT_RE = re.compile(r"^\s*(\d{2,4})(NFC)?\s*$", re.I)
DATE_RE = re.compile(r"(\w+day),\s*(\d{1,2})\s+(\w+)\s+(\d{4})")


async def _login(page, email: str, password: str) -> None:
    """Submit the Devise sign-in form and raise if credentials are rejected."""
    await page.goto(f"{BASE_URL}/users/sign_in", wait_until="domcontentloaded", timeout=20_000)
    await page.fill("input[name='user[email]']", email)
    await page.fill("input[name='user[password]']", password)
    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
            await page.click("button[type='submit'], input[type='submit']")
    except PlaywrightTimeout:
        pass

    if "/users/sign_in" in page.url or page.url.rstrip("/").endswith("/users/sign_in"):
        raise ValueError(f"TopDog login failed — check credentials (still at {page.url})")


async def get_authed_cookies(email: str, password: str) -> dict[str, str]:
    """Log in and return the session cookies as a dict suitable for httpx."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _login(page, email, password)
            cookies = await context.cookies()
        finally:
            await browser.close()
    return {c["name"]: c["value"] for c in cookies}


async def sync_user_entries(
    email: str,
    password: str,
    on_progress=None,
) -> list[dict]:
    """
    Log in to TopDog, visit /entries, and return trials the user is in.

    Each trial dict:
        {external_id, name, start_date, entries: [
            {trial_external_id, dog_name, event_name, height_group, cat_number}
        ]}

    Only trials with upcoming dates are returned (the page itself shows
    upcoming only, but we also filter by date as a safeguard).
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()

        if on_progress:
            on_progress(0, 1)

        try:
            await _login(page, email, password)
        except ValueError:
            await browser.close()
            raise

        await page.goto(f"{BASE_URL}/entries", wait_until="domcontentloaded", timeout=30_000)
        html = await page.content()
        await browser.close()

    trials = _parse_entries_page(html)
    today = date.today()
    return [t for t in trials if not t["start_date"] or t["start_date"] >= today]


def _parse_entries_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    trials: list[dict] = []

    for pane in soup.select("div.tab-pane[id^='t']"):
        m = re.match(r"^t(\d+)$", pane.get("id", ""))
        if not m:
            continue
        external_id = m.group(1)

        strong = pane.find("strong")
        if not strong:
            continue
        name = _clean(strong.get_text())

        small = pane.find("small", class_="text-muted")
        start_date = _parse_date(small.get_text(" ", strip=True)) if small else None

        entries: list[dict] = []
        for row in pane.select("table tbody tr"):
            entry = _parse_entry_row(row, external_id)
            if entry:
                entries.append(entry)

        trials.append({
            "external_id": external_id,
            "name": name,
            "start_date": start_date,
            "entries": entries,
        })

    return trials


def _parse_entry_row(row, external_id: str) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 4:
        return None
    texts = [_clean(c.get_text(" ", strip=True)) for c in cells]
    # Column order on /entries: # | Dog | Class | Height | Judge | Status | (edit)
    cat_raw, dog, event, height_raw = texts[0], texts[1], texts[2], texts[3]

    if not dog or not event:
        return None

    cat_number = None
    cm = CAT_RE.match(cat_raw)
    if cm:
        cat_number = cm.group(1) + ("NFC" if cm.group(2) else "")

    height = None
    hm = HEIGHT_RE.match(height_raw)
    if hm:
        height = int(hm.group(1))

    return {
        "trial_external_id": external_id,
        "dog_name": dog,
        "event_name": event,
        "height_group": height,
        "cat_number": cat_number,
    }


def _parse_date(text: str):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    _, d, mon, y = m.groups()
    try:
        return datetime.strptime(f"{d} {mon} {y}", "%d %B %Y").date()
    except ValueError:
        return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
