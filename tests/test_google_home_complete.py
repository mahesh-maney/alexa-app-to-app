"""Unit tests for google_home_complete Lambda.

POST /api/v1/voice/google-home/account-linking/deep-link/complete
"""
from __future__ import annotations

import base64 as _b64
import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_complete_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_complete", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Test helpers ──────────────────────────────────────────────────────────────
USER_ID    = "user-sub-complete-abc"
OTHER_USER = "different-user-xyz"
STATE_UUID = "a1b2c3d4-e5f6-4789-ab12-cd34ef567890"

VALID_TOKEN = (
    "header."
    + _b64.urlsafe_b64encode(json.dumps({"sub": USER_ID}).encode()).decode().rstrip("=")
    + ".sig"
)


def _event(token=None, state=STATE_UUID, body_override=None):
    body = body_override if body_override is not None else {"state": state}
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token or VALID_TOKEN}"},
        "body": json.dumps(body),
        "requestContext": {"identity": {"sourceIp": "1.2.3.4"}},
    }


def _session(user_id=USER_ID, ttl_offset=600):
    now = int(time.time())
    return {
        "state":     STATE_UUID,
        "userId":    user_id,
        "status":    "PENDING",
        "createdAt": now - 10,
        "ttl":       now + ttl_offset,
    }


def _token_record(agent_id="test-agent-id", linked_at="1700000000"):
    return {
        "userId":       USER_ID,
        "agentId":      agent_id,
        "linkedAt":     linked_at,
        "accessToken":  "at_abc",
        "refreshToken": "rt_abc",
        "scope":        "profile",
    }


def _run_with_ddb(sess_item, tok_item=None, state=STATE_UUID):
    m = _load()
    with patch.object(m, "_ddb") as mock_ddb:
        mock_ddb.return_value.Table.return_value.get_item.side_effect = [
            {"Item": sess_item},
            {"Item": tok_item},
        ]
        r = m.lambda_handler(_event(state=state), MagicMock())
    return r, m, mock_ddb


# ── Auth ──────────────────────────────────────────────────────────────────────
class TestAuth:
    def test_missing_header_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {}, "body": "{}"}, MagicMock())
        assert r["statusCode"] == 401

    def test_no_headers_key_returns_401(self):
        m = _load()
        r = m.lambda_handler({"body": "{}"}, MagicMock())
        assert r["statusCode"] == 401

    def test_malformed_jwt_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {"Authorization": "Bearer notajwt"}, "body": "{}"}, MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                {"Item": _session()},
                {"Item": None},
            ]
            r = m.lambda_handler(
                {"headers": {"authorization": f"Bearer {VALID_TOKEN}"},
                 "body": json.dumps({"state": STATE_UUID})},
                MagicMock(),
            )
        assert r["statusCode"] == 200


# ── Body validation ───────────────────────────────────────────────────────────
class TestBodyValidation:
    def test_missing_state_returns_400(self):
        m = _load()
        with patch.object(m, "_ddb"):
            r = m.lambda_handler(_event(body_override={}), MagicMock())
        assert r["statusCode"] == 400

    def test_empty_state_string_returns_400(self):
        m = _load()
        with patch.object(m, "_ddb"):
            r = m.lambda_handler(_event(body_override={"state": ""}), MagicMock())
        assert r["statusCode"] == 400

    def test_invalid_json_body_returns_400(self):
        m = _load()
        ev = {"headers": {"Authorization": f"Bearer {VALID_TOKEN}"}, "body": "{{bad json"}
        r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400

    def test_null_body_is_handled(self):
        m = _load()
        ev = {"headers": {"Authorization": f"Bearer {VALID_TOKEN}"}, "body": None}
        r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400


# ── Session validation ────────────────────────────────────────────────────────
class TestSessionValidation:
    def test_session_not_found_returns_400(self):
        r, _, _ = _run_with_ddb(None)
        assert r["statusCode"] == 400

    def test_session_not_found_body_has_error(self):
        r, _, _ = _run_with_ddb(None)
        assert "error" in json.loads(r["body"])

    def test_session_owner_mismatch_returns_400(self):
        r, _, _ = _run_with_ddb(_session(user_id=OTHER_USER))
        assert r["statusCode"] == 400

    def test_expired_session_returns_400(self):
        r, _, _ = _run_with_ddb(_session(ttl_offset=-60))  # expired 60s ago
        assert r["statusCode"] == 400

    def test_valid_session_proceeds(self):
        r, _, _ = _run_with_ddb(_session(), None)
        assert r["statusCode"] == 200

    def test_session_lookup_uses_state_as_key(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                {"Item": _session()},
                {"Item": None},
            ]
            m.lambda_handler(_event(), MagicMock())
        first_call_key = mock_ddb.return_value.Table.return_value.get_item.call_args_list[0][1]["Key"]
        assert first_call_key == {"state": STATE_UUID}

    def test_token_lookup_uses_user_id_as_key(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                {"Item": _session()},
                {"Item": None},
            ]
            m.lambda_handler(_event(), MagicMock())
        second_call_key = mock_ddb.return_value.Table.return_value.get_item.call_args_list[1][1]["Key"]
        assert second_call_key == {"userId": USER_ID}


# ── Link status in response ───────────────────────────────────────────────────
class TestLinkStatus:
    def test_linked_false_when_no_token_record(self):
        r, _, _ = _run_with_ddb(_session(), None)
        body = json.loads(r["body"])
        assert body["linked"] is False

    def test_linked_true_when_token_record_exists(self):
        r, _, _ = _run_with_ddb(_session(), _token_record())
        body = json.loads(r["body"])
        assert body["linked"] is True

    def test_agent_id_included_when_in_token_record(self):
        r, _, _ = _run_with_ddb(_session(), _token_record(agent_id="my-agent"))
        body = json.loads(r["body"])
        assert body.get("agentId") == "my-agent"

    def test_linked_at_included_when_in_token_record(self):
        r, _, _ = _run_with_ddb(_session(), _token_record(linked_at="1700001234"))
        body = json.loads(r["body"])
        assert body.get("linkedAt") == "1700001234"

    def test_agent_id_omitted_when_not_in_token_record(self):
        r, _, _ = _run_with_ddb(_session(), {"userId": USER_ID})
        body = json.loads(r["body"])
        assert "agentId" not in body

    def test_linked_at_omitted_when_not_linked(self):
        r, _, _ = _run_with_ddb(_session(), None)
        body = json.loads(r["body"])
        assert "linkedAt" not in body


# ── DDB errors ────────────────────────────────────────────────────────────────
class TestDdbError:
    def test_session_table_client_error_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"
            )
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_session_table_unexpected_error_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = RuntimeError("ddb down")
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_token_table_client_error_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                {"Item": _session()},
                ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem"),
            ]
            r = m.lambda_handler(_event(), MagicMock())
        assert r["statusCode"] == 500

    def test_token_table_unexpected_error_returns_500(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.get_item.side_effect = [
                {"Item": _session()},
                RuntimeError("ddb down"),
            ]
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
        r, _, _ = _run_with_ddb(_session(), None)
        assert r["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_content_type_is_json(self):
        r, _, _ = _run_with_ddb(_session(), None)
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        r, _, _ = _run_with_ddb(_session(), None)
        assert isinstance(json.loads(r["body"]), dict)
