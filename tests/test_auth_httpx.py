"""Unit tests for httpx-based TopDog authentication (no Playwright)."""
import asyncio
from unittest.mock import patch

import httpx
import pytest

from app.scraper import auth

SIGN_IN_HTML = """
<html><head>
<meta name="csrf-token" content="test-csrf-token">
</head><body>
<form action="/users/sign_in" method="post">
<input type="hidden" name="authenticity_token" value="test-csrf-token">
</form>
</body></html>
"""

ENTRIES_HTML = """
<div class="tab-pane" id="t9999">
  <strong>Test Trial</strong>
  <small class="text-muted">Saturday, 1 January 2099</small>
  <table><tbody>
    <tr><td>101</td><td>Rex</td><td>Novice Agility</td><td>400</td><td></td><td></td></tr>
  </tbody></table>
</div>
"""


class _FakeResponse:
    def __init__(self, text: str, url: str, status_code: int = 200):
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", str(self.url))
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.cookies = httpx.Cookies()
        self._get_handlers: dict[str, _FakeResponse] = {}
        self._post_handlers: dict[str, _FakeResponse] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def on_get(self, url_suffix: str, response: _FakeResponse) -> None:
        self._get_handlers[url_suffix] = response

    def on_post(self, url_suffix: str, response: _FakeResponse) -> None:
        self._post_handlers[url_suffix] = response

    async def get(self, url: str, **kwargs):
        for suffix, response in self._get_handlers.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected GET {url}")

    async def post(self, url: str, **kwargs):
        for suffix, response in self._post_handlers.items():
            if url.endswith(suffix):
                self.cookies.set("_topdog_session", "abc123", domain="www.topdogevents.com.au")
                return response
        raise AssertionError(f"unexpected POST {url}")


def test_extract_csrf_token_from_meta():
    assert auth._extract_csrf_token(SIGN_IN_HTML) == "test-csrf-token"


def test_extract_csrf_token_from_hidden_input():
    html = '<input name="authenticity_token" value="hidden-token">'
    assert auth._extract_csrf_token(html) == "hidden-token"


def test_extract_csrf_token_missing_raises():
    with pytest.raises(ValueError, match="CSRF token not found"):
        auth._extract_csrf_token("<html></html>")


def test_login_success():
    async def run():
        client = _FakeClient()
        client.on_get("/users/sign_in", _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL))
        client.on_post(
            "/users/sign_in",
            _FakeResponse("<html>ok</html>", f"{auth.BASE_URL}/entries"),
        )
        await auth._login(client, "user@example.com", "secret")
        assert client.cookies.get("_topdog_session") == "abc123"

    asyncio.run(run())


def test_login_failure_still_on_sign_in():
    async def run():
        client = _FakeClient()
        client.on_get("/users/sign_in", _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL))
        client.on_post(
            "/users/sign_in",
            _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL),
        )
        with pytest.raises(ValueError, match="login failed"):
            await auth._login(client, "bad@example.com", "wrong")

    asyncio.run(run())


def test_get_authed_cookies_returns_cookie_dict():
    async def run():
        fake = _FakeClient()
        fake.on_get("/users/sign_in", _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL))
        fake.on_post(
            "/users/sign_in",
            _FakeResponse("<html>ok</html>", f"{auth.BASE_URL}/"),
        )

        with patch("app.scraper.auth.httpx.AsyncClient", return_value=fake):
            cookies = await auth.get_authed_cookies("user@example.com", "secret")

        assert cookies["_topdog_session"] == "abc123"

    asyncio.run(run())


def test_sync_user_entries_parses_trials():
    async def run():
        fake = _FakeClient()
        fake.on_get("/users/sign_in", _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL))
        fake.on_post(
            "/users/sign_in",
            _FakeResponse("<html>ok</html>", f"{auth.BASE_URL}/"),
        )
        fake.on_get("/entries", _FakeResponse(ENTRIES_HTML, auth.ENTRIES_URL))

        progress_calls: list[tuple[int, int]] = []

        def on_progress(current, total):
            progress_calls.append((current, total))

        with patch("app.scraper.auth.httpx.AsyncClient", return_value=fake):
            trials = await auth.sync_user_entries(
                "user@example.com",
                "secret",
                on_progress=on_progress,
            )

        assert progress_calls == [(0, 1)]
        assert len(trials) == 1
        assert trials[0]["external_id"] == "9999"
        assert trials[0]["name"] == "Test Trial"
        assert trials[0]["entries"][0]["dog_name"] == "Rex"

    asyncio.run(run())


def test_sync_user_entries_bad_credentials_propagates():
    async def run():
        fake = _FakeClient()
        fake.on_get("/users/sign_in", _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL))
        fake.on_post(
            "/users/sign_in",
            _FakeResponse(SIGN_IN_HTML, auth.SIGN_IN_URL),
        )

        with patch("app.scraper.auth.httpx.AsyncClient", return_value=fake):
            with pytest.raises(ValueError, match="login failed"):
                await auth.sync_user_entries("bad@example.com", "wrong")

    asyncio.run(run())
