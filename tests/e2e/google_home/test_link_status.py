"""
E2E tests — Google Home link status endpoint.

GET /api/v1/voice/google-home/account-linking
Auth: Cognito JWT (Bearer)

Covers:
  - Returns linked=false when not linked
  - Returns linked=true after linking (tested via test_link_flow)
  - 401 on missing / invalid token
"""
import pytest


pytestmark = pytest.mark.smoke


class TestGhLinkStatus:

    def test_unlinked_state_returns_false(self, cfg, api, auth_headers, ensure_gh_unlinked):
        r = api.get(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected status: {r.status_code} — {r.text}"
        assert r.json()["linked"] is False

    def test_response_has_linked_key(self, cfg, api, auth_headers, ensure_gh_unlinked):
        r = api.get(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        assert "linked" in r.json()

    def test_missing_auth_header_returns_401(self, cfg, api):
        r = api.get(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            timeout=10,
        )
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, cfg, api):
        r = api.get(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers={"Authorization": "Bearer this.is.invalid"},
            timeout=10,
        )
        assert r.status_code == 401
