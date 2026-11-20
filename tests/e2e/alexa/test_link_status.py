"""
E2E tests — Alexa link status endpoint.

GET /api/v1/alexa/status?siteId=<id>
Auth: Cognito JWT (Bearer, IdToken)

Covers:
  - Returns linked=false when not linked
  - 400 on missing siteId
  - 401 on missing / invalid token
"""
import pytest


pytestmark = pytest.mark.smoke


class TestAlexaLinkStatus:

    def test_unlinked_state_returns_false(self, cfg, api, auth_headers,
                                          site_id, ensure_alexa_unlinked):
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} — {r.text}"
        assert r.json()["linked"] is False

    def test_response_has_linked_key(self, cfg, api, auth_headers,
                                     site_id, ensure_alexa_unlinked):
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert "linked" in r.json()

    def test_no_site_id_returns_global_status(self, cfg, api, auth_headers):
        # Without siteId the Lambda performs a global (account-level) linked check.
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert "linked" in r.json()

    def test_missing_auth_returns_4xx(self, cfg, api, site_id):
        # Alexa endpoints sit behind a REST API Lambda authorizer → returns 403.
        # GH endpoints use an HTTP API JWT authorizer → returns 401.
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            params={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"

    def test_invalid_token_returns_4xx(self, cfg, api, site_id):
        r = api.get(
            f"{cfg.api_base}/api/v1/alexa/status",
            params={"siteId": site_id},
            headers={"Authorization": "Bearer bad.token.value"},
            timeout=10,
        )
        assert r.status_code in (401, 403), f"Expected 401/403, got {r.status_code}"
