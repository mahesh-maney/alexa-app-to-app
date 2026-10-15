"""Unit tests for google_home_oauth_authorize Lambda — Cognito-redirect flow.

GET /google-home/oauth/authorize
  Validates OAuth params → stores transaction in DDB → 302 to Cognito Hosted UI.

The old App-Flip (POST) flow was removed; the lambda is now GET-only.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError

_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_authorize_fn"
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


CLIENT_ID    = "test-google-client-id"
REDIRECT_URI = "https://oauth-redirect.googleusercontent.com/r/test-project"
GOOGLE_STATE = "google-csrf-state-AbCxYz"


def _get_event(client_id=CLIENT_ID, redirect_uri=REDIRECT_URI,
               response_type="code", state=GOOGLE_STATE, scope="profile"):
    return {
        "httpMethod": "GET",
        "path": "/google-home/oauth/authorize",
        "queryStringParameters": {
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": response_type,
            "state":         state,
            "scope":         scope,
        },
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _ctx():
    c = MagicMock()
    c.aws_request_id = "req-test-abc"
    return c


def _ddb_ok():
    m = MagicMock()
    m.return_value.Table.return_value.put_item.return_value = {}
    return m


# ── Config errors ──────────────────────────────────────────────────────────────
class TestConfigErrors:
    def _missing(self, var: str):
        orig = os.environ.pop(var, "")
        try:
            m = _load()
            r = m.lambda_handler(_get_event(), _ctx())
        finally:
            if orig:
                os.environ[var] = orig
        return r

    def test_missing_cognito_hosted_ui_domain_returns_500(self):
        assert self._missing("COGNITO_HOSTED_UI_DOMAIN")["statusCode"] == 500

    def test_missing_cognito_callback_uri_returns_500(self):
        assert self._missing("COGNITO_CALLBACK_URI")["statusCode"] == 500

    def test_missing_data_region_returns_500(self):
        assert self._missing("DATA_REGION")["statusCode"] == 500

    def test_missing_google_client_id_returns_500(self):
        assert self._missing("GOOGLE_CLIENT_ID")["statusCode"] == 500

    def test_missing_cognito_client_id_returns_500(self):
        assert self._missing("COGNITO_CLIENT_ID")["statusCode"] == 500

    def test_500_body_is_html(self):
        r = self._missing("COGNITO_HOSTED_UI_DOMAIN")
        assert "text/html" in r["headers"]["Content-Type"]
        assert "<html" in r["body"]


# ── Method routing ─────────────────────────────────────────────────────────────
class TestMethodRouting:
    def test_post_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "POST"}
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(ev, _ctx())["statusCode"] == 405

    def test_put_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "PUT"}
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(ev, _ctx())["statusCode"] == 405

    def test_delete_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "DELETE"}
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(ev, _ctx())["statusCode"] == 405

    def test_patch_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "PATCH"}
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(ev, _ctx())["statusCode"] == 405

    def test_get_returns_302(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(_get_event(), _ctx())["statusCode"] == 302

    def test_405_body_is_html(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "PATCH"}
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(ev, _ctx())
        assert "text/html" in r["headers"]["Content-Type"]


# ── client_id validation ───────────────────────────────────────────────────────
class TestClientIdValidation:
    def test_wrong_client_id_returns_400_html(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(client_id="wrong-client"), _ctx())
        assert r["statusCode"] == 400
        assert "text/html" in r["headers"]["Content-Type"]

    def test_wrong_client_id_does_not_store_transaction(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m.lambda_handler(_get_event(client_id="wrong-client"), _ctx())
        mock_ddb.return_value.Table.return_value.put_item.assert_not_called()

    def test_wrong_client_id_body_mentions_invalid(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(client_id="bad"), _ctx())
        assert "invalid" in r["body"].lower() or "client" in r["body"].lower()

    def test_correct_client_id_proceeds_to_302(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            assert m.lambda_handler(_get_event(), _ctx())["statusCode"] == 302

    def test_unconfigured_google_client_id_accepts_any(self):
        orig = os.environ.pop("GOOGLE_CLIENT_ID", "")
        try:
            m = _load()
            with patch.object(m, "_ddb", _ddb_ok()):
                r = m.lambda_handler(_get_event(client_id="anything"), _ctx())
        finally:
            if orig:
                os.environ["GOOGLE_CLIENT_ID"] = orig
        assert r["statusCode"] != 400


# ── redirect_uri validation ────────────────────────────────────────────────────
class TestRedirectUriValidation:
    def test_empty_redirect_uri_returns_400(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(redirect_uri=""), _ctx())
        assert r["statusCode"] == 400

    def test_redirect_uri_not_in_allowlist_returns_400(self):
        orig = os.environ.get("ALLOWED_REDIRECT_URIS", "")
        os.environ["ALLOWED_REDIRECT_URIS"] = "https://allowed.example.com/cb"
        try:
            m = _load()
            with patch.object(m, "_ddb", _ddb_ok()):
                r = m.lambda_handler(_get_event(redirect_uri="https://evil.com/steal"), _ctx())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = orig
        assert r["statusCode"] == 400

    def test_redirect_uri_in_allowlist_passes(self):
        allowed = "https://allowed.example.com/callback"
        orig = os.environ.get("ALLOWED_REDIRECT_URIS", "")
        os.environ["ALLOWED_REDIRECT_URIS"] = allowed
        try:
            m = _load()
            with patch.object(m, "_ddb", _ddb_ok()):
                r = m.lambda_handler(_get_event(redirect_uri=allowed), _ctx())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = orig
        assert r["statusCode"] == 302

    def test_empty_allowlist_accepts_any_uri(self):
        orig = os.environ.get("ALLOWED_REDIRECT_URIS", "")
        os.environ["ALLOWED_REDIRECT_URIS"] = ""
        try:
            m = _load()
            with patch.object(m, "_ddb", _ddb_ok()):
                r = m.lambda_handler(_get_event(redirect_uri="https://anything.example.com/cb"), _ctx())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = orig
        assert r["statusCode"] == 302

    def test_multiple_allowed_uris_second_matches(self):
        uri_a, uri_b = "https://a.example.com/cb", "https://b.example.com/cb"
        orig = os.environ.get("ALLOWED_REDIRECT_URIS", "")
        os.environ["ALLOWED_REDIRECT_URIS"] = f"{uri_a},{uri_b}"
        try:
            m = _load()
            with patch.object(m, "_ddb", _ddb_ok()):
                r = m.lambda_handler(_get_event(redirect_uri=uri_b), _ctx())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = orig
        assert r["statusCode"] == 302

    def test_redirect_uri_not_in_allowlist_does_not_store_transaction(self):
        orig = os.environ.get("ALLOWED_REDIRECT_URIS", "")
        os.environ["ALLOWED_REDIRECT_URIS"] = "https://allowed.example.com/cb"
        try:
            m = _load()
            with patch.object(m, "_ddb") as mock_ddb:
                m.lambda_handler(_get_event(redirect_uri="https://evil.com/steal"), _ctx())
        finally:
            os.environ["ALLOWED_REDIRECT_URIS"] = orig
        mock_ddb.return_value.Table.return_value.put_item.assert_not_called()


# ── response_type validation ───────────────────────────────────────────────────
class TestResponseTypeValidation:
    def test_response_type_token_redirects_error(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(response_type="token"), _ctx())
        assert r["statusCode"] == 302
        assert "error=unsupported_response_type" in r["headers"]["Location"]

    def test_response_type_empty_redirects_error(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(response_type=""), _ctx())
        assert "error=unsupported_response_type" in r["headers"]["Location"]

    def test_response_type_error_echoes_state(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(response_type="token"), _ctx())
        qs = parse_qs(urlparse(r["headers"]["Location"]).query)
        assert qs["state"][0] == GOOGLE_STATE

    def test_response_type_code_is_valid(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(response_type="code"), _ctx())
        assert "error" not in r["headers"]["Location"]

    def test_response_type_error_does_not_store_transaction(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m.lambda_handler(_get_event(response_type="token"), _ctx())
        mock_ddb.return_value.Table.return_value.put_item.assert_not_called()


# ── state validation ───────────────────────────────────────────────────────────
class TestStateValidation:
    def test_empty_state_redirects_invalid_request(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(state=""), _ctx())
        assert r["statusCode"] == 302
        assert "error=invalid_request" in r["headers"]["Location"]

    def test_empty_state_does_not_store_transaction(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m.lambda_handler(_get_event(state=""), _ctx())
        mock_ddb.return_value.Table.return_value.put_item.assert_not_called()

    def test_whitespace_only_state_treated_as_empty(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(state="   "), _ctx())
        assert "error=invalid_request" in r["headers"]["Location"]

    def test_valid_state_passed_to_cognito_url(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(state="my-csrf-token-123"), _ctx())
        qs = parse_qs(urlparse(r["headers"]["Location"]).query)
        assert qs["state"][0] == "my-csrf-token-123"


# ── DynamoDB transaction storage ──────────────────────────────────────────────
class TestTransactionStorage:
    def test_put_item_called_once(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(), _ctx())
        mock_ddb.return_value.Table.return_value.put_item.assert_called_once()

    def test_transaction_state_matches_google_state(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(), _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["state"] == GOOGLE_STATE

    def test_transaction_stores_client_id(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(), _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["client_id"] == CLIENT_ID

    def test_transaction_stores_redirect_uri(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(), _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["redirect_uri"] == REDIRECT_URI

    def test_transaction_stores_custom_scope(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(scope="openid email"), _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["scope"] == "openid email"

    def test_transaction_scope_defaults_to_profile(self):
        m = _load()
        ev = _get_event()
        ev["queryStringParameters"].pop("scope")
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(ev, _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["scope"] == "profile"

    def test_transaction_ttl_greater_than_created_at(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_get_event(), _ctx())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["ttl"] > item["created_at"]

    def test_ddb_client_error_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "PutItem"
            )
            r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 302
        assert "error=server_error" in r["headers"]["Location"]

    def test_ddb_error_echoes_state(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "x"}}, "PutItem"
            )
            r = m.lambda_handler(_get_event(), _ctx())
        qs = parse_qs(urlparse(r["headers"]["Location"]).query)
        assert qs["state"][0] == GOOGLE_STATE

    def test_ddb_error_redirect_goes_to_redirect_uri(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "PutItem"
            )
            r = m.lambda_handler(_get_event(), _ctx())
        assert r["headers"]["Location"].startswith(REDIRECT_URI)


# ── Cognito redirect URL ──────────────────────────────────────────────────────
class TestCognitoRedirectUrl:
    def _qs(self) -> dict:
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 302
        return parse_qs(urlparse(r["headers"]["Location"]).query)

    def test_redirects_to_cognito_domain(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(), _ctx())
        assert "auth.test.example.com" in r["headers"]["Location"]

    def test_url_has_oauth2_authorize_path(self):
        m = _load()
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(_get_event(), _ctx())
        assert "/oauth2/authorize" in r["headers"]["Location"]

    def test_cognito_response_type_is_code(self):
        assert self._qs()["response_type"][0] == "code"

    def test_cognito_client_id_is_cognito_client(self):
        assert self._qs()["client_id"][0] == "test-cognito-client-id"

    def test_cognito_redirect_uri_points_to_callback(self):
        assert "cognito/callback" in self._qs()["redirect_uri"][0]

    def test_cognito_state_equals_google_state(self):
        assert self._qs()["state"][0] == GOOGLE_STATE

    def test_cognito_scope_includes_openid(self):
        assert "openid" in self._qs()["scope"][0]

    def test_cognito_has_prompt_login(self):
        """prompt=login must force login on every GH account-linking attempt."""
        assert self._qs().get("prompt", [None])[0] == "login"

    def test_null_query_string_parameters_handled(self):
        m = _load()
        ev = {
            "httpMethod": "GET",
            "queryStringParameters": None,
            "requestContext": {"identity": {"sourceIp": ""}},
        }
        with patch.object(m, "_ddb", _ddb_ok()):
            r = m.lambda_handler(ev, _ctx())
        assert r["statusCode"] in (400, 302, 500)
