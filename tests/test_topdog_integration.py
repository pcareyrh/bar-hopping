"""Live integration tests against topdogevents.com.au.

Credentialled tests require TOPDOG_USER and TOPDOG_PW (in the shell environment
or .env — loaded by tests/conftest.py). Skipped when credentials are absent.

Run all integration tests:
    set -a && . ./.env && set +a && .venv/bin/python -m pytest tests/test_topdog_integration.py -v

Run only the unauthenticated probe (no credentials needed):
    .venv/bin/python -m pytest tests/test_topdog_integration.py -v -k csrf
"""
import asyncio
import os

import httpx
import pytest

from app.scraper.auth import get_authed_cookies, sync_user_entries, SIGN_IN_URL, _extract_csrf_token
from app.scraper.trials import fetch_trial_detail, scrape_trial_detail, scrape_trial_details_batch


@pytest.fixture(scope="module")
def topdog_credentials() -> tuple[str, str]:
    user = os.getenv("TOPDOG_USER")
    password = os.getenv("TOPDOG_PW")
    if not user or not password:
        pytest.skip("TOPDOG_USER and TOPDOG_PW must be set for live TopDog integration tests")
    return user, password


def test_sign_in_page_exposes_csrf_without_browser():
    async def run():
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(SIGN_IN_URL)
        resp.raise_for_status()
        token = _extract_csrf_token(resp.text)
        assert len(token) > 10

    asyncio.run(run())


def test_get_authed_cookies_live(topdog_credentials):
    user, password = topdog_credentials

    async def run():
        cookies = await get_authed_cookies(user, password)
        assert cookies
        assert any(
            name for name in cookies
            if "session" in name.lower() or name.startswith("_")
        )

    asyncio.run(run())


def test_sync_user_entries_live(topdog_credentials):
    user, password = topdog_credentials

    async def run():
        trials = await sync_user_entries(user, password)
        assert isinstance(trials, list)
        for trial in trials:
            assert trial["external_id"]
            assert trial["name"]
            assert isinstance(trial["entries"], list)

    asyncio.run(run())


def test_fetch_trial_detail_live(topdog_credentials):
    user, password = topdog_credentials

    async def run():
        trials = await sync_user_entries(user, password)
        if not trials:
            pytest.skip("no upcoming trials on /entries to fetch detail for")

        external_id = trials[0]["external_id"]
        detail = await fetch_trial_detail(external_id)
        assert detail["external_id"] == external_id
        assert detail.get("name")

    asyncio.run(run())


def test_scrape_trial_detail_live_matches_fetch(topdog_credentials):
    user, password = topdog_credentials

    async def run():
        trials = await sync_user_entries(user, password)
        if not trials:
            pytest.skip("no upcoming trials on /entries")

        external_id = trials[0]["external_id"]
        fetched = await fetch_trial_detail(external_id)
        scraped = await scrape_trial_detail(external_id)
        assert scraped["external_id"] == fetched["external_id"]
        assert scraped["name"] == fetched["name"]

    asyncio.run(run())


def test_scrape_trial_details_batch_live(topdog_credentials):
    user, password = topdog_credentials

    async def run():
        trials = await sync_user_entries(user, password)
        if not trials:
            pytest.skip("no upcoming trials on /entries")

        ids = [t["external_id"] for t in trials[:2]]
        batch = await scrape_trial_details_batch(ids)
        assert len(batch) == len(ids)
        for item, external_id in zip(batch, ids):
            assert item["external_id"] == external_id
            assert item.get("name")

    asyncio.run(run())


def test_bad_credentials_rejected(topdog_credentials):
    user, _ = topdog_credentials

    async def run():
        with pytest.raises(ValueError, match="login failed"):
            await get_authed_cookies(user, "definitely-wrong-password-xyz")

    asyncio.run(run())
