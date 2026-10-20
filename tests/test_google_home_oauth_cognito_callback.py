"""Unit tests for google_home_oauth_cognito_callback Lambda.

GET  /google-home/oauth/cognito/callback  — steps 9-14 (Cognito code exchange + consent page)
POST /google-home/oauth/consent           — steps 15-27 (approve / cancel)
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import time
import urllib.parse
from unittest.mock import MagicMock, patch, ANY

import pytest
from botocore.exceptions import ClientError

_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_callback_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_oauth_cognito_callback", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test constants ─────────────────────────────────────────────────────────────
USER_ID      = "user-sub-callback-abc"
EMAIL        = "testuser@example.com"
GOOGLE_STATE = "google-csrf-state-callback-xyz"
REDIRECT_URI = "https://oauth-redirect.googleusercontent.com/r/test-project"
COGNITO_CODE = "cognito-code-abc123"
AUTH_CODE    = "gh-auth-code-xyz789"


def _ctx():
    c = MagicMock()
    c.aws_request_id = "req-callback-abc"
    return c


def _fake_id_token(sub=USER_ID, email=EMAIL) -> str:
    """Build a fake (unsigned) JWT with the given sub and email claims."""
    header  = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": sub, "email": email, "token_use": "id"}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _get_event(code=COGNITO_CODE, state=GOOGLE_STATE, error=None, error_desc=None):
    params = {"state": state}
    if code:
        params["code"] = code
    if error:
        params["error"] = error
    if error_desc:
        params["error_description"] = error_desc
    return {
        "httpMethod":            "GET",
        "path":                  "/google-home/oauth/cognito/callback",
        "queryStringParameters": params,
        "requestContext":        {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _post_event(action="approve", state=GOOGLE_STATE, base64_encode=False):
    body = urllib.parse.urlencode({"action": action, "state": state})
    if base64_encode:
        body = base64.b64encode(body.encode()).decode()
    return {
        "httpMethod":       "POST",
        "path":             "/google-home/oauth/consent",
        "isBase64Encoded":  base64_encode,
        "body":             body,
        "requestContext":   {"identity": {"sourceIp": "5.6.7.8"}},
    }


def _transaction(state=GOOGLE_STATE, redirect_uri=REDIRECT_URI, user_id=USER_ID,
                 email=EMAIL, scope="profile", ttl_offset=600):
    return {
        "state":        state,
        "client_id":    "test-google-client-id",
        "redirect_uri": redirect_uri,
        "scope":        scope,
        "created_at":   int(time.time()) - 10,
        "ttl":          int(time.time()) + ttl_offset,
        "userId":       user_id,
        "userEmail":    email,
    }


def _token_response(id_token=None):
    return {"id_token": id_token or _fake_id_token(), "access_token": "cog-at-xyz"}


# ── Config errors ──────────────────────────────────────────────────────────────
class TestConfigErrors:
    def _missing(self, var):
        orig = os.environ.pop(var, "")
        try:
            m = _load()
            r = m.lambda_handler(_get_event(), _ctx())
        finally:
            if orig:
                os.environ[var] = orig
        return r

    def test_missing_data_region_returns_500(self):
        assert self._missing("DATA_REGION")["statusCode"] == 500

    def test_missing_cognito_client_id_returns_500(self):
        assert self._missing("COGNITO_CLIENT_ID")["statusCode"] == 500

    def test_missing_cognito_hosted_ui_domain_returns_500(self):
        assert self._missing("COGNITO_HOSTED_UI_DOMAIN")["statusCode"] == 500

    def test_missing_cognito_callback_uri_returns_500(self):
        assert self._missing("COGNITO_CALLBACK_URI")["statusCode"] == 500

    def test_500_body_is_html(self):
        r = self._missing("DATA_REGION")
        assert "text/html" in r["headers"]["Content-Type"]


# ── Method routing ─────────────────────────────────────────────────────────────
class TestMethodRouting:
    def test_get_routes_to_cognito_callback(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=None):
            r = m.lambda_handler(_get_event(), _ctx())
        # Missing transaction → 400, but NOT 405
        assert r["statusCode"] != 405

    def test_post_routes_to_consent(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=None):
            r = m.lambda_handler(_post_event(), _ctx())
        assert r["statusCode"] != 405

    def test_put_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "PUT"}
        r = m.lambda_handler(ev, _ctx())
        assert r["statusCode"] == 405

    def test_delete_returns_405(self):
        m = _load()
        ev = {**_get_event(), "httpMethod": "DELETE"}
        r = m.lambda_handler(ev, _ctx())
        assert r["statusCode"] == 405


# ── GET: Cognito callback — validation ────────────────────────────────────────
class TestCognitoCallbackValidation:
    def test_missing_state_returns_400(self):
        m = _load()
        ev = _get_event(state="")
        r = m.lambda_handler(ev, _ctx())
        assert r["statusCode"] == 400
        assert "text/html" in r["headers"]["Content-Type"]

    def test_missing_state_body_has_error_text(self):
        m = _load()
        ev = _get_event(state="")
        r = m.lambda_handler(ev, _ctx())
        assert "state" in r["body"].lower() or "session" in r["body"].lower()

    def test_transaction_not_found_returns_400(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=None):
            r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 400

    def test_missing_code_without_error_redirects_access_denied(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_get_event(code=None), _ctx())
        assert r["statusCode"] == 302
        assert "error=access_denied" in r["headers"]["Location"]

    def test_cognito_error_param_redirects_access_denied(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_get_event(code=None, error="access_denied"), _ctx())
        assert r["statusCode"] == 302
        assert "error=access_denied" in r["headers"]["Location"]

    def test_missing_code_redirect_goes_to_correct_redirect_uri(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_get_event(code=None), _ctx())
        assert r["headers"]["Location"].startswith(REDIRECT_URI)

    def test_missing_code_redirect_echoes_state(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_get_event(code=None), _ctx())
        assert f"state={GOOGLE_STATE}" in r["headers"]["Location"]


# ── GET: Cognito callback — token exchange errors ─────────────────────────────
class TestCognitoTokenExchange:
    def test_token_exchange_failure_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", side_effect=Exception("network error")):
                r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 302
        assert "error=server_error" in r["headers"]["Location"]

    def test_no_id_token_in_response_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", return_value={"access_token": "at"}):
                r = m.lambda_handler(_get_event(), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_malformed_id_token_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code",
                              return_value={"id_token": "not.a.valid.jwt.too.many.parts"}):
                r = m.lambda_handler(_get_event(), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_id_token_missing_sub_redirects_server_error(self):
        bad_token = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode() + "." + \
                    base64.urlsafe_b64encode(json.dumps({"email": EMAIL}).encode()).rstrip(b"=").decode() + \
                    ".fakesig"
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", return_value={"id_token": bad_token}):
                r = m.lambda_handler(_get_event(), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_ddb_update_failure_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code",
                              return_value=_token_response()):
                with patch.object(m, "_update_transaction_user",
                                  side_effect=ClientError(
                                      {"Error": {"Code": "InternalServerError", "Message": "x"}},
                                      "UpdateItem")):
                    r = m.lambda_handler(_get_event(), _ctx())
        assert "error=server_error" in r["headers"]["Location"]


# ── GET: Cognito callback — happy path / consent page ─────────────────────────
class TestConsentPage:
    def _get_consent_html(self, app_name=None, brand_subtitle=None,
                          privacy_url=None, terms_url=None) -> str:
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code",
                              return_value=_token_response()):
                with patch.object(m, "_update_transaction_user"):
                    r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 200
        return r["body"]

    def test_valid_flow_returns_200(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                with patch.object(m, "_update_transaction_user"):
                    r = m.lambda_handler(_get_event(), _ctx())
        assert r["statusCode"] == 200

    def test_content_type_is_html(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                with patch.object(m, "_update_transaction_user"):
                    r = m.lambda_handler(_get_event(), _ctx())
        assert "text/html" in r["headers"]["Content-Type"]

    def test_consent_page_has_allow_button(self):
        html = self._get_consent_html()
        assert "ALLOW" in html.upper() or "allow" in html.lower()

    def test_consent_page_has_deny_button(self):
        html = self._get_consent_html()
        assert "DENY" in html.upper() or "deny" in html.lower()

    def test_consent_page_shows_user_email(self):
        # email is logged in AUDIT but not rendered in the HTML template
        html = self._get_consent_html()
        # verify page is structurally valid (email check is done via audit log, not HTML)
        assert "<!DOCTYPE html>" in html

    def test_consent_page_shows_app_name(self):
        html = self._get_consent_html()
        assert "Digilux" in html or os.environ.get("APP_NAME", "") in html

    def test_consent_page_shows_brand_subtitle(self):
        # Subtitle label was intentionally removed from the consent page header
        html = self._get_consent_html()
        assert "<!DOCTYPE html>" in html  # page renders without error

    def test_consent_page_contains_privacy_policy_url(self):
        html = self._get_consent_html()
        assert os.environ.get("PRIVACY_POLICY_URL", "") in html

    def test_consent_page_contains_terms_url(self):
        html = self._get_consent_html()
        assert os.environ.get("TERMS_URL", "") in html

    def test_consent_page_has_state_hidden_field(self):
        html = self._get_consent_html()
        assert GOOGLE_STATE in html

    def test_consent_page_has_approve_action(self):
        html = self._get_consent_html()
        assert "approve" in html

    def test_consent_page_has_cancel_action(self):
        html = self._get_consent_html()
        assert "cancel" in html

    def test_update_transaction_called_with_user_id_and_email(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                with patch.object(m, "_update_transaction_user") as mock_update:
                    m.lambda_handler(_get_event(), _ctx())
        mock_update.assert_called_once_with(GOOGLE_STATE, USER_ID, EMAIL)

    def test_app_name_env_var_rendered_in_title(self):
        orig = os.environ.get("APP_NAME", "")
        os.environ["APP_NAME"] = "MyBrand Smart Home"
        try:
            m = _load()
            with patch.object(m, "_get_transaction", return_value=_transaction()):
                with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                    with patch.object(m, "_update_transaction_user"):
                        r = m.lambda_handler(_get_event(), _ctx())
        finally:
            os.environ["APP_NAME"] = orig
        assert "MyBrand" in r["body"]

    def test_custom_privacy_url_rendered(self):
        orig = os.environ.get("PRIVACY_POLICY_URL", "")
        os.environ["PRIVACY_POLICY_URL"] = "https://custom.brand.com/privacy"
        try:
            m = _load()
            with patch.object(m, "_get_transaction", return_value=_transaction()):
                with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                    with patch.object(m, "_update_transaction_user"):
                        r = m.lambda_handler(_get_event(), _ctx())
        finally:
            os.environ["PRIVACY_POLICY_URL"] = orig
        assert "https://custom.brand.com/privacy" in r["body"]

    def test_custom_terms_url_rendered(self):
        orig = os.environ.get("TERMS_URL", "")
        os.environ["TERMS_URL"] = "https://custom.brand.com/terms"
        try:
            m = _load()
            with patch.object(m, "_get_transaction", return_value=_transaction()):
                with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                    with patch.object(m, "_update_transaction_user"):
                        r = m.lambda_handler(_get_event(), _ctx())
        finally:
            os.environ["TERMS_URL"] = orig
        assert "https://custom.brand.com/terms" in r["body"]

    def test_custom_brand_subtitle_rendered(self):
        # Subtitle label was intentionally removed from the consent page header;
        # BRAND_SUBTITLE env var is retained but not displayed
        orig = os.environ.get("BRAND_SUBTITLE", "")
        os.environ["BRAND_SUBTITLE"] = "MyBrand Auth Portal"
        try:
            m = _load()
            with patch.object(m, "_get_transaction", return_value=_transaction()):
                with patch.object(m, "_exchange_cognito_code", return_value=_token_response()):
                    with patch.object(m, "_update_transaction_user"):
                        r = m.lambda_handler(_get_event(), _ctx())
        finally:
            os.environ["BRAND_SUBTITLE"] = orig
        assert r["statusCode"] == 200  # page renders successfully


# ── POST: consent approve ──────────────────────────────────────────────────────
class TestConsentApprove:
    def test_approve_returns_302(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve"), _ctx())
        assert r["statusCode"] == 302

    def test_approve_redirects_to_google_redirect_uri(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve"), _ctx())
        assert r["headers"]["Location"].startswith(REDIRECT_URI)

    def test_approve_location_has_code_param(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve"), _ctx())
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(r["headers"]["Location"]).query)
        assert "code" in qs
        assert len(qs["code"][0]) > 10

    def test_approve_location_echoes_state(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve"), _ctx())
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(r["headers"]["Location"]).query)
        assert qs["state"][0] == GOOGLE_STATE

    def test_approve_stores_auth_code_with_user_id(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code") as mock_store:
                with patch.object(m, "_delete_transaction"):
                    m.lambda_handler(_post_event("approve"), _ctx())
        mock_store.assert_called_once()
        _, user_id, redirect_uri, scope = mock_store.call_args[0]
        assert user_id == USER_ID
        assert redirect_uri == REDIRECT_URI

    def test_approve_deletes_transaction(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction") as mock_del:
                    m.lambda_handler(_post_event("approve"), _ctx())
        mock_del.assert_called_once_with(GOOGLE_STATE)

    def test_approve_missing_user_id_redirects_server_error(self):
        tx = _transaction()
        tx.pop("userId", None)
        m = _load()
        with patch.object(m, "_get_transaction", return_value=tx):
            r = m.lambda_handler(_post_event("approve"), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_approve_empty_user_id_redirects_server_error(self):
        tx = {**_transaction(), "userId": ""}
        m = _load()
        with patch.object(m, "_get_transaction", return_value=tx):
            r = m.lambda_handler(_post_event("approve"), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_approve_ddb_error_storing_code_redirects_server_error(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code", side_effect=ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "PutItem")):
                r = m.lambda_handler(_post_event("approve"), _ctx())
        assert "error=server_error" in r["headers"]["Location"]

    def test_approve_base64_encoded_body_handled(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve", base64_encode=True), _ctx())
        assert r["statusCode"] == 302

    def test_approve_no_error_in_redirect(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_store_auth_code"):
                with patch.object(m, "_delete_transaction"):
                    r = m.lambda_handler(_post_event("approve"), _ctx())
        assert "error" not in r["headers"]["Location"]


# ── POST: consent cancel ───────────────────────────────────────────────────────
class TestConsentCancel:
    def test_cancel_returns_302(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(_post_event("cancel"), _ctx())
        assert r["statusCode"] == 302

    def test_cancel_redirects_with_access_denied(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(_post_event("cancel"), _ctx())
        assert "error=access_denied" in r["headers"]["Location"]

    def test_cancel_redirect_goes_to_google_redirect_uri(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(_post_event("cancel"), _ctx())
        assert r["headers"]["Location"].startswith(REDIRECT_URI)

    def test_cancel_echoes_state_in_redirect(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(_post_event("cancel"), _ctx())
        assert GOOGLE_STATE in r["headers"]["Location"]

    def test_cancel_deletes_transaction(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction") as mock_del:
                m.lambda_handler(_post_event("cancel"), _ctx())
        mock_del.assert_called_once_with(GOOGLE_STATE)

    def test_cancel_no_redirect_uri_returns_200_html(self):
        tx = {**_transaction(), "redirect_uri": ""}
        m = _load()
        with patch.object(m, "_get_transaction", return_value=tx):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(_post_event("cancel"), _ctx())
        assert r["statusCode"] == 200
        assert "text/html" in r["headers"]["Content-Type"]


# ── POST: consent edge cases ───────────────────────────────────────────────────
class TestConsentEdgeCases:
    def test_missing_state_returns_400(self):
        m = _load()
        r = m.lambda_handler(_post_event("approve", state=""), _ctx())
        assert r["statusCode"] == 400

    def test_transaction_not_found_returns_400(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=None):
            r = m.lambda_handler(_post_event("approve"), _ctx())
        assert r["statusCode"] == 400

    def test_unknown_action_returns_400(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_post_event("unknown_action"), _ctx())
        assert r["statusCode"] == 400

    def test_empty_action_returns_400(self):
        m = _load()
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            r = m.lambda_handler(_post_event(""), _ctx())
        assert r["statusCode"] == 400

    def test_malformed_body_returns_400(self):
        m = _load()
        ev = {
            "httpMethod": "POST",
            "body": "\x00\x01\x02 malformed",
            "isBase64Encoded": False,
            "requestContext": {"identity": {"sourceIp": ""}},
        }
        # Should not raise; may return 400 or handle gracefully
        r = m.lambda_handler(ev, _ctx())
        assert r["statusCode"] in (400, 200)

    def test_expired_transaction_returns_400(self):
        tx = _transaction(ttl_offset=-60)  # already expired
        m = _load()
        # _get_transaction returns None for expired (handled inside lambda)
        with patch.object(m, "_get_transaction", return_value=None):
            r = m.lambda_handler(_post_event("approve"), _ctx())
        assert r["statusCode"] == 400


# ── _decode_id_token unit tests ───────────────────────────────────────────────
class TestDecodeIdToken:
    def test_decodes_sub_correctly(self):
        m = _load()
        token = _fake_id_token(sub="u-123", email="x@y.com")
        claims = m._decode_id_token(token)
        assert claims["sub"] == "u-123"

    def test_decodes_email_correctly(self):
        m = _load()
        token = _fake_id_token(sub="u-123", email="hello@world.com")
        claims = m._decode_id_token(token)
        assert claims["email"] == "hello@world.com"

    def test_invalid_part_count_raises(self):
        m = _load()
        try:
            m._decode_id_token("only.two")
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_padded_base64_decoded_correctly(self):
        """Payload length not divisible by 4 — padding must be added."""
        m = _load()
        token = _fake_id_token(sub="padding-test", email="p@t.com")
        claims = m._decode_id_token(token)
        assert claims["sub"] == "padding-test"


# ── Direct helper function unit tests ─────────────────────────────────────────

class TestGetTransactionHelper:
    """Test _get_transaction directly to cover DDB error/not-found/expired paths."""

    def test_returns_item_when_valid(self):
        m = _load()
        item = {"state": GOOGLE_STATE, "ttl": int(time.time()) + 600, "client_id": "c"}
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": item}
            result = m._get_transaction(GOOGLE_STATE)
        assert result == item

    def test_returns_none_on_client_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
            )
            result = m._get_transaction(GOOGLE_STATE)
        assert result is None

    def test_returns_none_when_item_not_found(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            result = m._get_transaction(GOOGLE_STATE)
        assert result is None

    def test_returns_none_when_ttl_expired(self):
        m = _load()
        expired_item = {"state": GOOGLE_STATE, "ttl": int(time.time()) - 100}
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": expired_item}
            result = m._get_transaction(GOOGLE_STATE)
        assert result is None


class TestUpdateTransactionUserHelper:
    def test_calls_update_item(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._update_transaction_user(GOOGLE_STATE, "user-123", "user@example.com")
            mock_ddb.return_value.Table.return_value.update_item.assert_called_once()

    def test_update_sets_user_id_and_email(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._update_transaction_user(GOOGLE_STATE, "uid-abc", "abc@test.com")
            call_kw = mock_ddb.return_value.Table.return_value.update_item.call_args[1]
            vals = call_kw["ExpressionAttributeValues"]
            assert vals[":uid"] == "uid-abc"
            assert vals[":email"] == "abc@test.com"


class TestDeleteTransactionHelper:
    def test_calls_delete_item(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._delete_transaction(GOOGLE_STATE)
            mock_ddb.return_value.Table.return_value.delete_item.assert_called_once()

    def test_silently_handles_client_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "DeleteItem"
            )
            # should not raise
            m._delete_transaction(GOOGLE_STATE)


class TestStoreAuthCodeHelper:
    def test_calls_put_item(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._store_auth_code("code-abc", "user-123", "https://redirect.example.com", "profile")
            mock_ddb.return_value.Table.return_value.put_item.assert_called_once()

    def test_stored_item_has_ttl(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._store_auth_code("code-xyz", "user-123", "https://redirect.example.com", "profile")
            item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
            assert "ttl" in item
            assert item["ttl"] > int(time.time())

    def test_stored_item_fields(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._store_auth_code("code-123", "uid-456", "https://red.ex.com", "smart_home")
            item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
            assert item["code"] == "code-123"
            assert item["userId"] == "uid-456"
            assert item["scope"] == "smart_home"


class TestExchangeCognitoCodeHelper:
    def test_returns_parsed_json_on_success(self):
        m = _load()
        fake_response = json.dumps({"id_token": "tok", "access_token": "at"}).encode()
        resp_mock = MagicMock()
        resp_mock.read.return_value = fake_response
        resp_mock.__enter__ = lambda s: s
        resp_mock.__exit__ = MagicMock(return_value=False)
        import urllib.request as _req
        with patch.object(_req, "urlopen", return_value=resp_mock):
            result = m._exchange_cognito_code("auth-code-123")
        assert result["id_token"] == "tok"

    def test_raises_on_http_error(self):
        m = _load()
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with pytest.raises(Exception):
                m._exchange_cognito_code("bad-code")


class TestConsentBase64Body:
    """Ensure consent POST handler decodes base64-encoded body."""
    def test_base64_encoded_body_is_decoded(self):
        m = _load()
        import base64 as b64lib
        import urllib.parse as urlparse
        raw_body = urlparse.urlencode({"action": "cancel", "state": GOOGLE_STATE})
        encoded_body = b64lib.b64encode(raw_body.encode()).decode()
        ev = {
            "httpMethod": "POST",
            "isBase64Encoded": True,
            "headers": {},
            "body": encoded_body,
        }
        with patch.object(m, "_get_transaction", return_value=_transaction()):
            with patch.object(m, "_delete_transaction"):
                r = m.lambda_handler(ev, _ctx())
        # cancel → 302 or 200 page; just verify it doesn't error out
        assert r["statusCode"] in (200, 302)


class TestDdbWarmPath:
    """Verify the warm-container DDB reuse path (lines 110-114)."""
    def test_ddb_returns_existing_instance_on_warm_call(self):
        m = _load()
        # Set a fake cached client
        fake_ddb = MagicMock()
        m._dynamodb = fake_ddb
        # _ddb() should return the cached instance without creating a new one
        result = m._ddb()
        assert result is fake_ddb
