"""Unit tests for httpx-based trial detail fetching (no Playwright)."""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.scraper import trials

TRIAL_HTML = """
<html><body>
<h1>Spring Agility</h1>
<div class="page-header"><h4>12th March 2026</h4></div>
<a href="/trials/42/schedule/get">Schedule</a>
<a href="/trials/42/catalogue/get">Catalogue</a>
<div id="location"><h6>Test Grounds</h6><p>1 Main St</p></div>
<p>Judging starts at 8:00 AM</p>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.com")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self._responses: dict[str, _FakeResponse] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def on_get(self, external_id: str, response: _FakeResponse) -> None:
        self._responses[external_id] = response

    async def get(self, url: str, **kwargs):
        for external_id, response in self._responses.items():
            if f"/trials/{external_id}" in url:
                return response
        raise AssertionError(f"unexpected GET {url}")


def test_fetch_trial_detail_parses_html():
    async def run():
        fake = _FakeClient()
        fake.on_get("42", _FakeResponse(TRIAL_HTML))

        with patch("app.scraper.trials.httpx.AsyncClient", return_value=fake):
            detail = await trials.fetch_trial_detail("42")

        assert detail["external_id"] == "42"
        assert detail["name"] == "Spring Agility"
        assert detail["venue"] == "Test Grounds — 1 Main St"
        assert detail["schedule_doc_url"].endswith("/trials/42/schedule/get")
        assert detail["catalogue_doc_url"].endswith("/trials/42/catalogue/get")

    asyncio.run(run())


def test_scrape_trial_detail_delegates_to_fetch():
    async def run():
        with patch(
            "app.scraper.trials.fetch_trial_detail",
            new_callable=AsyncMock,
            return_value={"external_id": "1"},
        ) as mock:
            result = await trials.scrape_trial_detail("1")
        assert result == {"external_id": "1"}
        mock.assert_awaited_once_with("1")

    asyncio.run(run())


def test_scrape_trial_details_batch_empty():
    async def run():
        assert await trials.scrape_trial_details_batch([]) == []

    asyncio.run(run())


def test_scrape_trial_details_batch_reuses_client():
    async def run():
        fake = _FakeClient()
        fake.on_get("10", _FakeResponse(TRIAL_HTML.replace("42", "10").replace("Spring Agility", "Trial A")))
        fake.on_get("11", _FakeResponse("<html><body><h1>Trial B</h1></body></html>"))

        progress: list[tuple[int, int]] = []

        with patch("app.scraper.trials.httpx.AsyncClient", return_value=fake):
            results = await trials.scrape_trial_details_batch(
                ["10", "11"],
                on_progress=lambda c, t: progress.append((c, t)),
            )

        assert progress == [(1, 2), (2, 2)]
        assert len(results) == 2
        assert results[0]["name"] == "Trial A"
        assert results[1]["name"] == "Trial B"

    asyncio.run(run())


def test_scrape_trial_details_batch_tolerates_fetch_errors():
    async def run():
        fake = _FakeClient()
        fake.on_get("99", _FakeResponse("error", status_code=500))

        with patch("app.scraper.trials.httpx.AsyncClient", return_value=fake):
            results = await trials.scrape_trial_details_batch(["99"])

        assert results == [{"external_id": "99"}]

    asyncio.run(run())
