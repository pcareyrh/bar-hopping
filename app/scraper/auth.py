"""Authenticate to TopDog and scrape a user's entries for upcoming trials."""
import re
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup

BASE_URL = "https://www.topdogevents.com.au"

# Height text → integer mm mapping
HEIGHT_MAP = {
    "200": 200, "300": 300, "400": 400, "500": 500, "600": 600,
    "200mm": 200, "300mm": 300, "400mm": 400, "500mm": 500, "600mm": 600,
}


async def sync_user_entries(
    email: str,
    password: str,
    trial_external_ids: list[str],
    on_progress=None,
) -> list[dict]:
    """
    Log in to TopDog and return a list of entry dicts for the given trials.

    Each dict: {
        trial_external_id, dog_name, height_group, event_name,
        cat_number, sequence_number, ring_number (nullable)
    }
    """
    entries: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()

        # Log in
        await page.goto(f"{BASE_URL}/users/sign_in", wait_until="networkidle")
        await page.fill("input[name='user[email]']", email)
        await page.fill("input[name='user[password]']", password)
        await page.click("input[type='submit'], button[type='submit']")
        await page.wait_for_load_state("networkidle")

        # Confirm login succeeded (look for sign-out link)
        if "sign_in" in page.url:
            await browser.close()
            raise ValueError("TopDog login failed — check credentials")

        total = len(trial_external_ids)
        for i, external_id in enumerate(trial_external_ids, start=1):
            if on_progress:
                on_progress(i, total)
            trial_entries = await _scrape_my_entries(page, external_id)
            entries.extend(trial_entries)

        await browser.close()

    return entries


async def _scrape_my_entries(page, external_id: str) -> list[dict]:
    """Navigate to a trial's 'My Entries' tab and extract entry rows."""
    # TopDog trial entries page — adjust path if needed
    url = f"{BASE_URL}/trials/{external_id}/entries/my_entries"
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
    except PlaywrightTimeout:
        return []

    content = await page.content()
    return _parse_my_entries(external_id, content)


def _parse_my_entries(external_id: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    entries = []

    # Find entry rows — structure varies by TopDog version; adapt as needed
    for row in soup.select("table tr, .entry-row, [class*='entry']"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        texts = [c.get_text(strip=True) for c in cells]

        # Skip header rows
        if any(h in texts[0].lower() for h in ("dog", "class", "event", "height")):
            continue

        entry = _extract_entry_fields(texts, external_id)
        if entry:
            entries.append(entry)

    # Fallback: look for structured divs
    if not entries:
        for item in soup.select("[data-dog], .dog-entry, .my-entry"):
            entry = _extract_entry_from_element(item, external_id)
            if entry:
                entries.append(entry)

    return entries


def _extract_entry_fields(texts: list[str], external_id: str) -> dict | None:
    """Heuristically extract entry fields from table cell texts."""
    if len(texts) < 3:
        return None

    dog_name = None
    height_group = None
    event_name = None
    cat_number = None
    sequence_number = None
    ring_number = None

    for i, text in enumerate(texts):
        # Height detection
        for key, val in HEIGHT_MAP.items():
            if text == key or text.lower() == key:
                height_group = val
                break

        # Cat number: numeric string optionally ending in NFC
        if re.match(r"^\d{3}(NFC)?$", text):
            cat_number = text

        # Sequence number: plain integer
        if re.match(r"^\d+$", text) and int(text) < 200 and cat_number is None:
            sequence_number = int(text)

        # Ring number: "Ring 1", "1", etc.
        rm = re.match(r"^(?:Ring\s*)?(\d+)$", text, re.I)
        if rm and height_group and ring_number is None:
            ring_number = rm.group(1)

        # Event name heuristic: contains "Agility" or "Jumping"
        if any(kw in text for kw in ("Agility", "Jumping", "Gamblers", "Snooker", "Tunnelers")):
            event_name = text

        # Dog name: first non-numeric, non-height, multi-char field
        if dog_name is None and len(text) > 2 and not text.isdigit() and "mm" not in text.lower():
            if not any(kw in text for kw in ("Agility", "Jumping", "Ring", "Cat", "Height")):
                dog_name = text

    if not (event_name or cat_number):
        return None

    return {
        "trial_external_id": external_id,
        "dog_name": dog_name,
        "height_group": height_group,
        "event_name": event_name,
        "cat_number": cat_number,
        "sequence_number": sequence_number,
        "ring_number": ring_number,
    }


def _extract_entry_from_element(el, external_id: str) -> dict | None:
    texts = [t for t in el.stripped_strings]
    return _extract_entry_fields(texts, external_id)
