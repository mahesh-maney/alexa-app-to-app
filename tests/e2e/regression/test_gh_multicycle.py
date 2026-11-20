"""
Regression tests — Google Home link/unlink stability over multiple cycles.

Regression for: multi-device overwrite bug where a second user linking GH
would overwrite the first user's token record (fixed in commit 192f612).
Cognito JWTs are now used directly end-to-end; no custom token table.

This test drives 5 full link → unlink cycles and asserts state is correct
after every step.

Lightweight version: unlink → status check × 5 (no browser needed).
Full version: complete OAuth flow × 5 (requires browser + GH client secret).
"""
from __future__ import annotations

import os
import pytest

from tests.e2e.conftest import poll_until
from tests.e2e.helpers import require_playwright


pytestmark = pytest.mark.regression

CYCLES = 5


class TestGhLinkUnlinkStability:

    def test_unlink_status_cycle_is_stable(self, cfg, api, auth_headers,
                                            ensure_gh_unlinked):
        """
        Verifies that unlink is idempotent and status is consistently false
        across 5 cycles. No browser required.
        """
        for cycle in range(1, CYCLES + 1):
            # Confirm unlinked
            r = api.get(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: status check failed"
            assert r.json()["linked"] is False, (
                f"Cycle {cycle}: expected linked=false, got {r.json()}"
            )

            # Unlink (idempotent)
            r = api.delete(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: unlink returned {r.status_code}"

            # Status still false
            r = api.get(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            )
            assert r.json()["linked"] is False, (
                f"Cycle {cycle}: linked drifted to true after unlink"
            )

    @pytest.mark.browser
    def test_full_link_unlink_cycles_stay_stable(self, cfg, api, auth_headers,
                                                  site_id, playwright_available,
                                                  ensure_gh_unlinked):
        """
        Full OAuth link → unlink × 5. Requires browser + GH client secret.
        """
        client_secret = os.getenv("TEST_GH_CLIENT_SECRET", "")
        if not client_secret:
            pytest.skip("TEST_GH_CLIENT_SECRET not set")
        require_playwright(playwright_available)

        from tests.e2e.google_home.test_link_flow import _run_gh_oauth_flow
        from tests.e2e.config import test_user_email, test_user_password

        for cycle in range(1, CYCLES + 1):
            # ── Link ──────────────────────────────────────────────────────────
            r = api.post(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/start",
                headers=auth_headers, json={"siteId": site_id}, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: start failed"
            state        = r.json()["state"]
            web_fallback = r.json()["webFallbackUrl"]

            auth_code = _run_gh_oauth_flow(
                cfg, web_fallback, test_user_email(), test_user_password()
            )

            # Exchange auth code
            r = api.post(
                f"{cfg.api_base}/api/google-home/oauth/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          auth_code,
                    "client_id":     cfg.gh_client_id,
                    "client_secret": client_secret,
                    "redirect_uri":  cfg.gh_redirect_uri,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            assert r.status_code == 200, f"Cycle {cycle}: /token failed: {r.text}"

            # Complete
            r = api.post(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking/deep-link/complete",
                headers=auth_headers, json={"state": state}, timeout=10,
            )
            assert r.status_code == 200, f"Cycle {cycle}: complete failed"
            assert r.json()["linked"] is True

            linked = poll_until(
                lambda: api.get(
                    f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                    headers=auth_headers, timeout=10,
                ).json().get("linked"),
                expected=True,
            )
            assert linked is True, f"Cycle {cycle}: not linked after OAuth flow"

            # ── Unlink ────────────────────────────────────────────────────────
            api.delete(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            )
            linked = poll_until(
                lambda: api.get(
                    f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                    headers=auth_headers, timeout=10,
                ).json().get("linked"),
                expected=False,
            )
            assert linked is False, f"Cycle {cycle}: still linked after unlink"
