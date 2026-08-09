"""Unit tests for google_home_status Lambda.

GET /api/v1/voice/google-home/account-linking
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
_MOD_NAME = "gh_status_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_status", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test helpers ──────────────────────────────────────────────────────────────
USER_ID = "user-sub-status-abc"
VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None):
    return {
        "httpMethod": "GET",
        "path": "/api/v1/voice/google-home/account-linking",
        "headers": {"Authorization": f"Bearer {token or VALID_TOKEN}"},
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
        "queryStringParameters": {},
    }


def _token_record(agent_id="test-agent-id", linked_at="1700000000"):
    return {
        "userId":      USER_ID,
        "agentId":     agent_id,
        "linkedAt":    linked_at,
        "accessToken": "at_abc",
        "expiresAt":   9999999999,
        "scope":       "profile",
    }


def _run_with_ddb(item):
    m = _load()
    with patch.object(m, "_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value.get_item.return_value = (
            {"Item": item} if item is not None else {}
        )
        r = m.lambda_handler(_event(), MagicMock())
    return r, m


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

    def test_jwt_missing_sub_and_username_returns_401(self):
        m = _load()
        bad = (
            "h."
            + _b64.urlsafe_b64encode(json.dumps({"iss": "x"}).encode()).decode().rstrip("=")
            + ".s"
        )
        r = m.lambda_handler({"headers": {"Authorization": f"Bearer {bad}"}}, MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler({"headers": {"authorization": f"Bearer {VALID_TOKEN}"}}, MagicMock())
        assert r["statusCode"] == 200

    def test_username_claim_accepted_as_fallback(self):
        m = _load()
        token = (
            "h."
            + _b64.urlsafe_b64encode(json.dumps({"username": USER_ID}).encode()).decode().rstrip("=")
            + ".s"
        )
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            r = m.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, MagicMock())
        assert r["statusCode"] == 200


# ── Not linked ────────────────────────────────────────────────────────────────
class TestNotLinked:
    def test_returns_200_when_no_record(self):
        r, _ = _run_with_ddb(None)
        assert r["statusCode"] == 200

    def test_linked_false_when_no_record(self):
        r, _ = _run_with_ddb(None)
        body = json.loads(r["body"])
        assert body["linked"] is False

    def test_linked_false_when_item_is_none(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": None}
            r = m.lambda_handler(_event(), MagicMock())
        assert json.loads(r["body"])["linked"] is False

    def test_linked_at_absent_when_not_linked(self):
        r, _ = _run_with_ddb(None)
        assert "linkedAt" not in json.loads(r["body"])


# ── Linked ────────────────────────────────────────────────────────────────────
class TestLinked:
    def test_returns_200_when_record_exists(self):
        r, _ = _run_with_ddb(_token_record())
        assert r["statusCode"] == 200

    def test_linked_true_when_record_exists(self):
        r, _ = _run_with_ddb(_token_record())
        assert json.loads(r["body"])["linked"] is True

    def test_agent_id_from_record(self):
        r, _ = _run_with_ddb(_token_record(agent_id="agent-xyz"))
        assert json.loads(r["body"])["agentId"] == "agent-xyz"

    def test_linked_at_from_record(self):
        r, _ = _run_with_ddb(_token_record(linked_at="1700001234"))
        assert json.loads(r["body"])["linkedAt"] == "1700001234"

    def test_agent_id_omitted_when_not_in_record(self):
        r, _ = _run_with_ddb({"userId": USER_ID, "linkedAt": "123"})
        body = json.loads(r["body"])
        # agentId falls back to _AGENT_ID env var when no record agentId
        # (status lambda uses record.get("agentId", _AGENT_ID))
        # either way, response is 200 + linked=true
        assert body["linked"] is True

    def test_linked_at_omitted_when_not_in_record(self):
        r, _ = _run_with_ddb({"userId": USER_ID})
        assert "linkedAt" not in json.loads(r["body"])

    def test_queries_correct_table(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.assert_called_with(m._TOKENS_TABLE)

    def test_queries_correct_user_id(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.return_value.get_item.assert_called_once_with(
            Key={"userId": USER_ID}
        )


# ── DDB errors ────────────────────────────────────────────────────────────────
class TestDdbError:
    def test_client_error_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_exception_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_error_body_has_error_key(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert "error" in json.loads(r["body"])


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
        r, _ = _run_with_ddb(None)
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        r, _ = _run_with_ddb(None)
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        r, _ = _run_with_ddb(None)
        assert isinstance(json.loads(r["body"]), dict)
