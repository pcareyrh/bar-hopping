"""Authenticate to TopDog and scrape upcoming trials + entries from /entries."""
import re
from datetime import datetime, date

import httpx
from bs4 import BeautifulSoup

from app.trial_dates import trial_dict_active_on

BASE_URL = "https://www.topdogevents.com.au"
SIGN_IN_URL = f"{BASE_URL}/users/sign_in"
ENTRIES_URL = f"{BASE_URL}/entries"

_DEFAULT_HEADERS = {
    "User-Agent": "BarHopping/1.0",
    "Accept": "text/html,application/xhtml+xml",
}

HEIGHT_RE = re.compile(r"^\s*(200|300|400|500|600)\s*(mm)?\s*$", re.I)
CAT_RE = re.compile(r"^\s*(\d{2,4})(NFC)?\s*$", re.I)
DATE_RE = re.compile(r"(?:\w+day,\s*)?(\d{1,2})\s+(\w+)\s+(\d{4})")


def _client_kwargs() -> dict:
    return {
        "follow_redirects": True,
        "timeout": 30,
        "headers": _DEFAULT_HEADERS,
    }


def _extract_csrf_token(html: str) -> str:
    """Read Devise CSRF token from the sign-in page (meta tag or hidden input)."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    inp = soup.find("input", attrs={"name": "authenticity_token"})
    if inp and inp.get("value"):
        return inp["value"]
    raise ValueError("CSRF token not found on TopDog sign-in page")


def _redirected_to_sign_in(response: httpx.Response) -> bool:
    return "/users/sign_in" in str(response.url)


def _raise_for_status(response: httpx.Response, *, context: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ValueError(
            f"TopDog {context} returned HTTP {e.response.status_code}"
        ) from e


def _ensure_authenticated(response: httpx.Response, *, context: str) -> None:
    _raise_for_status(response, context=context)
    if _redirected_to_sign_in(response):
        raise ValueError(
            f"TopDog authentication failed for {context} — redirected to {response.url}"
        )


async def _login(client: httpx.AsyncClient, email: str, password: str) -> None:
    """Submit the Devise sign-in form and raise if credentials are rejected."""
    try:
        resp = await client.get(SIGN_IN_URL)
    except httpx.RequestError as e:
        raise ValueError(f"TopDog sign-in page request failed: {e}") from e
    _raise_for_status(resp, context="sign-in page")
    token = _extract_csrf_token(resp.text)
    try:
        resp = await client.post(
            SIGN_IN_URL,
            data={
                "user[email]": email,
                "user[password]": password,
                "authenticity_token": token,
            },
            headers={"Referer": SIGN_IN_URL},
        )
    except httpx.RequestError as e:
        raise ValueError(f"TopDog login request failed: {e}") from e
    _raise_for_status(resp, context="login")
    if _redirected_to_sign_in(resp):
        raise ValueError(f"TopDog login failed — check credentials (still at {resp.url})")


def _cookies_dict(client: httpx.AsyncClient) -> dict[str, str]:
    return {name: value for name, value in client.cookies.items()}


async def get_authed_cookies(email: str, password: str) -> dict[str, str]:
    """Log in and return the session cookies as a dict suitable for httpx."""
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        await _login(client, email, password)
        return _cookies_dict(client)


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

    Trials that have not yet completed by the current day are returned (the
    page itself shows upcoming only, but we also filter by date as a safeguard).
    """
    async with httpx.AsyncClient(**_client_kwargs()) as client:
        if on_progress:
            on_progress(0, 1)
        await _login(client, email, password)
        try:
            resp = await client.get(ENTRIES_URL)
        except httpx.RequestError as e:
            raise ValueError(f"TopDog /entries request failed: {e}") from e
        _ensure_authenticated(resp, context="/entries")
        html = resp.text

    trials = _parse_entries_page(html)
    today = date.today()
    return [t for t in trials if trial_dict_active_on(t, today=today)]


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
        start_date, end_date = _parse_dates(small.get_text(" ", strip=True)) if small else (None, None)

        entries: list[dict] = []
        for row in pane.select("table tbody tr"):
            entry = _parse_entry_row(row, external_id)
            if entry:
                entries.append(entry)

        trials.append({
            "external_id": external_id,
            "name": name,
            "start_date": start_date,
            "end_date": end_date,
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


def _parse_dates(text: str) -> tuple[date | None, date | None]:
    dates: list[date] = []
    for d, mon, y in DATE_RE.findall(text or ""):
        try:
            dates.append(datetime.strptime(f"{d} {mon} {y}", "%d %B %Y").date())
        except ValueError:
            continue
    if not dates:
        return None, None
    return min(dates), max(dates)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
