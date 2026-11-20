"""
E2E tests — Alexa full app-to-app account-linking flow.

Flow:
  1. App calls POST /api/alexa/app-to-app/start → gets lwaUrl + state
  2. Headless browser opens lwaUrl → Amazon LWA login → server callback fires
     → server exchanges LWA code, writes session, redirects to deep link
  3. App calls POST /api/alexa/app-to-app/complete → gets cognitoAuthUrl
  4. Headless browser opens cognitoAuthUrl → Cognito login → cognito/callback fires
     → server writes alexaLinked=True, redirects to deep link
  5. GET /api/alexa/link-status → linked=true

Requires:
  - playwright installed: pip install playwright && playwright install chromium
  - TEST_USER_EMAIL, TEST_USER_PASSWORD, TEST_SITE_ID
  - TEST_AMAZON_EMAIL, TEST_AMAZON_PASSWORD (Amazon account for LWA)
"""
from __future__ import annotations

import os
import re

import pytest

from tests.e2e.conftest import poll_until
from tests.e2e.config import test_user_email, test_user_password
from tests.e2e.helpers import require_playwright


pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def amazon_creds():
    email    = os.getenv("TEST_AMAZON_EMAIL", "")
    password = os.getenv("TEST_AMAZON_PASSWORD", "")
    if not email or not password:
        pytest.skip(
            "TEST_AMAZON_EMAIL / TEST_AMAZON_PASSWORD not set — "
            "skipping Alexa app-to-app link flow tests"
        )
    return email, password


def _lwa_login(page, lwa_url: str, amazon_email: str, amazon_password: str) -> None:
    """Navigate to Amazon LWA URL and log in with given credentials."""
    page.goto(lwa_url, wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_selector("#ap_email", timeout=15_000)
    page.fill("#ap_email", amazon_email)
    page.click("#continue")
    page.wait_for_selector("#ap_password", timeout=10_000)
    page.fill("#ap_password", amazon_password)
    page.click("#signInSubmit")


def _cognito_login(page, cognito_url: str, email: str, password: str) -> None:
    """Navigate to Cognito Hosted UI and log in."""
    page.goto(cognito_url, wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_selector("#signInFormUsername", timeout=15_000)
    page.fill("#signInFormUsername", email)
    page.fill("#signInFormPassword", password)
    page.click('input[name="signInSubmitButton"]')


class TestAlexaAppToAppStart:

    def test_start_returns_required_fields(self, cfg, api, auth_headers,
                                           ensure_alexa_unlinked):
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/start",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"start failed: {r.status_code} — {r.text}"
        body = r.json()
        assert "lwaUrl"        in body, "Missing 'lwaUrl'"
        assert "state"         in body, "Missing 'state'"
        assert "codeChallenge" in body, "Missing 'codeChallenge'"

    def test_start_lwa_url_points_to_amazon(self, cfg, api, auth_headers,
                                             ensure_alexa_unlinked):
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/start",
            headers=auth_headers,
            timeout=10,
        )
        lwa_url = r.json()["lwaUrl"]
        assert "amazon.com" in lwa_url or "amazonalexa.com" in lwa_url, (
            f"lwaUrl does not point to Amazon: {lwa_url}"
        )

    def test_start_missing_auth_returns_401(self, cfg, api):
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/start",
            timeout=10,
        )
        assert r.status_code == 401

    def test_complete_with_invalid_state_returns_error(self, cfg, api,
                                                        auth_headers,
                                                        ensure_alexa_unlinked):
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/complete",
            headers=auth_headers,
            json={"state": "00000000-0000-0000-0000-000000000000"},
            timeout=10,
        )
        assert r.status_code in (400, 404, 410), (
            f"Expected error on invalid state, got {r.status_code}: {r.text}"
        )


class TestAlexaFullLinkFlow:

    @pytest.mark.browser
    def test_full_app_to_app_link_flow(self, cfg, api, auth_headers, site_id,
                                        amazon_creds, playwright_available,
                                        ensure_alexa_unlinked):
        require_playwright(playwright_available)
        from playwright.sync_api import sync_playwright

        amazon_email, amazon_password = amazon_creds

        # ── Step 1: Start ──────────────────────────────────────────────────────
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/start",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        state   = r.json()["state"]
        lwa_url = r.json()["lwaUrl"]

        # ── Step 2: LWA login in headless browser ──────────────────────────────
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()

            _lwa_login(page, lwa_url, amazon_email, amazon_password)
            # Server callback (/alexa/callback) fires and redirects to deep link
            page.wait_for_timeout(4_000)
            browser.close()

        # ── Step 3: Complete ───────────────────────────────────────────────────
        r = api.post(
            f"{cfg.api_base}/api/alexa/app-to-app/complete",
            headers=auth_headers,
            json={"state": state},
            timeout=10,
        )
        assert r.status_code == 200, f"complete failed: {r.status_code} — {r.text}"
        cognito_auth_url = r.json().get("cognitoAuthUrl")
        assert cognito_auth_url, "complete did not return cognitoAuthUrl"

        # ── Step 4: Cognito login in headless browser ──────────────────────────
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_page()

            _cognito_login(page, cognito_auth_url, test_user_email(), test_user_password())
            # Server cognito/callback fires and writes alexaLinked=True
            page.wait_for_timeout(4_000)
            browser.close()

        # ── Step 5: Poll link-status ───────────────────────────────────────────
        linked = poll_until(
            lambda: api.get(
                f"{cfg.api_base}/api/alexa/link-status",
                params={"siteId": site_id},
                headers=auth_headers,
                timeout=10,
            ).json().get("linked"),
            expected=True,
            retries=10,
            interval=2,
        )
        assert linked is True, "link-status did not return linked=true after full flow"
