"""
E2E tests — Google Home OAuth token endpoint.

POST /api/google-home/oauth/token
Auth: Google client_id + client_secret (body params)

Covers:
  - invalid_client on wrong client_id
  - invalid_client on wrong client_secret
  - invalid_grant on unknown / already-used auth code
  - unsupported_grant_type on unknown grant
  - invalid_request on missing code (authorization_code grant)
  - invalid_request on missing refresh_token (refresh_token grant)
  - refresh_token grant returns new access_token (requires TEST_GH_REFRESH_TOKEN)

NOTE: The happy-path authorization_code exchange is covered in test_link_flow.py
      (which runs the full OAuth flow and exchanges the code there).
"""
from __future__ import annotations

import os
import pytest
import requests

from tests.e2e.config import get_config, gh_client_secret


pytestmark = pytest.mark.smoke


@pytest.fixture(scope="module")
def token_url(cfg):
    return f"{cfg.api_base}/google-home/oauth/token"


@pytest.fixture(scope="module")
def valid_client_params(cfg):
    secret = os.getenv("TEST_GH_CLIENT_SECRET", "")
    if not secret:
        pytest.skip("TEST_GH_CLIENT_SECRET not set — skipping client-auth token tests")
    return {
        "client_id":     cfg.gh_client_id,
        "client_secret": secret,
    }


class TestTokenClientAuth:

    def test_wrong_client_id_returns_401(self, token_url, valid_client_params):
        r = requests.post(token_url, data={
            **valid_client_params,
            "client_id":    "wrong-client-id",
            "grant_type":   "authorization_code",
            "code":         "fake-code",
            "redirect_uri": "https://oauth-redirect.googleusercontent.com/r/test",
        }, timeout=10)
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"

    def test_wrong_client_secret_returns_401(self, token_url, valid_client_params, cfg):
        r = requests.post(token_url, data={
            **valid_client_params,
            "client_secret": "wrong-secret",
            "grant_type":    "authorization_code",
            "code":          "fake-code",
            "redirect_uri":  cfg.gh_redirect_uri,
        }, timeout=10)
        assert r.status_code == 401
        assert r.json()["error"] == "invalid_client"


class TestTokenGrantValidation:

    def test_unknown_grant_type_returns_400(self, token_url, valid_client_params, cfg):
        r = requests.post(token_url, data={
            **valid_client_params,
            "grant_type":   "implicit",
            "redirect_uri": cfg.gh_redirect_uri,
        }, timeout=10)
        assert r.status_code == 400
        assert r.json()["error"] == "unsupported_grant_type"

    def test_missing_code_on_auth_code_grant_returns_400(self, token_url, valid_client_params, cfg):
        r = requests.post(token_url, data={
            **valid_client_params,
            "grant_type":   "authorization_code",
            "redirect_uri": cfg.gh_redirect_uri,
            # code intentionally omitted
        }, timeout=10)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_missing_refresh_token_returns_400(self, token_url, valid_client_params):
        r = requests.post(token_url, data={
            **valid_client_params,
            "grant_type": "refresh_token",
            # refresh_token intentionally omitted
        }, timeout=10)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_request"

    def test_unknown_auth_code_returns_400(self, token_url, valid_client_params, cfg):
        r = requests.post(token_url, data={
            **valid_client_params,
            "grant_type":   "authorization_code",
            "code":         "this-code-does-not-exist-in-ddb",
            "redirect_uri": cfg.gh_redirect_uri,
        }, timeout=10)
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_grant"


class TestTokenRefresh:

    def test_refresh_token_returns_new_access_token(self, token_url, valid_client_params):
        refresh_token = os.getenv("TEST_GH_REFRESH_TOKEN", "")
        if not refresh_token:
            pytest.skip("TEST_GH_REFRESH_TOKEN not set — run full link flow first")

        r = requests.post(token_url, data={
            **valid_client_params,
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=15)
        assert r.status_code == 200, f"Refresh failed: {r.text}"
        body = r.json()
        assert "access_token" in body
        assert body.get("token_type", "").lower() == "bearer"
        assert body.get("expires_in", 0) > 0
