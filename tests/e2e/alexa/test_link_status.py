"""
E2E tests — Alexa link status endpoint.

GET /api/alexa/link-status?siteId=<id>
Auth: Cognito JWT (Bearer)

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
            f"{cfg.api_base}/api/alexa/link-status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, f"Unexpected: {r.status_code} — {r.text}"
        assert r.json()["linked"] is False

    def test_response_has_linked_key(self, cfg, api, auth_headers,
                                     site_id, ensure_alexa_unlinked):
        r = api.get(
            f"{cfg.api_base}/api/alexa/link-status",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
        assert "linked" in r.json()

    def test_missing_site_id_returns_400(self, cfg, api, auth_headers):
        r = api.get(
            f"{cfg.api_base}/api/alexa/link-status",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400
        assert r.json().get("error") == "MISSING_SITE_ID"

    def test_missing_auth_returns_401(self, cfg, api, site_id):
        r = api.get(
            f"{cfg.api_base}/api/alexa/link-status",
            params={"siteId": site_id},
            timeout=10,
        )
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, cfg, api, site_id):
        r = api.get(
            f"{cfg.api_base}/api/alexa/link-status",
            params={"siteId": site_id},
            headers={"Authorization": "Bearer bad.token.value"},
            timeout=10,
        )
        assert r.status_code == 401
