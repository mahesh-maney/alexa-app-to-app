"""Unit tests for google_home_fulfillment Lambda (merged mrinal + mahesh).

POST /google-home/fulfillment

Auth: opaque Bearer token → _resolve_user_id (accessToken-index GSI)
SYNC: _get_all_gh_devices (S3 + user_device_details DDB)
QUERY: _get_current_state (device_state DDB) + _decrypt_endpoint (AES-GCM)
EXECUTE: _publish_device_command (MQTT) + _update_device_state_db
DISCONNECT: delete from google_home_tokens
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch, call

from botocore.exceptions import ClientError

# ── Module loader ──────────────────────────────────────────────────────────────
_HERE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_NAME = "gh_fulfillment_fn"
_SPEC     = importlib.util.spec_from_file_location(
    _MOD_NAME,
    os.path.join(_HERE, "lambdas", "google_home_fulfillment", "lambda_function.py"),
)


def _load():
    os.environ.setdefault("DATA_REGION", "ap-south-1")
    sys.modules.pop(_MOD_NAME, None)
    m = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(m)
    sys.modules[_MOD_NAME] = m
    return m


# ── Constants ──────────────────────────────────────────────────────────────────
USER_ID      = "user-sub-fulfill-abc"
ACCESS_TOKEN = "valid_access_token_xyz"
DUID         = "duid-aabbcc"
MAC          = "aa:bb:cc:dd:ee:ff"
EP_ID        = 1
EP_TYPE      = 2   # LIGHT
SCENE_ID     = "42"


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
            "userId":      user_id,
            "accessToken": ACCESS_TOKEN,
            "expiresAt":   int(time.time()) + expires_offset,
        }]
    }


def _fake_endpoint_id(m) -> str:
    """Encrypt a test endpoint using the module's _encrypt_endpoint with a fake 32-byte key."""
    import base64
    fake_key = b"\x00" * 32
    with patch.object(m, "_get_endpoint_key", return_value=fake_key):
        return m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)


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
            "body": json.dumps({"requestId": "r1",
                                "inputs": [{"intent": "action.devices.SYNC", "payload": {}}]}),
        }
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 200

    def test_expired_token_returns_401(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=None):
            r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 401


# ── Token resolution ──────────────────────────────────────────────────────────
class TestResolveUserId:
    def test_returns_user_id_when_valid(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = _token_query_result()
            assert m._resolve_user_id(ACCESS_TOKEN) == USER_ID

    def test_returns_none_when_not_found(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            assert m._resolve_user_id("bad_token") is None

    def test_returns_none_when_expired(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = _token_query_result(
                expires_offset=-100
            )
            assert m._resolve_user_id(ACCESS_TOKEN) is None

    def test_returns_none_on_ddb_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.side_effect = ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "x"}}, "Query"
            )
            assert m._resolve_user_id(ACCESS_TOKEN) is None

    def test_queries_access_token_index(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            m._resolve_user_id(ACCESS_TOKEN)
        kw = mock_ddb.return_value.Table.return_value.query.call_args[1]
        assert kw["IndexName"] == "accessToken-index"


# ── SYNC intent ───────────────────────────────────────────────────────────────
class TestSync:
    def test_returns_200_with_devices(self):
        m = _load()
        fake_device = {"id": "ep1", "type": "action.devices.types.LIGHT",
                       "traits": ["action.devices.traits.OnOff"],
                       "name": {"name": "Bedroom Light", "nicknames": ["Bedroom Light"]},
                       "willReportState": True, "attributes": {}}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[fake_device]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["payload"]["agentUserId"] == USER_ID
        assert len(body["payload"]["devices"]) == 1
        assert body["payload"]["devices"][0]["id"] == "ep1"

    def test_empty_devices_when_none_found(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert json.loads(r["body"])["payload"]["devices"] == []

    def test_agent_user_id_matches(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert json.loads(r["body"])["payload"]["agentUserId"] == USER_ID

    def test_request_id_echoed(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert json.loads(r["body"])["requestId"] == "req-123"


# ── QUERY intent ──────────────────────────────────────────────────────────────
class TestQuery:
    def _query_event(self, device_ids: list[str]) -> dict:
        return _event("action.devices.QUERY",
                      {"devices": [{"id": d} for d in device_ids]})

    def test_scene_device_reports_off(self):
        m = _load()
        scene_device_id = f"scene::{SCENE_ID}"
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(self._query_event([scene_device_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][scene_device_id]
        assert state["on"] is False
        assert state["online"] is True

    def test_encrypted_device_returns_state(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        fake_state = {"onOff": 1, "level": 80}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value=fake_state):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert state["online"] is True
        assert state["on"] is True
        assert state["brightness"] == 80

    def test_bad_endpoint_id_returns_error(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(self._query_event(["not_a_valid_id"]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"]["not_a_valid_id"]
        assert state["online"] is False
        assert state["errorCode"] == "deviceNotFound"

    def test_cover_device_has_open_percent(self):
        m = _load()
        fake_key = b"\x00" * 32
        cover_type = 5
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, cover_type)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 2}):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert "openPercent" in state
        assert state["openPercent"] == 100

    def test_empty_device_list(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(self._query_event([]), MagicMock())
        assert json.loads(r["body"])["payload"]["devices"] == {}


# ── EXECUTE intent ────────────────────────────────────────────────────────────
class TestExecute:
    def _execute_event(self, device_ids: list[str], command: str, params: dict = None) -> dict:
        return _event("action.devices.EXECUTE", {
            "commands": [{
                "devices":   [{"id": d} for d in device_ids],
                "execution": [{"command": command, "params": params or {}}],
            }]
        })

    def test_on_off_command_publishes_mqtt(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command") as mock_pub:
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(
                            self._execute_event([ep_id], "action.devices.commands.OnOff",
                                               {"on": True}),
                            MagicMock(),
                        )
        assert r["statusCode"] == 200
        mock_pub.assert_called_once()
        _, _, _, _, state, _ = mock_pub.call_args[0]
        assert state["onOff"] is True

    def test_brightness_command_publishes_level(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command") as mock_pub:
                    with patch.object(m, "_update_device_state_db"):
                        m.lambda_handler(
                            self._execute_event([ep_id], "action.devices.commands.BrightnessAbsolute",
                                               {"brightness": 60}),
                            MagicMock(),
                        )
        _, _, _, _, state, _ = mock_pub.call_args[0]
        assert state["level"] == 60

    def test_scene_on_activates_scene(self):
        m = _load()
        scene_device_id = f"scene::{SCENE_ID}"
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_activate_scene") as mock_scene:
                r = m.lambda_handler(
                    self._execute_event([scene_device_id], "action.devices.commands.OnOff",
                                       {"on": True}),
                    MagicMock(),
                )
        mock_scene.assert_called_once_with(USER_ID, SCENE_ID)
        result = json.loads(r["body"])["payload"]["commands"][0]
        assert result["status"] == "SUCCESS"

    def test_scene_off_does_not_activate(self):
        m = _load()
        scene_device_id = f"scene::{SCENE_ID}"
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_activate_scene") as mock_scene:
                r = m.lambda_handler(
                    self._execute_event([scene_device_id], "action.devices.commands.OnOff",
                                       {"on": False}),
                    MagicMock(),
                )
        mock_scene.assert_not_called()
        assert r["statusCode"] == 200

    def test_bad_endpoint_id_returns_error(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(
                self._execute_event(["bad_id"], "action.devices.commands.OnOff", {"on": True}),
                MagicMock(),
            )
        results = json.loads(r["body"])["payload"]["commands"]
        error_result = next((x for x in results if x["status"] == "ERROR"), None)
        assert error_result is not None
        assert "bad_id" in error_result["ids"]

    def test_success_ids_and_error_ids_separated(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            good_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command"):
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(
                            self._execute_event([good_id, "bad_id"],
                                               "action.devices.commands.OnOff", {"on": True}),
                            MagicMock(),
                        )
        results = json.loads(r["body"])["payload"]["commands"]
        statuses = {x["status"]: x["ids"] for x in results}
        assert good_id in statuses["SUCCESS"]
        assert "bad_id" in statuses["ERROR"]


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
        assert json.loads(r["body"])["payload"] == {}

    def test_ddb_error_still_returns_200(self):
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
    def test_invalid_json_returns_400(self):
        m = _load()
        ev = {"headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"}, "body": "{{bad json"}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 400

    def test_null_body_returns_400(self):
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
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["headers"]["Content-Type"] == "application/json"

    def test_body_is_valid_json(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert isinstance(json.loads(r["body"]), dict)

    def test_response_contains_request_id(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert json.loads(r["body"])["requestId"] == "req-123"


# ── _build_iot_state unit tests ───────────────────────────────────────────────
class TestBuildIotState:
    def test_on_off_on(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.OnOff", {"on": True}, EP_TYPE, DUID, MAC, EP_ID)
        assert state == {"onOff": True}

    def test_on_off_off(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.OnOff", {"on": False}, EP_TYPE, DUID, MAC, EP_ID)
        assert state == {"onOff": False}

    def test_brightness(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.BrightnessAbsolute", {"brightness": 75},
                                    EP_TYPE, DUID, MAC, EP_ID)
        assert state["level"] == 75
        assert state["onOff"] is True

    def test_brightness_zero_sets_off(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.BrightnessAbsolute", {"brightness": 0},
                                    EP_TYPE, DUID, MAC, EP_ID)
        assert state["level"] == 0
        assert state["onOff"] is False

    def test_color_temperature(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.ColorAbsolute",
                                    {"color": {"temperatureK": 4000}},
                                    13, DUID, MAC, EP_ID)
        assert "colorTemp" in state
        assert state["onOff"] is True

    def test_cover_on_off_maps_to_open_close(self):
        m = _load()
        state_open  = m._build_iot_state("action.devices.commands.OnOff", {"on": True},
                                          5, DUID, MAC, EP_ID)
        state_close = m._build_iot_state("action.devices.commands.OnOff", {"on": False},
                                          5, DUID, MAC, EP_ID)
        assert state_open  == {"onOff": 2}
        assert state_close == {"onOff": 4}

    def test_fan_speed(self):
        m = _load()
        state = m._build_iot_state("action.devices.commands.SetFanSpeed", {"fanSpeed": "3"},
                                    3, DUID, MAC, EP_ID)
        assert state["level"] == 3

    def test_unknown_command_raises(self):
        m = _load()
        try:
            m._build_iot_state("action.devices.commands.Invalid", {}, EP_TYPE, DUID, MAC, EP_ID)
            assert False, "Expected ValueError"
        except ValueError:
            pass


# ── Fan speed helpers ─────────────────────────────────────────────────────────
class TestFanSpeedHelpers:
    def test_fan_speed_to_level(self):
        m = _load()
        assert m._fan_speed_to_level("1") == 1
        assert m._fan_speed_to_level("4") == 4

    def test_level_to_fan_speed(self):
        m = _load()
        assert m._level_to_fan_speed(1) == "1"
        assert m._level_to_fan_speed(4) == "4"
        assert m._level_to_fan_speed(0) == "1"
