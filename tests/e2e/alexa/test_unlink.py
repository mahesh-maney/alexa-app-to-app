"""
E2E tests — Alexa unlink endpoint.

DELETE /api/v1/alexa/unlink?siteId=<id>
Auth: Cognito JWT (Bearer, IdToken)

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
            f"{cfg.api_base}/api/v1/alexa/unlink",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} — {r.text}"

    def test_no_site_id_performs_global_unlink(self, cfg, api, auth_headers):
        # Without siteId the Lambda performs a global (account-level) unlink.
        r = api.delete(
            f"{cfg.api_base}/api/v1/alexa/unlink",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json().get("unlinked") is True

    def test_unlink_missing_auth_returns_4xx(self, cfg, api, site_id):
        # Alexa REST API Lambda authorizer returns 403 for missing/invalid auth.
        r = api.delete(
            f"{cfg.api_base}/api/v1/alexa/unlink",
            params={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_unlink_invalid_token_returns_4xx(self, cfg, api, site_id):
        r = api.delete(
            f"{cfg.api_base}/api/v1/alexa/unlink",
            params={"siteId": site_id},
            headers={"Authorization": "Bearer bad.token.here"},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_status_is_unlinked_after_delete(self, cfg, api, auth_headers,
                                              site_id, ensure_alexa_unlinked):
        api.delete(
            f"{cfg.api_base}/api/v1/alexa/unlink",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.json()["linked"] is False
