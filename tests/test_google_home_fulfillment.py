"""Unit tests for google_home_fulfillment Lambda.

POST /google-home/fulfillment
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_fulfillment_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_fulfillment", "lambda_function.py"),
)


def _load():
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Constants ─────────────────────────────────────────────────────────────────
USER_ID      = "user-sub-fulfill-abc"
ACCESS_TOKEN = "valid_access_token_xyz"
SITE_ID      = "site-12345"


def _event(intent: str, payload: dict | None = None, token: str = ACCESS_TOKEN) -> dict:
    return {
        "httpMethod": "POST",
        "headers": {"Authorization": f"Bearer {token}"},
        "body": json.dumps({
            "requestId": "req-123",
            "inputs": [{"intent": intent, "payload": payload or {}}],
        }),
    }


def _token_query_result(user_id=USER_ID, expires_offset=3600):
    return {
        "Items": [{
            "userId":       user_id,
            "accessToken":  ACCESS_TOKEN,
            "refreshToken": "rt_xyz",
            "expiresAt":    int(time.time()) + expires_offset,
        }]
    }


def _site_item(site_id=SITE_ID, site_name="My Home"):
    return {"userId": USER_ID, "siteId": site_id, "siteName": site_name}


def _run(intent, payload=None, resolve_user_id=USER_ID, ddb_side_effects=None):
    """Helper: mock _resolve_user_id and optionally _ddb."""
    m = _load()
    with patch.object(m, "_resolve_user_id", return_value=resolve_user_id):
        with patch.object(m, "_ddb") as mock_ddb:
            if ddb_side_effects is not None:
                mock_ddb.return_value.Table.return_value.query.side_effect = ddb_side_effects
                mock_ddb.return_value.Table.return_value.delete_item.side_effect = ddb_side_effects
            r = m.lambda_handler(_event(intent, payload), MagicMock())
    return r, m, mock_ddb


# ── Authentication ────────────────────────────────────────────────────────────
class TestAuth:
    def test_no_authorization_header_returns_401(self):
        m = _load()
        r = m.lambda_handler({"headers": {}, "body": "{}"}, MagicMock())
        assert r["statusCode"] == 401

    def test_no_headers_key_returns_401(self):
        m = _load()
        r = m.lambda_handler({"body": "{}"}, MagicMock())
        assert r["statusCode"] == 401

    def test_non_bearer_scheme_returns_401(self):
        m = _load()
        r = m.lambda_handler(
            {"headers": {"Authorization": "Basic abc"}, "body": "{}"},
            MagicMock(),
        )
        assert r["statusCode"] == 401

    def test_invalid_token_returns_401(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=None):
            r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 401

    def test_lowercase_authorization_header_accepted(self):
        m = _load()
        ev = {
            "headers": {"authorization": f"Bearer {ACCESS_TOKEN}"},
            "body": json.dumps({"requestId": "r1", "inputs": [{"intent": "action.devices.SYNC", "payload": {}}]}),
        }
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 200

    def test_expired_token_returns_401(self):
        """_resolve_user_id returns None if token expired — treated as 401."""
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=None):
            r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 401


# ── Token resolution (unit tests for _resolve_user_id) ───────────────────────
class TestResolveUserId:
    def test_returns_user_id_when_token_found_and_valid(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = _token_query_result()
            user_id = m._resolve_user_id(ACCESS_TOKEN)
        assert user_id == USER_ID

    def test_returns_none_when_token_not_found(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            result = m._resolve_user_id("nonexistent_token")
        assert result is None

    def test_returns_none_when_token_expired(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = _token_query_result(
                expires_offset=-100  # expired
            )
            result = m._resolve_user_id(ACCESS_TOKEN)
        assert result is None

    def test_returns_none_on_ddb_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query"
            )
            result = m._resolve_user_id(ACCESS_TOKEN)
        assert result is None

    def test_queries_access_token_index(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            m._resolve_user_id(ACCESS_TOKEN)
        call_kwargs = mock_ddb.return_value.Table.return_value.query.call_args[1]
        assert call_kwargs["IndexName"] == "accessToken-index"


# ── SYNC intent ───────────────────────────────────────────────────────────────
class TestSync:
    def test_returns_200(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_site_item()]
                }
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 200

    def test_response_contains_devices(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_site_item(site_id="s1", site_name="Living Room")]
                }
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        body = json.loads(r["body"])
        assert "devices" in body["payload"]
        devices = body["payload"]["devices"]
        assert len(devices) == 1
        assert devices[0]["id"] == "site:s1"

    def test_device_name_matches_site_name(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {
                    "Items": [_site_item(site_name="Kitchen")]
                }
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        device = json.loads(r["body"])["payload"]["devices"][0]
        assert device["name"]["name"] == "Kitchen"

    def test_empty_devices_when_no_sites(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        devices = json.loads(r["body"])["payload"]["devices"]
        assert devices == []

    def test_agent_user_id_in_response(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert json.loads(r["body"])["payload"]["agentUserId"] == USER_ID

    def test_ddb_error_returns_empty_devices(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query"
                )
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        # _get_user_devices returns [] on error — still 200
        assert r["statusCode"] == 200
        assert json.loads(r["body"])["payload"]["devices"] == []


# ── QUERY intent ──────────────────────────────────────────────────────────────
class TestQuery:
    def test_returns_200(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            payload = {"devices": [{"id": "site:s1"}]}
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.QUERY", payload), MagicMock())
        assert r["statusCode"] == 200

    def test_returns_device_states(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            payload = {"devices": [{"id": "site:s1"}, {"id": "site:s2"}]}
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.QUERY", payload), MagicMock())
        devices = json.loads(r["body"])["payload"]["devices"]
        assert "site:s1" in devices
        assert "site:s2" in devices

    def test_each_device_has_online_true(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            payload = {"devices": [{"id": "site:s1"}]}
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.QUERY", payload), MagicMock())
        device_state = json.loads(r["body"])["payload"]["devices"]["site:s1"]
        assert device_state["online"] is True
        assert device_state["status"] == "SUCCESS"

    def test_empty_devices_list_returns_empty_states(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.QUERY", {"devices": []}), MagicMock())
        assert json.loads(r["body"])["payload"]["devices"] == {}


# ── EXECUTE intent ────────────────────────────────────────────────────────────
class TestExecute:
    def _execute_payload(self, device_ids=None, command="action.devices.commands.ActivateScene"):
        device_ids = device_ids or ["site:s1"]
        return {
            "commands": [{
                "devices":   [{"id": did} for did in device_ids],
                "execution": [{"command": command, "params": {}}],
            }]
        }

    def test_returns_200(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.EXECUTE", self._execute_payload()), MagicMock())
        assert r["statusCode"] == 200

    def test_response_contains_commands_list(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.EXECUTE", self._execute_payload()), MagicMock())
        results = json.loads(r["body"])["payload"]["commands"]
        assert isinstance(results, list)
        assert len(results) == 1

    def test_result_has_success_status(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.EXECUTE", self._execute_payload(["site:s1"])), MagicMock())
        result = json.loads(r["body"])["payload"]["commands"][0]
        assert result["status"] == "SUCCESS"
        assert "site:s1" in result["ids"]

    def test_multiple_devices_each_get_result(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(
                    _event("action.devices.EXECUTE", self._execute_payload(["site:s1", "site:s2"])),
                    MagicMock(),
                )
        results = json.loads(r["body"])["payload"]["commands"]
        all_ids = [r["ids"][0] for r in results]
        assert "site:s1" in all_ids
        assert "site:s2" in all_ids


# ── DISCONNECT intent ─────────────────────────────────────────────────────────
class TestDisconnect:
    def test_returns_200(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                r = m.lambda_handler(_event("action.devices.DISCONNECT"), MagicMock())
        assert r["statusCode"] == 200

    def test_deletes_token_record(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                m.lambda_handler(_event("action.devices.DISCONNECT"), MagicMock())
        mock_ddb.return_value.Table.return_value.delete_item.assert_called_once_with(
            Key={"userId": USER_ID}
        )

    def test_returns_empty_payload(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.delete_item.return_value = {}
                r = m.lambda_handler(_event("action.devices.DISCONNECT"), MagicMock())
        body = json.loads(r["body"])
        assert body.get("payload") == {}

    def test_ddb_error_still_returns_200(self):
        """DISCONNECT should not fail even if DDB delete fails."""
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.delete_item.side_effect = ClientError(
                    {"Error": {"Code": "InternalServerError", "Message": "x"}}, "DeleteItem"
                )
                r = m.lambda_handler(_event("action.devices.DISCONNECT"), MagicMock())
        assert r["statusCode"] == 200


# ── Unknown / missing intent ──────────────────────────────────────────────────
class TestUnknownIntent:
    def test_unknown_intent_returns_400(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb"):
                r = m.lambda_handler(_event("action.devices.UNKNOWN_THING"), MagicMock())
        assert r["statusCode"] == 400

    def test_no_inputs_returns_400(self):
        m = _load()
        ev = {
            "headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"},
            "body": json.dumps({"requestId": "r1", "inputs": []}),
        }
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400


# ── Body parsing ──────────────────────────────────────────────────────────────
class TestBodyParsing:
    def test_invalid_json_body_returns_400(self):
        m = _load()
        ev = {"headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"}, "body": "{{bad json"}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400

    def test_null_body_handled(self):
        m = _load()
        ev = {"headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"}, "body": None}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400


# ── Config error ──────────────────────────────────────────────────────────────
class TestConfigError:
    def test_missing_data_region_returns_500(self):
        original = os.environ.get("DATA_REGION")
        os.environ["DATA_REGION"] = ""
        try:
            m = _load()
            r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        finally:
            os.environ["DATA_REGION"] = original or "ap-south-1"
        assert r["statusCode"] == 500


# ── Response shape ────────────────────────────────────────────────────────────
class TestResponseShape:
    def test_content_type_is_json(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert isinstance(json.loads(r["body"]), dict)

    def test_response_contains_request_id(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_ddb") as mock_ddb:
                mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        # requestId is echoed from Google's request
        assert "requestId" in json.loads(r["body"])
