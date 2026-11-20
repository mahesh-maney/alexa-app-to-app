"""
E2E tests — Google Home unlink endpoint.

DELETE /api/v1/voice/google-home/account-linking
Auth: Cognito JWT (Bearer)

Covers:
  - Unlink when already unlinked → 200, linked=false (idempotent)
  - 401 on missing / invalid token
  - Status confirms linked=false after unlink
"""
import pytest


pytestmark = pytest.mark.smoke


class TestGhUnlink:

    def test_unlink_when_not_linked_is_idempotent(self, cfg, api, auth_headers, ensure_gh_unlinked):
        r = api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} — {r.text}"
        assert r.json().get("linked") is False

    def test_unlink_missing_auth_returns_401(self, cfg, api):
        r = api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            timeout=10,
        )
        assert r.status_code == 401

    def test_unlink_invalid_token_returns_401(self, cfg, api):
        r = api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers={"Authorization": "Bearer bad.token.here"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_status_is_unlinked_after_delete(self, cfg, api, auth_headers, ensure_gh_unlinked):
        api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        r = api.get(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        assert r.json()["linked"] is False
