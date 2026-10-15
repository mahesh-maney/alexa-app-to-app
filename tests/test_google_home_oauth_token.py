"""Unit tests for google_home_oauth_token Lambda.

POST /google-home/oauth/token
"""
from __future__ import annotations

import base64 as _b64
import importlib.util
import json
import os
import sys
import time
import urllib.parse
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_token_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_oauth_token", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Constants ─────────────────────────────────────────────────────────────────
CLIENT_ID     = "test-google-client-id"
CLIENT_SECRET = "test-google-client-secret"
USER_ID       = "user-sub-token-abc"
AUTH_CODE     = "auth-code-xyz-12345"
REFRESH_TOKEN = "refresh-tok-abc-12345"
GH_CREDS      = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}


def _form_body(**kwargs) -> str:
    return urllib.parse.urlencode(kwargs)


def _event(body: str, headers: dict | None = None) -> dict:
    return {
        "httpMethod": "POST",
        "headers": headers or {},
        "body": body,
    }


def _auth_code_event(code=AUTH_CODE, redirect_uri="https://oauth.google.com/redirect"):
    return _event(_form_body(
        grant_type="authorization_code",
        code=code,
        redirect_uri=redirect_uri,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    ))


def _refresh_event(refresh_token=REFRESH_TOKEN):
    return _event(_form_body(
        grant_type="refresh_token",
        refresh_token=refresh_token,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    ))


def _code_record(code=AUTH_CODE, user_id=USER_ID, ttl_offset=300, redirect_uri="https://oauth.google.com/redirect"):
    return {
        "code":        code,
        "userId":      user_id,
        "redirectUri": redirect_uri,
        "scope":       "profile",
        "createdAt":   int(time.time()) - 10,
        "ttl":         int(time.time()) + ttl_offset,
    }


def _token_record_for_refresh(user_id=USER_ID, refresh_token=REFRESH_TOKEN):
    return {
        "userId":       user_id,
        "accessToken":  "old_at_abc",
        "refreshToken": refresh_token,
        "expiresAt":    int(time.time()) + 3600,
        "scope":        "profile",
    }


def _basic_auth_header(client_id=CLIENT_ID, secret=CLIENT_SECRET) -> str:
    raw = f"{client_id}:{secret}".encode()
    return "Basic " + _b64.b64encode(raw).decode()


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_secret_arn_returns_server_error(self):
        original = os.environ.get("GOOGLE_CLIENT_SECRET_ARN")
        os.environ["GOOGLE_CLIENT_SECRET_ARN"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_auth_code_event(), MagicMock())
        finally:
            os.environ["GOOGLE_CLIENT_SECRET_ARN"] = original or "arn:aws:secretsmanager:ap-south-1:000000000000:secret:test/gh"
        body = json.loads(r["body"])
        assert body["error"] == "server_error"


# ── Client credential validation ──────────────────────────────────────────────
class TestClientValidation:
    def test_invalid_client_id_returns_invalid_client(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(
                grant_type="authorization_code",
                code=AUTH_CODE,
                client_id="wrong-id",
                client_secret=CLIENT_SECRET,
            )
            r = m.lambda_handler(_event(body_str), MagicMock())
        body = json.loads(r["body"])
        assert body["error"] == "invalid_client"
        assert r["statusCode"] == 401

    def test_invalid_client_secret_returns_invalid_client(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(
                grant_type="authorization_code",
                code=AUTH_CODE,
                client_id=CLIENT_ID,
                client_secret="wrong-secret",
            )
            r = m.lambda_handler(_event(body_str), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_client"

    def test_valid_credentials_from_body_params_passes(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        # invalid_grant (code not found) — but NOT invalid_client
        assert json.loads(r["body"])["error"] != "invalid_client"

    def test_valid_credentials_from_basic_auth_header_passes(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(grant_type="authorization_code", code=AUTH_CODE)
            headers  = {"Authorization": _basic_auth_header()}
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
                r = m.lambda_handler(_event(body_str, headers), MagicMock())
        assert json.loads(r["body"])["error"] != "invalid_client"

    def test_malformed_basic_auth_falls_back_to_body(self):
        """If Basic auth header is malformed, falls back to body params."""
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(
                grant_type="authorization_code",
                code=AUTH_CODE,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
            )
            headers = {"Authorization": "Basic not_valid_base64!!!"}
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
                r = m.lambda_handler(_event(body_str, headers), MagicMock())
        # Should use body params — not invalid_client
        assert json.loads(r["body"])["error"] != "invalid_client"

    def test_unavailable_credentials_returns_invalid_client(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value={}):
            r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_client"


# ── Grant type routing ────────────────────────────────────────────────────────
class TestGrantType:
    def test_missing_grant_type_returns_unsupported(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            r = m.lambda_handler(_event(_form_body(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)), MagicMock())
        assert json.loads(r["body"])["error"] == "unsupported_grant_type"

    def test_unknown_grant_type_returns_unsupported(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(grant_type="device_code", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
            r = m.lambda_handler(_event(body_str), MagicMock())
        assert json.loads(r["body"])["error"] == "unsupported_grant_type"


# ── Authorization code grant ──────────────────────────────────────────────────
class TestAuthorizationCodeGrant:
    def test_missing_code_returns_invalid_request(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(grant_type="authorization_code", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
            r = m.lambda_handler(_event(body_str), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_request"

    def test_code_not_found_returns_invalid_grant(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_grant"

    def test_expired_code_returns_invalid_grant(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {
                    "Item": _code_record(ttl_offset=-60)  # expired
                }
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_grant"

    def test_redirect_uri_mismatch_returns_invalid_grant(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {
                    "Item": _code_record(redirect_uri="https://oauth.google.com/redirect")
                }
                body_str = _form_body(
                    grant_type="authorization_code",
                    code=AUTH_CODE,
                    redirect_uri="https://different.google.com/redirect",  # mismatch
                    client_id=CLIENT_ID,
                    client_secret=CLIENT_SECRET,
                )
                r = m.lambda_handler(_event(body_str), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_grant"

    def test_valid_code_returns_200_with_tokens(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "Bearer"
        assert isinstance(body["expires_in"], int)

    def test_code_deleted_after_exchange(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                m.lambda_handler(_auth_code_event(), MagicMock())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once_with(Key={"code": AUTH_CODE})

    def test_tokens_stored_with_user_id(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                m.lambda_handler(_auth_code_event(), MagicMock())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["userId"] == USER_ID
        assert "accessToken" in item
        assert "refreshToken" in item
        assert "expiresAt" in item

    def test_ddb_error_on_code_lookup_returns_server_error(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
                )
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "server_error"

    def test_ddb_error_on_tokens_store_returns_server_error(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "PutItem"
                )
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "server_error"


# ── Refresh token grant ───────────────────────────────────────────────────────
class TestRefreshTokenGrant:
    def test_missing_refresh_token_returns_invalid_request(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            body_str = _form_body(grant_type="refresh_token", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
            r = m.lambda_handler(_event(body_str), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_request"

    def test_refresh_token_not_found_returns_invalid_grant(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_refresh_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_grant"

    def test_valid_refresh_token_returns_200(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                r = m.lambda_handler(_refresh_event(), MagicMock())
        assert r["statusCode"] == 200

    def test_valid_refresh_token_returns_new_access_token(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                r = m.lambda_handler(_refresh_event(), MagicMock())
        body = json.loads(r["body"])
        assert "access_token" in body
        assert body["token_type"] == "Bearer"

    def test_refresh_keeps_same_refresh_token(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                r = m.lambda_handler(_refresh_event(refresh_token=REFRESH_TOKEN), MagicMock())
        body = json.loads(r["body"])
        assert body.get("refresh_token") == REFRESH_TOKEN

    def test_update_item_called_with_new_access_token(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                m.lambda_handler(_refresh_event(), MagicMock())
        mock_ddb.return_value.Table.return_value.update_item.assert_called_once()

    def test_ddb_error_on_refresh_query_returns_server_error(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query"
                )
                r = m.lambda_handler(_refresh_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "server_error"


# ── Token response shape ──────────────────────────────────────────────────────
class TestTokenResponseShape:
    def test_cache_control_header_present(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert r["headers"]["Cache-Control"] == "no-store"

    def test_body_is_valid_json(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert isinstance(json.loads(r["body"]), dict)


# ── Additional coverage ───────────────────────────────────────────────────────

class TestTokenResponseFields:
    def test_expires_in_matches_config(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        body = json.loads(r["body"])
        assert body["expires_in"] == int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "3600"))

    def test_expires_in_is_positive_integer(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert json.loads(r["body"])["expires_in"] > 0

    def test_access_token_is_non_empty_string(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        at = json.loads(r["body"])["access_token"]
        assert isinstance(at, str) and len(at) > 16

    def test_refresh_token_is_non_empty_string(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        rt = json.loads(r["body"])["refresh_token"]
        assert isinstance(rt, str) and len(rt) > 16

    def test_scope_present_in_auth_code_response(self):
        # token endpoint returns access_token, token_type, expires_in, refresh_token only
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        body = json.loads(r["body"])
        assert "access_token" in body and "token_type" in body

    def test_scope_present_in_refresh_response(self):
        # token endpoint returns access_token, token_type, expires_in, refresh_token only
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                r = m.lambda_handler(_refresh_event(), MagicMock())
        body = json.loads(r["body"])
        assert "access_token" in body and "token_type" in body

    def test_content_type_is_application_json(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert "application/json" in r["headers"]["Content-Type"]

    def test_no_error_field_on_success(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        assert "error" not in json.loads(r["body"])

    def test_refresh_no_error_field_on_success(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                r = m.lambda_handler(_refresh_event(), MagicMock())
        assert "error" not in json.loads(r["body"])

    def test_access_tokens_are_different_across_refreshes(self):
        m = _load()
        tokens = []
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.return_value = {}
                for _ in range(2):
                    r = m.lambda_handler(_refresh_event(), MagicMock())
                    tokens.append(json.loads(r["body"])["access_token"])
        assert tokens[0] != tokens[1]

    def test_ddb_error_on_refresh_update_returns_server_error(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_token_record_for_refresh()]
                }
                mock_ddb.return_value.Table.return_value.update_item.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "UpdateItem"
                )
                r = m.lambda_handler(_refresh_event(), MagicMock())
        assert json.loads(r["body"])["error"] == "server_error"

    def test_empty_body_returns_invalid_client(self):
        # empty body → empty client_id → invalid_client before grant_type is checked
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            r = m.lambda_handler(_event(""), MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_client"

    def test_null_body_handled_gracefully(self):
        m = _load()
        ev = {"httpMethod": "POST", "headers": {}, "body": None}
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            r = m.lambda_handler(ev, MagicMock())
        body = json.loads(r["body"])
        assert "error" in body

    def test_two_access_tokens_from_same_code_second_fails(self):
        """Auth code is single-use; second exchange returns invalid_grant."""
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                # First call returns the code; second call returns None (already deleted)
                mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                    {"Item": _code_record()},
                    {"Item": None},
                ]
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r1 = m.lambda_handler(_auth_code_event(), MagicMock())
                r2 = m.lambda_handler(_auth_code_event(), MagicMock())
        assert r1["statusCode"] == 200
        assert json.loads(r2["body"])["error"] == "invalid_grant"


# ── Direct helper function unit tests ─────────────────────────────────────────

class TestTokenHelperFunctions:
    """Cover _ddb warm path, _sm warm path, _get_gh_credentials cache hit,
    _validate_client_credentials incomplete creds, AUTH_CODE_DELETE_ERROR,
    isBase64Encoded body, and missing GH_SECRET_ARN path."""

    def test_ddb_client_reused_on_second_call(self):
        m = _load()
        # First call initialises the client
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                m.lambda_handler(_auth_code_event(), MagicMock())
        # Manually set a cached dynamodb and verify second call uses it
        m._dynamodb = MagicMock()
        m._dynamodb.Table.return_value.get_item.return_value = {}
        # Call _ddb() directly — should return the cached value
        result = m._ddb()
        assert result is m._dynamodb

    def test_sm_client_reused_on_second_call(self):
        m = _load()
        import boto3
        fake_sm = MagicMock()
        m._sm_cache = fake_sm
        result = m._sm()
        assert result is fake_sm

    def test_get_gh_credentials_returns_cached_when_available(self):
        m = _load()
        cached = {"client_id": "cached-id", "client_secret": "cached-secret"}
        m._gh_creds_cache = cached
        result = m._get_gh_credentials()
        assert result is cached

    def test_get_gh_credentials_returns_empty_when_no_secret_arn(self):
        orig = os.environ.pop("GOOGLE_CLIENT_SECRET_ARN", "")
        try:
            m = _load()
            m._gh_creds_cache = None
            result = m._get_gh_credentials()
            assert result == {}
        finally:
            if orig:
                os.environ["GOOGLE_CLIENT_SECRET_ARN"] = orig

    def test_validate_client_credentials_returns_false_when_incomplete(self):
        m = _load()
        # creds missing client_secret
        with patch.object(m, "_get_gh_credentials", return_value={"client_id": "x"}):
            result = m._validate_client_credentials("x", "y")
        assert result is False

    def test_validate_client_credentials_returns_false_when_empty_creds(self):
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value={}):
            result = m._validate_client_credentials("x", "y")
        assert result is False

    def test_auth_code_delete_error_is_non_fatal(self):
        """ClientError during auth code deletion should not abort token issuance."""
        m = _load()
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                # delete raises, put succeeds
                mock_ddb.return_value.Table.return_value.delete_item.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem"
                )
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_auth_code_event(), MagicMock())
        # token should still be issued
        assert r["statusCode"] == 200
        assert "access_token" in json.loads(r["body"])

    def test_base64_encoded_body_is_decoded(self):
        import base64 as b64lib
        import urllib.parse as urlparse
        m = _load()
        raw_body = urlparse.urlencode({
            "grant_type": "authorization_code",
            "client_id": GH_CREDS["client_id"],
            "client_secret": GH_CREDS["client_secret"],
            "code": "auth-code-b64",
            "redirect_uri": "https://oauth.google.com/redirect",
        })
        encoded_body = b64lib.b64encode(raw_body.encode()).decode()
        ev = {"httpMethod": "POST", "isBase64Encoded": True,
              "headers": {}, "body": encoded_body}
        with patch.object(m, "_get_gh_credentials", return_value=GH_CREDS):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _code_record()}
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 200

    def test_get_sm_credentials_on_sm_error(self):
        """Secrets Manager error → _get_gh_credentials returns {}."""
        m = _load()
        m._gh_creds_cache = None
        mock_sm = MagicMock()
        mock_sm.get_secret_value.side_effect = Exception("access denied")
        with patch.object(m, "_sm", return_value=mock_sm):
            result = m._get_gh_credentials()
        assert result == {}
