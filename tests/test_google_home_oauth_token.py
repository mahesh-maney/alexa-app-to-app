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
