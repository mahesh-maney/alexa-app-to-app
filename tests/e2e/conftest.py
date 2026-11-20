"""
tests/e2e/conftest.py — Shared fixtures for all E2E tests.

Fixtures:
    cfg             EnvConfig for the selected TEST_ENV
    api             requests.Session (shared, no auth)
    cognito_tokens  Real Cognito tokens obtained via USER_PASSWORD_AUTH (session scope)
    auth_headers    {"Authorization": "Bearer <access_token>"} (session scope)
    site_id         TEST_SITE_ID value
    device_id       TEST_DEVICE_ID value (skips test if unset)
    ensure_gh_unlinked    Function-scope: unlinks GH before + after each test
    ensure_alexa_unlinked Function-scope: unlinks Alexa before + after each test

NOTE — Cognito USER_PASSWORD_AUTH:
    The Cognito app client must have ALLOW_USER_PASSWORD_AUTH in its auth flows.
    If the test run fails with "USER_PASSWORD_AUTH not supported", enable it in
    AWS Console → Cognito → User Pools → App Clients → Edit → Auth Flows.
    Alternatively, set TEST_COGNITO_JWT to a pre-obtained access token.
"""
from __future__ import annotations

import os
import time

import boto3
import pytest
import requests

from tests.e2e.config import get_config, test_user_email, test_user_password, test_site_id


# ── Core fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def cfg():
    return get_config()


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def cognito_tokens(cfg):
    """
    Obtain a real Cognito access + refresh token for the test user.
    Falls back to TEST_COGNITO_JWT env var if USER_PASSWORD_AUTH is not enabled
    on the app client.
    """
    override = os.getenv("TEST_COGNITO_JWT")
    if override:
        return {"AccessToken": override, "RefreshToken": "", "IdToken": ""}

    client = boto3.client("cognito-idp", region_name=cfg.cognito_region)
    try:
        resp = client.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": test_user_email(),
                "PASSWORD": test_user_password(),
            },
            ClientId=cfg.cognito_client_id,
        )
        return resp["AuthenticationResult"]
    except client.exceptions.NotAuthorizedException as exc:
        pytest.fail(
            f"Cognito login failed for {test_user_email()!r}: {exc}\n"
            "Check TEST_USER_EMAIL / TEST_USER_PASSWORD are correct."
        )
    except Exception as exc:
        pytest.fail(
            f"Could not obtain Cognito token: {exc}\n"
            "If USER_PASSWORD_AUTH is not enabled on the app client, set "
            "TEST_COGNITO_JWT=<your_access_token> and re-run."
        )


@pytest.fixture(scope="session")
def auth_headers(cognito_tokens):
    return {"Authorization": f"Bearer {cognito_tokens['AccessToken']}"}


@pytest.fixture(scope="session")
def site_id():
    return test_site_id()


@pytest.fixture(scope="session")
def device_id():
    val = os.getenv("TEST_DEVICE_ID", "")
    if not val:
        pytest.skip("TEST_DEVICE_ID not set — skipping fulfillment test")
    return val


# ── State cleanup fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def ensure_gh_unlinked(cfg, api, auth_headers):
    """Unlink GH before the test, then again after. Guarantees a clean slate."""
    _gh_unlink(cfg, api, auth_headers)
    yield
    _gh_unlink(cfg, api, auth_headers)


@pytest.fixture()
def ensure_alexa_unlinked(cfg, api, auth_headers, site_id):
    """Unlink Alexa before the test, then again after. Guarantees a clean slate."""
    _alexa_unlink(cfg, api, auth_headers, site_id)
    yield
    _alexa_unlink(cfg, api, auth_headers, site_id)


def _gh_unlink(cfg, api, auth_headers):
    try:
        api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
    except Exception:
        pass  # ignore — may already be unlinked


def _alexa_unlink(cfg, api, auth_headers, site_id):
    try:
        api.delete(
            f"{cfg.api_base}/api/alexa/account-linking",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
    except Exception:
        pass  # ignore — may already be unlinked


# ── Browser (Playwright) availability guard ────────────────────────────────────

@pytest.fixture(scope="session")
def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# ── Helpers shared across test modules ────────────────────────────────────────

def poll_until(fn, expected=True, retries=10, interval=2):
    """
    Call fn() up to `retries` times, waiting `interval` seconds between attempts.
    Returns the last result. Useful for waiting on async state changes.
    """
    result = None
    for _ in range(retries):
        result = fn()
        if result == expected:
            return result
        time.sleep(interval)
    return result
