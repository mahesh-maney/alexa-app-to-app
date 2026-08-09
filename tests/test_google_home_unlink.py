"""Unit tests for google_home_unlink Lambda.

DELETE /api/v1/voice/google-home/account-linking
"""
from __future__ import annotations

import base64 as _b64
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_unlink_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_unlink", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test helpers ──────────────────────────────────────────────────────────────
USER_ID = "user-sub-unlink-abc"
VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None):
    return {
        "httpMethod": "DELETE",
        "path": "/api/v1/voice/google-home/account-linking",
        "headers": {"Authorization": f"Bearer {token or VALID_TOKEN}"},
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _token_record(access_token="at_abc123"):
    return {
        "userId":       USER_ID,
        "agentId":      "test-agent-id",
        "linkedAt":     "1700000000",
        "accessToken":  access_token,
        "refreshToken": "rt_abc123",
        "scope":        "profile",
    }


def _run_unlink(record, mock_revoke=True):
    """Run unlink with a given token record (or None for already-unlinked).
    Optionally mock _revoke_google_token to prevent HTTP calls."""
    m = _load()
    with patch.object(m, "_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value.get_item.return_value = (
            {"Item": record} if record is not None else {}
        )
        mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
        if mock_revoke:
            with patch.object(m, "_revoke_google_token") as mock_rev:
                r = m.lambda_handler(_event(), MagicMock())
            return r, m, mock_ddb, mock_rev
        else:
            r = m.lambda_handler(_event(), MagicMock())
            return r, m, mock_ddb, None


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_missing_header_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {}}, MagicMock())
        assert r["statusCode"] == 401

    def test_no_headers_key_returns_401(self):
        m = _load()
        r = m.lambda_handler({}, MagicMock())
        assert r["statusCode"] == 401

    def test_malformed_jwt_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {"Authorization": "Bearer notajwt"}}, MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            with patch.object(m, "_revoke_google_token"):
                r = m.lambda_handler({"headers": {"authorization": f"Bearer {VALID_TOKEN}"}}, MagicMock())
        assert r["statusCode"] == 200


# ── Already unlinked (idempotent) ─────────────────────────────────────────────
class TestAlreadyUnlinked:
    def test_returns_200_when_no_record(self):
        r, _, _, _ = _run_unlink(None)
        assert r["statusCode"] == 200

    def test_returns_linked_false_when_no_record(self):
        r, _, _, _ = _run_unlink(None)
        assert json.loads(r["body"]) == {"linked": False}

    def test_delete_not_called_when_no_record(self):
        r, _, mock_ddb, _ = _run_unlink(None)
        mock_ddb.return_value.Table.return_value.delete_item.assert_not_called()

    def test_revoke_not_called_when_no_record(self):
        r, _, _, mock_rev = _run_unlink(None)
        mock_rev.assert_not_called()


# ── Successful unlink ─────────────────────────────────────────────────────────
class TestSuccessfulUnlink:
    def test_returns_200(self):
        r, _, _, _ = _run_unlink(_token_record())
        assert r["statusCode"] == 200

    def test_returns_linked_false(self):
        r, _, _, _ = _run_unlink(_token_record())
        assert json.loads(r["body"]) == {"linked": False}

    def test_delete_item_called_with_correct_user_id(self):
        r, m, mock_ddb, _ = _run_unlink(_token_record())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once_with(
            Key={"userId": USER_ID}
        )

    def test_delete_item_called_on_tokens_table(self):
        r, m, mock_ddb, _ = _run_unlink(_token_record())
        # Both get_item and delete_item go through the same mock Table
        mock_ddb.return_value.Table.assert_called_with(m._TOKENS_TABLE)

    def test_revoke_called_when_access_token_present(self):
        r, _, _, mock_rev = _run_unlink(_token_record(access_token="at_secret"))
        mock_rev.assert_called_once_with("at_secret", USER_ID)

    def test_revoke_not_called_when_no_access_token(self):
        record = {"userId": USER_ID, "agentId": "a"}  # no accessToken key
        r, _, _, mock_rev = _run_unlink(record)
        mock_rev.assert_not_called()

    def test_unlink_succeeds_even_if_revoke_network_error(self):
        """Even if the HTTP token revocation call fails, DDB delete still runs."""
        import urllib.error
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = urllib.error.HTTPError(
                    url="https://oauth2.googleapis.com/revoke",
                    code=400, msg="Bad Request", hdrs=None, fp=None,
                )
                r = m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once()
        assert r["statusCode"] == 200


# ── Token revocation (unit tests for _revoke_google_token helper) ─────────────
class TestRevokeGoogleToken:
    def test_http_error_is_swallowed(self):
        import urllib.error
        m = _load()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://oauth2.googleapis.com/revoke",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=None,
            )
            # Should not raise
            m._revoke_google_token("some_token", USER_ID)

    def test_url_error_is_swallowed(self):
        import urllib.error
        m = _load()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("connection refused")
            m._revoke_google_token("some_token", USER_ID)

    def test_unexpected_error_is_swallowed(self):
        m = _load()
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = RuntimeError("unexpected")
            m._revoke_google_token("some_token", USER_ID)

    def test_successful_revoke_does_not_raise(self):
        m = _load()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            m._revoke_google_token("some_token", USER_ID)  # should not raise


# ── DDB errors ────────────────────────────────────────────────────────────────
class TestDdbGetError:
    def test_client_error_on_get_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_error_on_get_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500


class TestDdbDeleteError:
    def test_client_error_on_delete_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem"
            )
            with patch.object(m, "_revoke_google_token"):
                r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_error_on_delete_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": _token_record()}
            mock_ddb.return_value.Table.return_value.delete_item.side_effect = RuntimeError("ddb down")
            with patch.object(m, "_revoke_google_token"):
                r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_data_region_returns_500(self):
        original = os.environ.get("DATA_REGION")
        os.environ["DATA_REGION"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_event(), MagicMock())
        finally:
            os.environ["DATA_REGION"] = original or "ap-south-1"
        assert r["statusCode"] == 500


# ── Response shape ────────────────────────────────────────────────────────────
class TestResponseShape:
    def test_cors_header_present(self):
        r, _, _, _ = _run_unlink(None)
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        r, _, _, _ = _run_unlink(None)
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        r, _, _, _ = _run_unlink(None)
        assert isinstance(json.loads(r["body"]), dict)
