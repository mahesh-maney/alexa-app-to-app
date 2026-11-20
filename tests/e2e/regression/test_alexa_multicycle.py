"""
Regression tests — Alexa link/unlink stability over multiple cycles.

Regression for: reverse-map race condition that caused alexaLinked=True to
disappear after 2+ link/unlink cycles (fixed 2026-11-16, commit on
mahesh_cleanup_token_lifecycle).

Root cause: 4 Lambdas (AcceptGrant, SkillAccountLinked, alexa_complete_app_to_app,
alexa_link_status) concurrently writing/deleting the same token item — alexaCustomerId
was lost between cycles making the GSI blind on subsequent SkillDisabled events.

This test drives 5 full link → unlink cycles via the app API and asserts that
linked/unlinked state is correct after each operation.

Requires:
  - Full browser-based Alexa link flow (TEST_AMAZON_EMAIL / TEST_AMAZON_PASSWORD)
    OR: pre-linked account where we can just test the unlink/status cycle
"""
from __future__ import annotations

import os
import pytest

from tests.e2e.conftest import poll_until
from tests.e2e.helpers import require_playwright


pytestmark = pytest.mark.regression

CYCLES = 5


@pytest.fixture(scope="module")
def can_run_browser_flow():
    return bool(os.getenv("TEST_AMAZON_EMAIL") and os.getenv("TEST_AMAZON_PASSWORD"))


class TestAlexaLinkUnlinkStability:

    @pytest.mark.smoke
    def test_unlink_link_status_cycle_is_stable(self, cfg, api, auth_headers,
                                                 site_id, ensure_alexa_unlinked):
        """
        Verifies that link-status correctly returns linked=false after each unlink
        and that the state remains consistent over 5 cycles of unlink→status checks.
        This is the lightweight version (no browser) — tests the status + unlink
        endpoints are stable and don't drift.
        """
        for cycle in range(1, CYCLES + 1):
            # Confirm unlinked
            r = api.get(
                f"{cfg.api_base}/api/v1/alexa/status",
                params={"siteId": site_id},
                headers=auth_headers,
                timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: status check failed"
            assert r.json()["linked"] is False, (
                f"Cycle {cycle}: expected linked=false, got {r.json()}"
            )

            # Unlink (idempotent — already unlinked)
            r = api.delete(
                f"{cfg.api_base}/api/v1/alexa/unlink",
                params={"siteId": site_id},
                headers=auth_headers,
                timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: unlink returned {r.status_code}"

            # Status still false
            r = api.get(
                f"{cfg.api_base}/api/v1/alexa/status",
                params={"siteId": site_id},
                headers=auth_headers,
                timeout=10,
            )
            assert r.json()["linked"] is False, (
                f"Cycle {cycle}: linked drifted to true after unlink"
            )

    @pytest.mark.browser
    def test_full_link_unlink_cycles_stay_stable(self, cfg, api, auth_headers, site_id,
                                                  can_run_browser_flow,
                                                  playwright_available,
                                                  ensure_alexa_unlinked):
        """
        Full link → unlink × 5. Requires browser + Amazon credentials.
        Each cycle: run full LWA+Cognito flow → assert linked=true → unlink → assert linked=false.
        """
        if not can_run_browser_flow:
            pytest.skip("TEST_AMAZON_EMAIL / TEST_AMAZON_PASSWORD not set")
        require_playwright(playwright_available)

        from tests.e2e.alexa.test_link_flow import (
            _lwa_login, _cognito_login,
        )
        from tests.e2e.config import test_user_email, test_user_password
        from playwright.sync_api import sync_playwright

        amazon_email    = os.getenv("TEST_AMAZON_EMAIL")
        amazon_password = os.getenv("TEST_AMAZON_PASSWORD")

        for cycle in range(1, CYCLES + 1):
            # ── Link ──────────────────────────────────────────────────────────
            r = api.post(
                f"{cfg.api_base}/api/v1/alexa/startAppToApp",
                headers=auth_headers, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: start failed"
            state   = r.json()["state"]
            redirect_uri = r.json()["redirectUri"]

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page    = browser.new_page()
                _lwa_login(page, redirect_uri, amazon_email, amazon_password)
                page.wait_for_timeout(4_000)
                browser.close()

            r = api.post(
                f"{cfg.api_base}/api/v1/alexa/completeAppToApp",
                headers=auth_headers, json={"state": state}, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: complete failed"
            cognito_url = r.json()["cognitoAuthUrl"]

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page    = browser.new_page()
                _cognito_login(page, cognito_url, test_user_email(), test_user_password())
                page.wait_for_timeout(4_000)
                browser.close()

            linked = poll_until(
                lambda: api.get(
                    f"{cfg.api_base}/api/v1/alexa/status",
                    params={"siteId": site_id}, headers=auth_headers, timeout=10,
                ).json().get("linked"),
                expected=True,
            )
            assert linked is True, f"Cycle {cycle}: not linked after full link flow"

            # ── Unlink ────────────────────────────────────────────────────────
            api.delete(
                f"{cfg.api_base}/api/v1/alexa/unlink",
                params={"siteId": site_id}, headers=auth_headers, timeout=10,
            )
            linked = poll_until(
                lambda: api.get(
                    f"{cfg.api_base}/api/v1/alexa/status",
                    params={"siteId": site_id}, headers=auth_headers, timeout=10,
                ).json().get("linked"),
                expected=False,
            )
            assert linked is False, f"Cycle {cycle}: still linked after unlink"
