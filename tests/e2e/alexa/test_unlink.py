"""
E2E tests — Alexa unlink endpoint.

DELETE /api/alexa/account-linking?siteId=<id>
Auth: Cognito JWT (Bearer)

Covers:
  - Unlink when already unlinked → 200 (idempotent)
  - 400 on missing siteId
  - 401 on missing / invalid token
  - Status confirms linked=false after unlink
"""
import pytest


pytestmark = pytest.mark.smoke


class TestAlexaUnlink:

    def test_unlink_when_not_linked_is_idempotent(self, cfg, api, auth_headers,
                                                   site_id, ensure_alexa_unlinked):
        r = api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} — {r.text}"

    def test_unlink_missing_site_id_returns_400(self, cfg, api, auth_headers):
        r = api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400
        assert r.json().get("error") == "MISSING_SITE_ID"

    def test_unlink_missing_auth_returns_401(self, cfg, api, site_id):
        r = api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            params={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code == 401

    def test_unlink_invalid_token_returns_401(self, cfg, api, site_id):
        r = api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            params={"siteId": site_id},
            headers={"Authorization": "Bearer bad.token.here"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_status_is_unlinked_after_delete(self, cfg, api, auth_headers,
                                              site_id, ensure_alexa_unlinked):
        api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        r = api.get(
            f"{cfg.api_base}/api/alexa/link-status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.json()["linked"] is False
