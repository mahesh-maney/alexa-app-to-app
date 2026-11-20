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
from pathlib import Path

import boto3
import pytest
import requests

# Load .env.e2e before anything else so all os.getenv() calls see the values.
# This avoids shell xargs mangling special characters (!, #, etc.) in passwords.
_env_file = Path(__file__).parent / ".env.e2e"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

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
    Obtain a Cognito access token with smarthome_server/read+write scopes.

    Preferred path (TEST_COGNITO_REFRESH_TOKEN set in .env.e2e):
        POST {cognito_domain}/oauth2/token with grant_type=refresh_token.
        The refresh token is obtained once via the auth-code flow (see
        tests/e2e/get_cognito_token.py) and stored in .env.e2e.

    Fallback (TEST_COGNITO_JWT set):
        Use the token directly.

    Why not USER_PASSWORD_AUTH?
        USER_PASSWORD_AUTH only grants scope 'aws.cognito.signin.user.admin'.
        The API methods require 'smarthome_server/read' or 'smarthome_server/write'.
        Only the hosted-UI authorization-code flow yields those resource-server scopes.
    """
    override = os.getenv("TEST_COGNITO_JWT")
    if override:
        return {"AccessToken": override, "RefreshToken": "", "IdToken": ""}

    refresh_token = os.getenv("TEST_COGNITO_REFRESH_TOKEN", "")
    if refresh_token:
        import requests as _req
        resp = _req.post(
            f"https://{cfg.cognito_domain}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cfg.cognito_client_id,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            pytest.fail(
                f"Refresh token exchange failed ({resp.status_code}): {resp.text}\n"
                "Re-run tests/e2e/get_cognito_token.py to get a fresh refresh token."
            )
        data = resp.json()
        return {
            "AccessToken": data["access_token"],
            "IdToken": data.get("id_token", ""),
            "RefreshToken": refresh_token,
        }

    pytest.fail(
        "TEST_COGNITO_REFRESH_TOKEN is not set in .env.e2e.\n"
        "Run: python3 tests/e2e/get_cognito_token.py  to obtain one.\n"
        "Then add TEST_COGNITO_REFRESH_TOKEN=<value> to tests/e2e/.env.e2e."
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
            f"{cfg.api_base}/api/v1/alexa/unlink",
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
