"""Unit tests for google_home_start Lambda.

POST /api/v1/voice/google-home/account-linking/deep-link/start
"""
from __future__ import annotations

import base64 as _b64
import importlib.util
import json
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_start_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_start", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test helpers ──────────────────────────────────────────────────────────────
USER_ID = "user-sub-start-abc"
VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None):
    return {
        "httpMethod": "POST",
        "path": "/api/v1/voice/google-home/account-linking/deep-link/start",
        "headers": {"Authorization": f"Bearer {token or VALID_TOKEN}"},
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _run_ok(extra_env=None):
    """Load module and invoke with a valid auth token, mocking DDB put_item."""
    m = _load()
    with patch.object(m, "_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value.put_item.return_value = {}
        r = m.lambda_handler(_event(), MagicMock())
    return r, m, mock_ddb


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_missing_authorization_header_returns_401(self):
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

    def test_jwt_missing_sub_returns_401(self):
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
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
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
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            r = m.lambda_handler({"headers": {"Authorization": f"Bearer {token}"}}, MagicMock())
        assert r["statusCode"] == 200


# ── Session creation ──────────────────────────────────────────────────────────
class TestSessionCreation:
    def test_returns_200(self):
        r, _, _ = _run_ok()
        assert r["statusCode"] == 200

    def test_response_contains_state(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        assert "state" in body

    def test_state_is_valid_uuid4(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        parsed = uuid.UUID(body["state"])
        assert parsed.version == 4

    def test_response_contains_agent_id(self):
        r, m, _ = _run_ok()
        body = json.loads(r["body"])
        assert body["agentId"] == m._AGENT_ID

    def test_response_contains_home_app_deep_link(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        assert "homeAppDeepLink" in body
        assert "madeby.google.com" in body["homeAppDeepLink"]

    def test_deep_link_contains_agent_id(self):
        r, m, _ = _run_ok()
        body = json.loads(r["body"])
        assert m._AGENT_ID in body["homeAppDeepLink"]

    def test_put_item_called_on_sessions_table(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        mock_ddb.return_value.Table.assert_called_with(m._SESSIONS_TABLE)
        mock_ddb.return_value.Table.return_value.put_item.assert_called_once()

    def test_session_item_user_id_matches_jwt_sub(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["userId"] == USER_ID

    def test_session_item_has_pending_status(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["status"] == "PENDING"

    def test_session_item_has_ttl(self):
        import time
        m = _load()
        now = int(time.time())
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.return_value = {}
            m.lambda_handler(_event(), MagicMock())
        item = mock_ddb.return_value.Table.return_value.put_item.call_args[1]["Item"]
        assert item["ttl"] > now


# ── Web fallback URL ──────────────────────────────────────────────────────────
class TestWebFallback:
    def test_web_fallback_url_present_when_oauth_base_url_set(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        assert "webFallbackUrl" in body

    def test_web_fallback_url_absent_when_oauth_base_url_empty(self):
        original = os.environ.get("OAUTH_BASE_URL")
        os.environ["OAUTH_BASE_URL"] = ""
        try:
            m = _load()
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.put_item.return_value = {}
                r = m.lambda_handler(_event(), MagicMock())
        finally:
            os.environ["OAUTH_BASE_URL"] = original or "https://iot.digilux.co.in/smarthome"
        body = json.loads(r["body"])
        assert "webFallbackUrl" not in body

    def test_web_fallback_url_contains_pre_auth_state_param(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        if "webFallbackUrl" in body:
            assert "pre_auth_state=" in body["webFallbackUrl"]

    def test_web_fallback_url_state_matches_response_state(self):
        r, _, _ = _run_ok()
        body = json.loads(r["body"])
        if "webFallbackUrl" in body:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(body["webFallbackUrl"]).query)
            assert qs.get("pre_auth_state", [None])[0] == body["state"]


# ── DDB errors ────────────────────────────────────────────────────────────────
class TestDdbError:
    def test_client_error_on_put_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                "PutItem",
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_unexpected_exception_on_put_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = RuntimeError("boom")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_error_body_has_error_key(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.put_item.side_effect = RuntimeError("boom")
            r = m.lambda_handler(_event(), MagicMock())
        assert "error" in json.loads(r["body"])


# ── Config errors ─────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_google_agent_id_returns_500(self):
        original = os.environ.get("GOOGLE_AGENT_ID")
        os.environ["GOOGLE_AGENT_ID"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_event(), MagicMock())
        finally:
            os.environ["GOOGLE_AGENT_ID"] = original or "test-agent-id"
        assert r["statusCode"] == 500

    def test_missing_oauth_base_url_returns_500(self):
        # OAUTH_BASE_URL is required for google_home_start
        original_agent = os.environ.get("GOOGLE_AGENT_ID")
        original_oauth = os.environ.get("OAUTH_BASE_URL")
        os.environ["OAUTH_BASE_URL"] = ""
        os.environ["GOOGLE_AGENT_ID"] = ""  # also blank to trigger _missing_vars
        try:
            m = _load()
            r = m.lambda_handler(_event(), MagicMock())
        finally:
            os.environ["GOOGLE_AGENT_ID"] = original_agent or "test-agent-id"
            os.environ["OAUTH_BASE_URL"] = original_oauth or "https://iot.digilux.co.in/smarthome"
        assert r["statusCode"] == 500

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
        r, _, _ = _run_ok()
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        r, _, _ = _run_ok()
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json_string(self):
        r, _, _ = _run_ok()
        assert isinstance(json.loads(r["body"]), dict)
