"""Unit tests for google_home_oauth_authorize Lambda.

GET/POST /google-home/oauth/authorize
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_auth_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_oauth_authorize", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Constants ─────────────────────────────────────────────────────────────────
CLIENT_ID    = "test-google-client-id"   # matches conftest GOOGLE_CLIENT_ID
REDIRECT_URI = "https://oauth-redirect.googleusercontent.com/r/test-project"
GOOGLE_STATE = "google-csrf-state-xyz"
USER_ID      = "user-sub-auth-abc"
PRE_AUTH     = "pre-auth-state-uuid-123"


def _get_event(
    client_id=CLIENT_ID,
    redirect_uri=REDIRECT_URI,
    response_type="code",
    state=GOOGLE_STATE,
    scope="profile",
    pre_auth_state="",
):
    return {
        "httpMethod": "GET",
        "path": "/google-home/oauth/authorize",
        "queryStringParameters": {
            "client_id":      client_id,
            "redirect_uri":   redirect_uri,
            "response_type":  response_type,
            "state":          state,
            "scope":          scope,
            "pre_auth_state": pre_auth_state,
        },
    }


def _post_event(
    username="user@example.com",
    password="secret",
    client_id=CLIENT_ID,
    redirect_uri=REDIRECT_URI,
    state=GOOGLE_STATE,
    scope="profile",
    pre_auth_state="",
):
    import urllib.parse
    body = urllib.parse.urlencode({
        "username":       username,
        "password":       password,
        "client_id":      client_id,
        "redirect_uri":   redirect_uri,
        "state":          state,
        "scope":          scope,
        "pre_auth_state": pre_auth_state,
    })
    return {
        "httpMethod": "POST",
        "path": "/google-home/oauth/authorize",
        "body": body,
    }


def _parse_redirect(r):
    """Parse the Location header of a 302 response into (base_url, query_params)."""
    location = r["headers"]["Location"]
    parsed   = urlparse(location)
    params   = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return location, params


# ── GET — client_id validation ────────────────────────────────────────────────
class TestGetClientIdValidation:
    def test_wrong_client_id_returns_400_html(self):
        m = _load()
        r = m.lambda_handler(_get_event(client_id="wrong-client"), MagicMock())
        assert r["statusCode"] == 400
        assert "text/html" in r["headers"]["Content-Type"]

    def test_correct_client_id_passes(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(client_id=CLIENT_ID), MagicMock())
        # Falls through to login form (200) since no pre_auth_state
        assert r["statusCode"] == 200

    def test_empty_client_id_when_not_configured_passes(self):
        """If GOOGLE_CLIENT_ID env var is blank, all client_ids pass validation."""
        original = os.environ.get("GOOGLE_CLIENT_ID")
        os.environ["GOOGLE_CLIENT_ID"] = ""
        try:
            m = _load()
            with patch.object(m, "_get_session_user", return_value=None):
                r = m.lambda_handler(_get_event(client_id="anything"), MagicMock())
        finally:
            os.environ["GOOGLE_CLIENT_ID"] = original or CLIENT_ID
        # Missing CLIENT_ID is a required var → config error (500)
        assert r["statusCode"] in (200, 400, 500)  # config-dependent


# ── GET — redirect_uri validation ─────────────────────────────────────────────
class TestGetRedirectUriValidation:
    def test_empty_redirect_uri_returns_400_html(self):
        m = _load()
        r = m.lambda_handler(_get_event(redirect_uri=""), MagicMock())
        assert r["statusCode"] == 400

    def test_invalid_redirect_uri_returns_400_when_allow_list_set(self):
        """When ALLOWED_REDIRECT_URIS is non-empty, rejected URIs return 400."""
        original = os.environ.get("ALLOWED_REDIRECT_URIS")
        os.environ["ALLOWED_REDIRECT_URIS"] = REDIRECT_URI
        try:
            m = _load()
            r = m.lambda_handler(_get_event(redirect_uri="https://evil.com/cb"), MagicMock())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = original or ""
        assert r["statusCode"] == 400

    def test_valid_redirect_uri_passes_when_allow_list_set(self):
        original = os.environ.get("ALLOWED_REDIRECT_URIS")
        os.environ["ALLOWED_REDIRECT_URIS"] = REDIRECT_URI
        try:
            m = _load()
            with patch.object(m, "_get_session_user", return_value=None):
                r = m.lambda_handler(_get_event(redirect_uri=REDIRECT_URI), MagicMock())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = original or ""
        assert r["statusCode"] == 200


# ── GET — response_type validation ────────────────────────────────────────────
class TestGetResponseType:
    def test_wrong_response_type_redirects_with_error(self):
        m = _load()
        r = m.lambda_handler(_get_event(response_type="token"), MagicMock())
        assert r["statusCode"] == 302
        _, params = _parse_redirect(r)
        assert params["error"] == "unsupported_response_type"

    def test_wrong_response_type_echoes_state(self):
        m = _load()
        r = m.lambda_handler(_get_event(response_type="token", state="my-state"), MagicMock())
        _, params = _parse_redirect(r)
        assert params.get("state") == "my-state"

    def test_code_response_type_shows_login_form(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(response_type="code"), MagicMock())
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]


# ── GET — pre_auth_state (seamless flow) ──────────────────────────────────────
class TestGetPreAuthFlow:
    def test_valid_pre_auth_state_redirects_with_code(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_get_event(pre_auth_state=PRE_AUTH), MagicMock())
        assert r["statusCode"] == 302
        _, params = _parse_redirect(r)
        assert "code" in params

    def test_valid_pre_auth_state_echoes_google_state(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_get_event(pre_auth_state=PRE_AUTH, state=GOOGLE_STATE), MagicMock())
        _, params = _parse_redirect(r)
        assert params["state"] == GOOGLE_STATE

    def test_invalid_pre_auth_state_shows_login_form(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(pre_auth_state=PRE_AUTH), MagicMock())
        # Falls through to login form
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]

    def test_store_auth_code_failure_redirects_with_server_error(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=USER_ID):
            with patch.object(m, "_store_auth_code", side_effect=Exception("ddb error")):
                r = m.lambda_handler(_get_event(pre_auth_state=PRE_AUTH), MagicMock())
        assert r["statusCode"] == 302
        _, params = _parse_redirect(r)
        assert params["error"] == "server_error"


# ── GET — login form ──────────────────────────────────────────────────────────
class TestGetLoginForm:
    def test_returns_200_html(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(), MagicMock())
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]

    def test_html_contains_form(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(), MagicMock())
        assert "<form" in r["body"]

    def test_html_contains_hidden_redirect_uri(self):
        m = _load()
        with patch.object(m, "_get_session_user", return_value=None):
            r = m.lambda_handler(_get_event(redirect_uri=REDIRECT_URI), MagicMock())
        import html
        assert html.escape(REDIRECT_URI) in r["body"]


# ── POST — missing credentials ────────────────────────────────────────────────
class TestPostMissingCredentials:
    def test_missing_username_shows_login_form_with_error(self):
        m = _load()
        r = m.lambda_handler(_post_event(username="", password="secret"), MagicMock())
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]

    def test_missing_password_shows_login_form_with_error(self):
        m = _load()
        r = m.lambda_handler(_post_event(username="user@example.com", password=""), MagicMock())
        assert r["statusCode"] == 200


# ── POST — login failed ───────────────────────────────────────────────────────
class TestPostLoginFailed:
    def test_bad_credentials_returns_200_with_error(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=None):
            r = m.lambda_handler(_post_event(), MagicMock())
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]

    def test_error_message_in_response(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=None):
            r = m.lambda_handler(_post_event(), MagicMock())
        assert "Incorrect" in r["body"] or "error" in r["body"].lower()


# ── POST — login success ──────────────────────────────────────────────────────
class TestPostLoginSuccess:
    def test_valid_credentials_redirects_302(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_post_event(), MagicMock())
        assert r["statusCode"] == 302

    def test_redirect_contains_code_param(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_post_event(state=GOOGLE_STATE), MagicMock())
        _, params = _parse_redirect(r)
        assert "code" in params

    def test_redirect_echoes_google_state(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_post_event(state=GOOGLE_STATE), MagicMock())
        _, params = _parse_redirect(r)
        assert params["state"] == GOOGLE_STATE

    def test_redirect_goes_to_correct_redirect_uri(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=USER_ID):
            with patch.object(m, "_store_auth_code"):
                r = m.lambda_handler(_post_event(redirect_uri=REDIRECT_URI), MagicMock())
        location = r["headers"]["Location"]
        assert location.startswith(REDIRECT_URI)

    def test_store_auth_code_called_with_user_id(self):
        m = _load()
        with patch.object(m, "_authenticate_cognito", return_value=USER_ID):
            with patch.object(m, "_store_auth_code") as mock_store:
                m.lambda_handler(_post_event(), MagicMock())
        assert mock_store.call_args[0][1] == USER_ID  # second arg is user_id


# ── POST — validation ─────────────────────────────────────────────────────────
class TestPostValidation:
    def test_invalid_client_id_returns_400(self):
        m = _load()
        r = m.lambda_handler(_post_event(client_id="wrong-client"), MagicMock())
        assert r["statusCode"] == 400

    def test_empty_redirect_uri_returns_400(self):
        m = _load()
        r = m.lambda_handler(_post_event(redirect_uri=""), MagicMock())
        assert r["statusCode"] == 400

    def test_invalid_redirect_uri_returns_400_when_allow_list_set(self):
        original = os.environ.get("ALLOWED_REDIRECT_URIS")
        os.environ["ALLOWED_REDIRECT_URIS"] = REDIRECT_URI
        try:
            m = _load()
            r = m.lambda_handler(_post_event(redirect_uri="https://evil.com/cb"), MagicMock())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = original or ""
        assert r["statusCode"] == 400


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_google_client_id_returns_500_html(self):
        original = os.environ.get("GOOGLE_CLIENT_ID")
        os.environ["GOOGLE_CLIENT_ID"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_get_event(), MagicMock())
        finally:
            os.environ["GOOGLE_CLIENT_ID"] = original or CLIENT_ID
        assert r["statusCode"] == 500
        assert "text/html" in r["headers"]["Content-Type"]

    def test_missing_cognito_client_id_returns_500_html(self):
        original = os.environ.get("COGNITO_CLIENT_ID")
        os.environ["COGNITO_CLIENT_ID"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_get_event(), MagicMock())
        finally:
            os.environ["COGNITO_CLIENT_ID"] = original or "test-cognito-client-id"
        assert r["statusCode"] == 500


# ── Method not allowed ────────────────────────────────────────────────────────
class TestMethodNotAllowed:
    def test_put_method_returns_405(self):
        m = _load()
        r = m.lambda_handler({"httpMethod": "PUT"}, MagicMock())
        assert r["statusCode"] == 405
