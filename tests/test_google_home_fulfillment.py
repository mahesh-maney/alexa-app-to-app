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


# ── Additional coverage ───────────────────────────────────────────────────────

class TestBuildIotStateExtended:
    def test_hsv_color_command(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.ColorAbsolute",
            {"color": {"spectrumHsv": {"hue": 120.0, "saturation": 1.0, "value": 0.8}}},
            4, DUID, MAC, EP_ID  # RGB type
        )
        assert "hue" in state
        assert "saturation" in state
        assert state["onOff"] is True

    def test_rgb_color_command(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.ColorAbsolute",
            {"color": {"spectrumRgb": 0xFF0000}},  # red
            4, DUID, MAC, EP_ID
        )
        assert "hue" in state
        assert state["saturation"] == 100

    def test_rgb_black_sets_off(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.ColorAbsolute",
            {"color": {"spectrumRgb": 0x000000}},
            4, DUID, MAC, EP_ID
        )
        assert state["onOff"] is False

    def test_open_close_open(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.OpenClose",
            {"openPercent": 100},
            5, DUID, MAC, EP_ID
        )
        assert state == {"onOff": 2}

    def test_open_close_close(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.OpenClose",
            {"openPercent": 0},
            5, DUID, MAC, EP_ID
        )
        assert state == {"onOff": 4}

    def test_start_stop_start(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.StartStop",
            {"start": True},
            5, DUID, MAC, EP_ID
        )
        assert state == {"onOff": 2}

    def test_start_stop_stop(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.StartStop",
            {"start": False},
            5, DUID, MAC, EP_ID
        )
        assert state == {"onOff": 0}

    def test_pause_unpause(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.PauseUnpause",
            {},
            5, DUID, MAC, EP_ID
        )
        assert state == {"onOff": 0}

    def test_activate_scene_raises_value_error(self):
        m = _load()
        try:
            m._build_iot_state("action.devices.commands.ActivateScene", {}, EP_TYPE, DUID, MAC, EP_ID)
            assert False, "Expected ValueError"
        except ValueError as e:
            assert "scene" in str(e).lower()

    def test_fan_speed_all_levels(self):
        m = _load()
        for speed, expected_level in [("1", 1), ("2", 2), ("3", 3), ("4", 4)]:
            state = m._build_iot_state(
                "action.devices.commands.SetFanSpeed",
                {"fanSpeed": speed},
                3, DUID, MAC, EP_ID
            )
            assert state["level"] == expected_level

    def test_unknown_fan_speed_defaults(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.SetFanSpeed",
            {"fanSpeed": "99"},
            3, DUID, MAC, EP_ID
        )
        assert "level" in state

    def test_color_unsupported_format_raises(self):
        m = _load()
        try:
            m._build_iot_state(
                "action.devices.commands.ColorAbsolute",
                {"color": {}},  # no temperatureK, spectrumHsv, or spectrumRgb
                4, DUID, MAC, EP_ID
            )
            assert False, "Expected ValueError"
        except ValueError:
            pass

    def test_cover_on_off_true_maps_open(self):
        m = _load()
        state = m._build_iot_state(
            "action.devices.commands.OnOff",
            {"on": True},
            5, DUID, MAC, EP_ID  # cover
        )
        assert state == {"onOff": 2}


class TestBuildGhReturnState:
    def test_on_off_on(self):
        m = _load()
        assert m._build_gh_return_state("action.devices.commands.OnOff", {"on": True}) == {"on": True}

    def test_on_off_off(self):
        m = _load()
        assert m._build_gh_return_state("action.devices.commands.OnOff", {"on": False}) == {"on": False}

    def test_brightness_above_zero_is_on(self):
        m = _load()
        result = m._build_gh_return_state("action.devices.commands.BrightnessAbsolute", {"brightness": 50})
        assert result["brightness"] == 50
        assert result["on"] is True

    def test_brightness_zero_is_off(self):
        m = _load()
        result = m._build_gh_return_state("action.devices.commands.BrightnessAbsolute", {"brightness": 0})
        assert result["on"] is False

    def test_color_temperature_return(self):
        m = _load()
        result = m._build_gh_return_state(
            "action.devices.commands.ColorAbsolute",
            {"color": {"temperatureK": 4000}}
        )
        assert result["color"]["temperatureK"] == 4000

    def test_color_hsv_return(self):
        m = _load()
        hsv = {"hue": 120.0, "saturation": 1.0, "value": 0.8}
        result = m._build_gh_return_state(
            "action.devices.commands.ColorAbsolute",
            {"color": {"spectrumHsv": hsv}}
        )
        assert result["color"]["spectrumHsv"] == hsv

    def test_fan_speed_return(self):
        m = _load()
        result = m._build_gh_return_state(
            "action.devices.commands.SetFanSpeed", {"fanSpeed": "3"}
        )
        assert result["currentFanSpeedSetting"] == "3"

    def test_open_close_return(self):
        m = _load()
        result = m._build_gh_return_state(
            "action.devices.commands.OpenClose", {"openPercent": 75}
        )
        assert result["openPercent"] == 75

    def test_start_stop_return(self):
        m = _load()
        result = m._build_gh_return_state("action.devices.commands.StartStop", {})
        assert "isRunning" in result

    def test_unknown_command_returns_empty(self):
        m = _load()
        result = m._build_gh_return_state("action.devices.commands.Unknown", {})
        assert result == {}


class TestQueryExtended:
    def _query_event(self, device_ids):
        return _event("action.devices.QUERY",
                      {"devices": [{"id": d} for d in device_ids]})

    def test_cct_device_returns_color_temperature(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 13)  # CCT
        fake_state = {"onOff": 1, "level": 70, "colorTemp": 250}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value=fake_state):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert "color" in state
        assert "temperatureK" in state["color"]

    def test_rgb_device_returns_spectrum_hsv(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 4)  # RGB
        fake_state = {"onOff": 1, "level": 80, "hue": 120, "saturation": 100}
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value=fake_state):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert "color" in state
        assert "spectrumHsv" in state["color"]

    def test_fan_device_returns_fan_speed(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 3)  # FAN
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 1, "level": 3}):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert "currentFanSpeedSetting" in state

    def test_cover_closed_returns_zero_open_percent(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 5)  # COVER
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 4}):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert state["openPercent"] == 0

    def test_cover_stopped_returns_is_paused(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 5)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 0}):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert state["isPaused"] is True

    def test_multiple_devices_all_returned(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ids = [m._encrypt_endpoint(DUID, MAC, i, EP_TYPE) for i in range(1, 4)]
        scene_id = "scene::42"
        all_ids = ids + [scene_id]
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 1, "level": 50}):
                    r = m.lambda_handler(self._query_event(all_ids), MagicMock())
        states = json.loads(r["body"])["payload"]["devices"]
        assert len(states) == len(all_ids)

    def test_device_without_level_uses_on_off_for_on_state(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_get_current_state", return_value={"onOff": 1}):
                    r = m.lambda_handler(self._query_event([ep_id]), MagicMock())
        state = json.loads(r["body"])["payload"]["devices"][ep_id]
        assert state["on"] is True


class TestExecuteExtended:
    def _execute_event(self, device_ids, command, params=None):
        return _event("action.devices.EXECUTE", {
            "commands": [{
                "devices":   [{"id": d} for d in device_ids],
                "execution": [{"command": command, "params": params or {}}],
            }]
        })

    def test_color_temperature_command_publishes(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 13)  # CCT
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command") as mock_pub:
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(
                            self._execute_event(
                                [ep_id],
                                "action.devices.commands.ColorAbsolute",
                                {"color": {"temperatureK": 4000}}
                            ), MagicMock()
                        )
        assert r["statusCode"] == 200
        mock_pub.assert_called_once()

    def test_open_close_command_publishes(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 5)  # COVER
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command") as mock_pub:
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(
                            self._execute_event(
                                [ep_id],
                                "action.devices.commands.OpenClose",
                                {"openPercent": 100}
                            ), MagicMock()
                        )
        assert r["statusCode"] == 200
        mock_pub.assert_called_once()

    def test_fan_speed_command_publishes(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, 3)  # FAN
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command") as mock_pub:
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(
                            self._execute_event(
                                [ep_id],
                                "action.devices.commands.SetFanSpeed",
                                {"fanSpeed": "2"}
                            ), MagicMock()
                        )
        assert r["statusCode"] == 200
        mock_pub.assert_called_once()

    def test_multiple_executions_in_one_command_group(self):
        m = _load()
        fake_key = b"\x00" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            ep_id = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        ev = _event("action.devices.EXECUTE", {
            "commands": [
                {
                    "devices":   [{"id": ep_id}],
                    "execution": [
                        {"command": "action.devices.commands.OnOff", "params": {"on": True}},
                        {"command": "action.devices.commands.BrightnessAbsolute", "params": {"brightness": 80}},
                    ],
                }
            ]
        })
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                with patch.object(m, "_publish_device_command"):
                    with patch.object(m, "_update_device_state_db"):
                        r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 200

    def test_handler_exception_returns_500(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", side_effect=RuntimeError("boom")):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 500

    def test_execute_empty_commands_returns_200(self):
        m = _load()
        ev = _event("action.devices.EXECUTE", {"commands": []})
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            r = m.lambda_handler(ev, MagicMock())
        assert r["statusCode"] == 200


class TestFanSpeedHelpersExtended:
    def test_level_to_fan_speed_boundary_zero(self):
        m = _load()
        assert m._level_to_fan_speed(0) == "1"

    def test_level_to_fan_speed_boundary_high(self):
        m = _load()
        assert m._level_to_fan_speed(99) == "4"

    def test_fan_speed_unknown_defaults_to_2(self):
        m = _load()
        assert m._fan_speed_to_level("unknown") == 2


class TestHsvToHex:
    def test_red(self):
        m = _load()
        result = m._hsv_to_hex(0, 100, 100)
        assert result.upper() == "#FF0000"

    def test_black(self):
        m = _load()
        result = m._hsv_to_hex(0, 0, 0)
        assert result.upper() == "#000000"

    def test_white(self):
        m = _load()
        result = m._hsv_to_hex(0, 0, 100)
        assert result.upper() == "#FFFFFF"


class TestEndpointEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        m = _load()
        fake_key = b"\xAB" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            encrypted = m._encrypt_endpoint(DUID, MAC, 5, 3)
            duid, mac, epid, eptype = m._decrypt_endpoint(encrypted)
        assert duid == DUID
        assert mac == MAC
        assert epid == 5
        assert eptype == 3

    def test_different_endpoints_produce_different_ids(self):
        m = _load()
        fake_key = b"\xAB" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            id1 = m._encrypt_endpoint(DUID, MAC, 1, EP_TYPE)
            id2 = m._encrypt_endpoint(DUID, MAC, 2, EP_TYPE)
        assert id1 != id2

    def test_same_inputs_produce_same_id(self):
        """Deterministic: same plaintext → same ciphertext."""
        m = _load()
        fake_key = b"\xAB" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            id1 = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
            id2 = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        assert id1 == id2

    def test_tampered_id_raises_on_decrypt(self):
        m = _load()
        fake_key = b"\xAB" * 32
        with patch.object(m, "_get_endpoint_key", return_value=fake_key):
            encrypted = m._encrypt_endpoint(DUID, MAC, EP_ID, EP_TYPE)
        tampered = encrypted[:-4] + "XXXX"
        try:
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                m._decrypt_endpoint(tampered)
            assert False, "Expected an exception on tampered ciphertext"
        except Exception:
            pass


class TestSceneHelpers:
    def test_encode_scene_prefix(self):
        m = _load()
        assert m._encode_scene("42") == "scene::42"

    def test_parse_scene_endpoint_id_valid(self):
        m = _load()
        assert m._parse_scene_endpoint_id("scene::99") == "99"

    def test_parse_scene_endpoint_id_non_scene_returns_none(self):
        m = _load()
        assert m._parse_scene_endpoint_id("not_a_scene") is None

    def test_parse_scene_endpoint_id_empty(self):
        m = _load()
        assert m._parse_scene_endpoint_id("") is None


# ── S3/IoT integration helpers ─────────────────────────────────────────────────

def _s3_page(keys: list[str]) -> dict:
    """Build a fake paginator page for list_objects_v2."""
    return {"Contents": [{"Key": k} for k in keys]}


def _make_s3_mock(pages: dict[str, bytes]):
    """
    Returns a mock s3_client where:
      - get_paginator("list_objects_v2") paginates over pages matching prefix
      - get_object(Key=k) returns pages[k]
    """
    from unittest.mock import MagicMock

    s3 = MagicMock()
    all_keys = list(pages.keys())

    def fake_paginator(op):
        assert op == "list_objects_v2"
        pag = MagicMock()
        def paginate(Bucket, Prefix, **kw):
            matching = [k for k in all_keys if k.startswith(Prefix)]
            return iter([{"Contents": [{"Key": k} for k in matching]}])
        pag.paginate.side_effect = paginate
        return pag

    def fake_get_object(Bucket, Key, **kw):
        body_mock = MagicMock()
        body_mock.read.return_value = pages[Key]
        return {"Body": body_mock}

    def fake_list_objects_v2(Bucket, Prefix, **kw):
        matching = [k for k in all_keys if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in matching]}

    s3.get_paginator.side_effect = fake_paginator
    s3.get_object.side_effect = fake_get_object
    s3.list_objects_v2.side_effect = fake_list_objects_v2
    return s3


# ── Load zone map ──────────────────────────────────────────────────────────────
class TestLoadZoneMap:
    def test_returns_zone_map_from_s3(self):
        m = _load()
        zone_data = json.dumps({"zoneId": "1", "zoneName": "Living Room"}).encode()
        pages = {f"{USER_ID}/site1/zones/z1/zone_data.json": zone_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            result = m._load_zone_map(USER_ID, "site1")
        assert result == {"1": "Living Room"}

    def test_skips_non_zone_data_files(self):
        m = _load()
        pages = {
            f"{USER_ID}/site1/zones/z1/zone_data.json": json.dumps({"zoneId": "1", "zoneName": "Bedroom"}).encode(),
            f"{USER_ID}/site1/zones/z1/other.json": b'{"zoneId": "2", "zoneName": "ignore"}',
        }
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            result = m._load_zone_map(USER_ID, "site1")
        assert "1" in result
        assert "2" not in result

    def test_returns_empty_on_s3_error(self):
        m = _load()
        mock_s3 = MagicMock()
        mock_s3.get_paginator.side_effect = Exception("S3 failure")
        with patch.object(m, "_s3_client", return_value=mock_s3):
            result = m._load_zone_map(USER_ID, "site1")
        assert result == {}

    def test_skips_zone_with_missing_id_or_name(self):
        m = _load()
        pages = {
            f"{USER_ID}/site1/zones/z1/zone_data.json": json.dumps({"zoneId": "", "zoneName": "NoId"}).encode(),
            f"{USER_ID}/site1/zones/z2/zone_data.json": json.dumps({"zoneId": "5", "zoneName": ""}).encode(),
        }
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            result = m._load_zone_map(USER_ID, "site1")
        assert result == {}


# ── Build device endpoints gh ──────────────────────────────────────────────────
class TestBuildDeviceEndpointsGh:
    def _ep_json(self, ep_id=1, ep_type=2, name="Light", ieee="aa:bb:cc:dd:ee:ff", zone_id="1"):
        return json.dumps([{
            "endpointId": ep_id,
            "endpointType": ep_type,
            "endpointName": name,
            "subdeviceIeeeAddress": ieee,
            "zoneId": zone_id,
            "isHidden": False,
        }]).encode()

    def test_returns_device_for_valid_endpoint(self):
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = self._ep_json()
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID, {"1": "Living"})
        assert len(result) == 1
        assert result[0]["type"] == "action.devices.types.LIGHT"

    def test_skips_hidden_endpoint(self):
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = json.dumps([{
            "endpointId": 1, "endpointType": 2, "endpointName": "Hidden",
            "subdeviceIeeeAddress": MAC, "isHidden": True,
        }]).encode()
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID)
        assert result == []

    def test_skips_excluded_ep_types(self):
        """ep_type 15, 20, 1000 are excluded."""
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = json.dumps([
            {"endpointId": 1, "endpointType": 15, "endpointName": "X",
             "subdeviceIeeeAddress": MAC, "isHidden": False},
        ]).encode()
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID)
        assert result == []

    def test_skips_endpoint_without_ieee_addr(self):
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = json.dumps([{
            "endpointId": 1, "endpointType": 2, "endpointName": "Light",
            "subdeviceIeeeAddress": "", "isHidden": False,
        }]).encode()
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID)
        assert result == []

    def test_returns_empty_list_on_exception(self):
        m = _load()
        mock_s3 = MagicMock()
        mock_s3.get_paginator.side_effect = Exception("broken")
        with patch.object(m, "_s3_client", return_value=mock_s3):
            result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID)
        assert result == []

    def test_room_hint_set_when_zone_mapped(self):
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = self._ep_json(zone_id="7")
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID, {"7": "Bedroom"})
        assert result[0].get("roomHint") == "Bedroom"

    def test_multi_site_prefixes_name_with_site(self):
        m = _load()
        fake_key = b"\x00" * 32
        ep_data = self._ep_json(name="Ceiling Fan")
        pages = {f"{USER_ID}/site1/devices/{DUID}/ep/endpoints.json": ep_data}
        mock_s3 = _make_s3_mock(pages)
        with patch.object(m, "_s3_client", return_value=mock_s3):
            with patch.object(m, "_get_endpoint_key", return_value=fake_key):
                result = m._build_device_endpoints_gh(USER_ID, "site1", "Home", DUID, {}, multi_site=True)
        assert "Home" in result[0]["name"]["name"]
        assert "Ceiling Fan" in result[0]["name"]["name"]


# ── Get all GH devices ─────────────────────────────────────────────────────────
class TestGetAllGhDevices:
    def _site_item(self, site_id="site1", site_name="Home", duid=DUID):
        return {"userId": USER_ID, "siteId": site_id, "siteName": site_name,
                "devices": [{"duid": duid}]}

    def test_returns_empty_list_when_no_sites(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            result = m._get_all_gh_devices(USER_ID)
        assert result == []

    def test_returns_devices_from_single_site(self):
        m = _load()
        fake_device = {"id": "dev-1", "type": "action.devices.types.LIGHT",
                       "traits": [], "name": {"name": "Light"}, "willReportState": True}
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [self._site_item()]
            }
            with patch.object(m, "_load_zone_map", return_value={}):
                with patch.object(m, "_build_device_endpoints_gh", return_value=[fake_device]):
                    mock_s3 = MagicMock()
                    mock_s3.get_object.side_effect = Exception("NoSuchKey")
                    with patch.object(m, "_s3_client", return_value=mock_s3):
                        result = m._get_all_gh_devices(USER_ID)
        assert len(result) == 1
        assert result[0]["id"] == "dev-1"

    def test_includes_scenes_from_s3(self):
        m = _load()
        scenes_json = json.dumps([{"sceneId": "99", "sceneName": "Movie Night"}]).encode()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [self._site_item()]
            }
            with patch.object(m, "_load_zone_map", return_value={}):
                with patch.object(m, "_build_device_endpoints_gh", return_value=[]):
                    mock_s3 = MagicMock()
                    body = MagicMock()
                    body.read.return_value = scenes_json
                    mock_s3.get_object.return_value = {"Body": body}
                    with patch.object(m, "_s3_client", return_value=mock_s3):
                        result = m._get_all_gh_devices(USER_ID)
        scene_items = [d for d in result if d.get("id", "").startswith("scene::")]
        assert len(scene_items) == 1
        assert scene_items[0]["name"]["name"] == "Movie Night"

    def test_deduplicates_scenes_across_sites(self):
        m = _load()
        scenes_json = json.dumps([{"sceneId": "5", "sceneName": "Party"}]).encode()
        sites = [
            self._site_item("site1", "Home1", "duid1"),
            self._site_item("site2", "Home2", "duid2"),
        ]
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": sites}
            with patch.object(m, "_load_zone_map", return_value={}):
                with patch.object(m, "_build_device_endpoints_gh", return_value=[]):
                    mock_s3 = MagicMock()
                    body = MagicMock()
                    body.read.return_value = scenes_json
                    mock_s3.get_object.return_value = {"Body": body}
                    with patch.object(m, "_s3_client", return_value=mock_s3):
                        result = m._get_all_gh_devices(USER_ID)
        scene_ids = [d["id"] for d in result if d.get("id", "").startswith("scene::")]
        assert len(scene_ids) == len(set(scene_ids))  # no duplicates


# ── Handle sync intent ─────────────────────────────────────────────────────────
class TestHandleSync:
    def test_sync_returns_all_devices(self):
        m = _load()
        fake_devices = [
            {"id": "d1", "type": "action.devices.types.LIGHT",
             "traits": [], "name": {"name": "L1"}, "willReportState": True},
            {"id": "d2", "type": "action.devices.types.SWITCH",
             "traits": [], "name": {"name": "S1"}, "willReportState": True},
        ]
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=fake_devices):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 200
        body = json.loads(r["body"])
        assert body["payload"]["agentUserId"] == USER_ID
        assert len(body["payload"]["devices"]) == 2

    def test_sync_returns_empty_when_no_devices(self):
        m = _load()
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_get_all_gh_devices", return_value=[]):
                r = m.lambda_handler(_event("action.devices.SYNC"), MagicMock())
        assert r["statusCode"] == 200
        assert json.loads(r["body"])["payload"]["devices"] == []


# ── Publish device command ─────────────────────────────────────────────────────
class TestPublishDeviceCommand:
    def test_publishes_to_iot_topic(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, EP_ID, EP_TYPE, {"onOff": True}, USER_ID)
        assert mock_iot.publish.called
        call_kwargs = mock_iot.publish.call_args[1]
        assert call_kwargs["topic"] == f"iot/device/{DUID}/request"
        payload = json.loads(call_kwargs["payload"])
        assert "cmdType" in payload

    def test_on_off_true_uses_cmd_type_131(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, 1, 2, {"onOff": True}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["cmdType"] == 131

    def test_on_off_false_uses_cmd_type_132(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, 1, 2, {"onOff": False}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["cmdType"] == 132

    def test_level_state_uses_cmd_type_108(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, 1, 2, {"level": 80}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["cmdType"] == 108

    def test_color_temp_uses_cmd_type_109(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, 1, 13, {"colorTemp": 250}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["cmdType"] == 109

    def test_rgb_color_uses_cmd_type_139(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value=""):
                m._publish_device_command(DUID, MAC, 1, 4, {"hue": 120, "saturation": 100}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["cmdType"] == 139

    def test_includes_bridge_addr_when_available(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_iot_client", return_value=mock_iot):
            with patch.object(m, "_get_bridge_address", return_value="bridge-12"):
                m._publish_device_command(DUID, MAC, 1, 2, {"onOff": True}, USER_ID)
        payload = json.loads(mock_iot.publish.call_args[1]["payload"])
        assert payload["msgPayload"].get("zigbeeBridgeAddress") == "bridge-12"


# ── Update device state DB ─────────────────────────────────────────────────────
class TestUpdateDeviceStateDb:
    def test_calls_update_item(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            m._update_device_state_db(DUID, MAC, EP_ID, {"onOff": 1, "level": 80})
            mock_ddb.return_value.Table.return_value.update_item.assert_called_once()

    def test_does_not_raise_on_ddb_failure(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.update_item.side_effect = Exception("DDB fail")
            # should not raise
            m._update_device_state_db(DUID, MAC, EP_ID, {"onOff": 1})


# ── Get bridge address ─────────────────────────────────────────────────────────
class TestGetBridgeAddress:
    def test_returns_bridge_from_s3(self):
        m = _load()
        # key structure: userId/siteId/devices/duid/bridgeAddr/someFile
        prefix = f"{USER_ID}/site1/devices/{DUID}/"
        s3_key = f"{prefix}bridge-aabb/sub/path/file.json"
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [{"userId": USER_ID, "siteId": "site1", "devices": [{"duid": DUID}]}]
            }
            mock_s3 = MagicMock()
            mock_s3.list_objects_v2.return_value = {
                "Contents": [{"Key": s3_key}]
            }
            with patch.object(m, "_s3_client", return_value=mock_s3):
                result = m._get_bridge_address(USER_ID, DUID)
        assert result == "bridge-aabb"

    def test_returns_empty_string_on_s3_error(self):
        m = _load()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [{"userId": USER_ID, "siteId": "site1", "devices": [{"duid": DUID}]}]
            }
            mock_s3 = MagicMock()
            mock_s3.list_objects_v2.side_effect = Exception("S3 error")
            with patch.object(m, "_s3_client", return_value=mock_s3):
                result = m._get_bridge_address(USER_ID, DUID)
        assert result == ""

    def test_returns_cached_result_second_call(self):
        m = _load()
        m._bridge_cache[DUID] = "cached-bridge"
        result = m._get_bridge_address(USER_ID, DUID)
        assert result == "cached-bridge"


# ── Activate scene ─────────────────────────────────────────────────────────────
class TestActivateScene:
    def test_publishes_to_scene_topic(self):
        m = _load()
        mock_iot = MagicMock()
        scene_json = json.dumps([{"sceneId": 42, "groupId": 5,
                                   "subdevicesMappedList": [{"deviceBridgeAddress": "br-01"}]}]).encode()
        s3_body = MagicMock()
        s3_body.read.return_value = scene_json
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": s3_body}
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [{"sceneId": "42", "siteId": "site1", "duid": DUID}]
            }
            with patch.object(m, "_s3_client", return_value=mock_s3):
                with patch.object(m, "_iot_client", return_value=mock_iot):
                    m._activate_scene(USER_ID, "42")
        assert mock_iot.publish.called
        call_kw = mock_iot.publish.call_args[1]
        assert call_kw["topic"] == f"iot/device/{DUID}/scene"
        payload = json.loads(call_kw["payload"])
        assert payload["cmdType"] == 130
        assert payload["msgPayload"]["sceneId"] == 42

    def test_does_nothing_when_scene_not_found(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_ddb") as mock_ddb:
            mock_ddb.return_value.Table.return_value.query.return_value = {"Items": []}
            with patch.object(m, "_iot_client", return_value=mock_iot):
                m._activate_scene(USER_ID, "99")
        mock_iot.publish.assert_not_called()

    def test_does_nothing_when_no_duid_for_scene(self):
        m = _load()
        mock_iot = MagicMock()
        with patch.object(m, "_ddb") as mock_ddb:
            # scene record has no duid, and UDD query also returns nothing
            mock_ddb.return_value.Table.return_value.query.return_value = {
                "Items": [{"sceneId": "42", "siteId": "site1"}]  # no duid
            }
            mock_ddb.return_value.Table.return_value.get_item.return_value = {"Item": {}}
            with patch.object(m, "_iot_client", return_value=mock_iot):
                with patch.object(m, "_s3_client", return_value=MagicMock()):
                    m._activate_scene(USER_ID, "42")
        mock_iot.publish.assert_not_called()


# ── Execute with scene ─────────────────────────────────────────────────────────
class TestExecuteSceneActivation:
    def _execute_event_with_scene(self, scene_ep_id: str, on: bool = True) -> dict:
        return {
            "httpMethod": "POST",
            "headers": {"Authorization": f"Bearer {ACCESS_TOKEN}"},
            "body": json.dumps({
                "requestId": "req-scene",
                "inputs": [{
                    "intent": "action.devices.EXECUTE",
                    "payload": {
                        "commands": [{
                            "devices": [{"id": scene_ep_id}],
                            "execution": [{
                                "command": "action.devices.commands.OnOff",
                                "params": {"on": on},
                            }],
                        }],
                    },
                }],
            }),
        }

    def test_scene_on_off_on_activates_scene(self):
        m = _load()
        scene_ep = m._encode_scene("42")
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_activate_scene") as mock_activate:
                r = m.lambda_handler(self._execute_event_with_scene(scene_ep, on=True), MagicMock())
        assert r["statusCode"] == 200
        mock_activate.assert_called_once_with(USER_ID, "42")

    def test_scene_on_off_off_skips_activate(self):
        m = _load()
        scene_ep = m._encode_scene("42")
        with patch.object(m, "_resolve_user_id", return_value=USER_ID):
            with patch.object(m, "_activate_scene") as mock_activate:
                r = m.lambda_handler(self._execute_event_with_scene(scene_ep, on=False), MagicMock())
        assert r["statusCode"] == 200
        mock_activate.assert_not_called()


# ── Build GH device object ─────────────────────────────────────────────────────
class TestBuildGhDevice:
    def test_fan_device_has_fan_speed_trait(self):
        m = _load()
        device = m._build_gh_device("Fan", 3, "ep-1", "Home", "Digilux")
        assert "action.devices.traits.FanSpeed" in device["traits"]
        assert device["type"] == "action.devices.types.FAN"

    def test_switch_device(self):
        m = _load()
        # ep_type not in any special set → SWITCH fallback
        device = m._build_gh_device("Switch", 99, "ep-99", "Home", "Digilux")
        assert device["type"] == "action.devices.types.SWITCH"

    def test_cct_only_device_has_color_setting(self):
        m = _load()
        device = m._build_gh_device("CCT Light", 13, "ep-2", "Home", "Digilux")
        assert "action.devices.traits.ColorSetting" in device["traits"]
        assert "colorTemperatureRange" in device.get("attributes", {})

    def test_rgb_only_device_has_color_setting(self):
        m = _load()
        device = m._build_gh_device("RGB Light", 4, "ep-3", "Home", "Digilux")
        assert "action.devices.traits.ColorSetting" in device["traits"]
        assert device["attributes"].get("colorModel") == "hsv"

    def test_rgb_cct_device_has_both_attributes(self):
        m = _load()
        device = m._build_gh_device("RGBCCT Light", 14, "ep-4", "Home", "Digilux")
        attrs = device.get("attributes", {})
        assert attrs.get("colorModel") == "hsv"
        assert "colorTemperatureRange" in attrs

    def test_cover_device_is_blinds_type(self):
        m = _load()
        device = m._build_gh_device("Blind", 5, "ep-5", "Home", "Digilux")
        assert device["type"] == "action.devices.types.BLINDS"
        assert "action.devices.traits.OpenClose" in device["traits"]

    def test_room_hint_absent_when_no_hint(self):
        m = _load()
        device = m._build_gh_device("Light", 2, "ep-6", "Home", "Digilux")
        assert "roomHint" not in device

    def test_room_hint_set_when_provided(self):
        m = _load()
        device = m._build_gh_device("Light", 2, "ep-7", "Home", "Digilux", room_hint="Kitchen")
        assert device.get("roomHint") == "Kitchen"


# ── Config error paths (authorize, callback, token lambdas) ───────────────────
class TestCognitoCallbackConfigErrors:
    """Test that cognito_callback returns 500 for missing critical config."""
    def _load_cb(self):
        mod_name = "gh_oauth_callback_fn"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(
            mod_name,
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "lambdas", "google_home_oauth_cognito_callback", "lambda_function.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[mod_name] = mod
        return mod

    def test_missing_cognito_callback_uri_returns_500(self):
        orig = os.environ.pop("COGNITO_CALLBACK_URI", "")
        try:
            m = self._load_cb()
            r = m.lambda_handler({"httpMethod": "GET", "queryStringParameters": {}, "headers": {}},
                                 MagicMock())
            assert r["statusCode"] == 500
        finally:
            if orig:
                os.environ["COGNITO_CALLBACK_URI"] = orig


# ── Token lambda config error paths ───────────────────────────────────────────
class TestTokenConfigErrors:
    def _load_token(self):
        mod_name = "gh_oauth_token_fn_cfg"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(
            mod_name,
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "lambdas", "google_home_oauth_token", "lambda_function.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[mod_name] = mod
        return mod

    def test_invalid_credentials_returns_invalid_client(self):
        import urllib.parse
        m = self._load_token()
        with patch.object(m, "_get_gh_credentials", return_value={"client_id": "x", "client_secret": "y"}):
            body = urllib.parse.urlencode({
                "grant_type": "authorization_code",
                "client_id": "wrong-id",
                "client_secret": "wrong-secret",
                "code": "some-code",
            })
            r = m.lambda_handler({"httpMethod": "POST", "headers": {}, "body": body}, MagicMock())
        assert json.loads(r["body"])["error"] == "invalid_client"
