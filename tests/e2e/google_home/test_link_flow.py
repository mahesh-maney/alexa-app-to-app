"""
E2E tests — Google Home full OAuth account-linking flow.

Flow:
  1. App calls POST /deep-link/start → gets webFallbackUrl + state
  2. Headless browser opens webFallbackUrl
     → redirected to Cognito Hosted UI → test user logs in
     → redirected to /cognito/callback → consent page rendered
     → user clicks Allow → redirected to Google's redirect URI with ?code=
  3. Test intercepts the auth code from the redirect URL
  4. POST /token with the auth code → access_token + refresh_token returned
  5. POST /deep-link/complete with state → linked=true confirmed
  6. GET /account-linking status → linked=true

Requires:
  - playwright installed: pip install playwright && playwright install chromium
  - TEST_USER_EMAIL, TEST_USER_PASSWORD, TEST_SITE_ID set
  - TEST_GH_CLIENT_SECRET set
"""
from __future__ import annotations

import os
import re

import pytest

from tests.e2e.conftest import poll_until
from tests.e2e.helpers import require_playwright


@pytest.fixture(scope="module")
def gh_client_secret():
    secret = os.getenv("TEST_GH_CLIENT_SECRET", "")
    if not secret:
        pytest.skip("TEST_GH_CLIENT_SECRET not set")
    return secret


def _run_gh_oauth_flow(cfg, web_fallback_url: str, test_email: str, test_password: str) -> str:
    """
    Drive the GH OAuth consent flow in a headless browser.
    Returns the auth code extracted from the final redirect to Google.
    """
    from playwright.sync_api import sync_playwright

    auth_code = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()

        # Capture the final redirect URL before the browser tries to load it
        def on_request(request):
            nonlocal auth_code
            if "oauth-redirect.googleusercontent.com" in request.url:
                m = re.search(r"[?&]code=([^&]+)", request.url)
                if m:
                    auth_code = m.group(1)

        page.on("request", on_request)

        # Step 2a: Open the OAuth authorize URL
        page.goto(web_fallback_url, wait_until="domcontentloaded", timeout=20_000)

        # Step 2b: Cognito Hosted UI login
        page.wait_for_selector("#signInFormUsername", timeout=15_000)
        page.fill("#signInFormUsername", test_email)
        page.fill("#signInFormPassword", test_password)
        page.click('input[name="signInSubmitButton"]')

        # Step 2c: Nucleus consent page — click Allow
        page.wait_for_selector("button[value='approve']", timeout=15_000)
        page.click("button[value='approve']")

        # Wait briefly for the redirect to fire
        page.wait_for_timeout(3_000)
        browser.close()

    if not auth_code:
        pytest.fail(
            "OAuth flow completed but no auth code was captured. "
            "Check that the consent page submits correctly and that "
            "the redirect URI is registered in Google Actions Console."
        )

    return auth_code


class TestGhFullLinkFlow:

    @pytest.mark.smoke
    def test_deep_link_start_returns_required_fields(self, cfg, api, auth_headers,
                                                      site_id, ensure_gh_unlinked):
        r = api.post(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/start",
            headers=auth_headers,
            json={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code == 200, f"start failed: {r.status_code} — {r.text}"
        body = r.json()
        assert "state"           in body, "Missing 'state'"
        assert "agentId"         in body, "Missing 'agentId'"
        assert "webFallbackUrl"  in body, "Missing 'webFallbackUrl'"
        assert "homeAppDeepLink" in body, "Missing 'homeAppDeepLink'"

    @pytest.mark.smoke
    def test_deep_link_start_missing_auth_returns_4xx(self, cfg, api):
        r = api.post(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/start",
            json={},
            timeout=10,
        )
        assert r.status_code in (401, 403), (
            f"Expected 401/403 for missing auth, got {r.status_code}"
        )

    @pytest.mark.browser
    def test_full_oauth_link_flow(self, cfg, api, auth_headers, site_id,
                                  gh_client_secret, playwright_available,
                                  ensure_gh_unlinked):
        require_playwright(playwright_available)
        from tests.e2e.config import test_user_email, test_user_password

        # ── Step 1: Start ──────────────────────────────────────────────────────
        r = api.post(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/start",
            headers=auth_headers,
            json={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code == 200
        start_body      = r.json()
        state           = start_body["state"]
        web_fallback    = start_body["webFallbackUrl"]

        # ── Step 2: Run consent flow in headless browser ───────────────────────
        auth_code = _run_gh_oauth_flow(
            cfg, web_fallback, test_user_email(), test_user_password()
        )

        # ── Step 3: Exchange auth code at /token ───────────────────────────────
        r = api.post(
            f"{cfg.api_base}/google-home/oauth/token",
            data={
                "grant_type":   "authorization_code",
                "code":         auth_code,
                "client_id":    cfg.gh_client_id,
                "client_secret": gh_client_secret,
                "redirect_uri": cfg.gh_redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        assert r.status_code == 200, f"/token exchange failed: {r.status_code} — {r.text}"
        tokens = r.json()
        assert "access_token"  in tokens, "No access_token in /token response"
        assert "refresh_token" in tokens, "No refresh_token in /token response"
        assert tokens.get("expires_in", 0) > 0

        # ── Step 4: deep-link/complete confirms linking ────────────────────────
        r = api.post(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/complete",
            headers=auth_headers,
            json={"state": state},
            timeout=10,
        )
        assert r.status_code == 200, f"complete failed: {r.status_code} — {r.text}"
        assert r.json()["linked"] is True

        # ── Step 5: Status confirms linked ────────────────────────────────────
        linked = poll_until(
            lambda: api.get(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            ).json().get("linked"),
            expected=True,
        )
        assert linked is True, "link-status did not return linked=true after full flow"

    @pytest.mark.smoke
    def test_deep_link_complete_with_invalid_state_returns_error(self, cfg, api,
                                                                  auth_headers,
                                                                  ensure_gh_unlinked):
        r = api.post(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/complete",
            headers=auth_headers,
            json={"state": "00000000-0000-0000-0000-000000000000"},
            timeout=10,
        )
        assert r.status_code in (400, 404, 410), (
            f"Expected error on invalid state, got {r.status_code}: {r.text}"
        )
