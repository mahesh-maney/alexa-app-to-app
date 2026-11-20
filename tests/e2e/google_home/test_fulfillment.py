"""
E2E tests — Google Home fulfillment endpoint.

POST /api/google-home/fulfillment
Auth: Bearer <gh_access_token>  (issued by /token)

Covers:
  - Invalid token → 200 with errorCode=authFailure (Google spec requires 200)
  - SYNC → returns non-empty device list
  - QUERY → returns state for a device
  - EXECUTE (OnOff) → returns SUCCESS status
  - DISCONNECT → server accepts and status shows linked=false

Requires:
  - TEST_GH_ACCESS_TOKEN env var  OR  full link flow already run this session
  - TEST_DEVICE_ID env var for QUERY/EXECUTE tests
"""
from __future__ import annotations

import os
import uuid

import pytest
import requests

from tests.e2e.conftest import poll_until


pytestmark = pytest.mark.fulfillment


@pytest.fixture(scope="module")
def gh_token(cfg, api, auth_headers, site_id):
    """
    Returns a valid GH access token.
    Uses TEST_GH_ACCESS_TOKEN env var if set, otherwise skips.
    (Full browser-based token acquisition is in test_link_flow.py)
    """
    token = os.getenv("TEST_GH_ACCESS_TOKEN", "")
    if not token:
        pytest.skip(
            "TEST_GH_ACCESS_TOKEN not set. "
            "Run test_link_flow.py::test_full_oauth_link_flow first and set "
            "the returned access_token as TEST_GH_ACCESS_TOKEN."
        )
    return token


@pytest.fixture(scope="module")
def fulfillment_url(cfg):
    return f"{cfg.api_base}/api/google-home/fulfillment"


def _intent(intent_type: str, payload: dict | None = None) -> dict:
    return {
        "requestId": str(uuid.uuid4()),
        "inputs": [{"intent": intent_type, "payload": payload or {}}],
    }


class TestFulfillmentAuth:

    def test_missing_token_returns_auth_failure(self, fulfillment_url):
        r = requests.post(
            fulfillment_url,
            json=_intent("action.devices.SYNC"),
            timeout=10,
        )
        # Google spec: fulfillment always returns 200, errors go in payload
        assert r.status_code == 200
        assert r.json()["payload"]["errorCode"] == "authFailure"

    def test_invalid_token_returns_auth_failure(self, fulfillment_url):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": "Bearer invalid.access.token"},
            json=_intent("action.devices.SYNC"),
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["payload"]["errorCode"] == "authFailure"


class TestFulfillmentSync:

    def test_sync_returns_device_list(self, fulfillment_url, gh_token):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.SYNC"),
            timeout=15,
        )
        assert r.status_code == 200, f"SYNC failed: {r.text}"
        payload = r.json()["payload"]
        assert "devices" in payload, "SYNC response missing 'devices'"
        assert len(payload["devices"]) > 0, "SYNC returned empty device list"

    def test_sync_response_has_agent_user_id(self, fulfillment_url, gh_token):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.SYNC"),
            timeout=15,
        )
        payload = r.json()["payload"]
        assert "agentUserId" in payload, "SYNC response missing 'agentUserId'"

    def test_sync_devices_have_required_fields(self, fulfillment_url, gh_token):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.SYNC"),
            timeout=15,
        )
        for device in r.json()["payload"]["devices"]:
            assert "id"     in device, f"Device missing 'id': {device}"
            assert "type"   in device, f"Device missing 'type': {device}"
            assert "traits" in device, f"Device missing 'traits': {device}"
            assert "name"   in device, f"Device missing 'name': {device}"


class TestFulfillmentQuery:

    def test_query_returns_device_state(self, fulfillment_url, gh_token, device_id):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.QUERY", {
                "devices": [{"id": device_id}],
            }),
            timeout=15,
        )
        assert r.status_code == 200, f"QUERY failed: {r.text}"
        devices = r.json()["payload"]["devices"]
        assert device_id in devices, f"Device {device_id!r} not in QUERY response"
        assert devices[device_id].get("status") != "ERROR", (
            f"Device returned ERROR status: {devices[device_id]}"
        )


class TestFulfillmentExecute:

    def test_execute_turn_on_returns_success(self, fulfillment_url, gh_token, device_id):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.EXECUTE", {
                "commands": [{
                    "devices": [{"id": device_id}],
                    "execution": [{
                        "command": "action.devices.commands.OnOff",
                        "params":  {"on": True},
                    }],
                }],
            }),
            timeout=15,
        )
        assert r.status_code == 200, f"EXECUTE failed: {r.text}"
        commands = r.json()["payload"]["commands"]
        assert len(commands) > 0
        assert commands[0]["status"] == "SUCCESS", (
            f"EXECUTE did not return SUCCESS: {commands[0]}"
        )

    def test_execute_turn_off_returns_success(self, fulfillment_url, gh_token, device_id):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.EXECUTE", {
                "commands": [{
                    "devices": [{"id": device_id}],
                    "execution": [{
                        "command": "action.devices.commands.OnOff",
                        "params":  {"on": False},
                    }],
                }],
            }),
            timeout=15,
        )
        assert r.status_code == 200
        assert r.json()["payload"]["commands"][0]["status"] == "SUCCESS"


class TestFulfillmentDisconnect:

    def test_disconnect_intent_accepted(self, fulfillment_url, gh_token,
                                        cfg, api, auth_headers, ensure_gh_unlinked):
        r = requests.post(
            fulfillment_url,
            headers={"Authorization": f"Bearer {gh_token}"},
            json=_intent("action.devices.DISCONNECT"),
            timeout=10,
        )
        assert r.status_code == 200, f"DISCONNECT failed: {r.text}"

        # After DISCONNECT, the app should see linked=false
        linked = poll_until(
            lambda: api.get(
                f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
                headers=auth_headers, timeout=10,
            ).json().get("linked"),
            expected=False,
        )
        assert linked is False, "linked=true even after DISCONNECT intent"
