"""
google_home_fulfillment — POST /google-home/fulfillment

Google Smart Home fulfillment webhook. Google calls this endpoint whenever a
user issues a voice command ("Hey Google, turn on the lights") or when Google
needs to discover devices (SYNC) or read their state (QUERY).

Authentication: Google sends our access token as "Authorization: Bearer <token>".
We look up the token in DDB (GSI: accessToken-index) to identify the user.

Supported intents:
  action.devices.SYNC      — return user's device list
  action.devices.QUERY     — return device states
  action.devices.EXECUTE   — send commands to devices
  action.devices.DISCONNECT — user unlinked from Google Home (clean up tokens)

Device integration:
  SYNC reads from digilux_honeywell_user_device_mapping to discover sites.
  QUERY/EXECUTE require integration with the Digilux device control API.
  This initial implementation returns the site list for SYNC and stubs
  QUERY/EXECUTE — extend these as device control APIs are connected.
"""
from __future__ import annotations

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)


# ── Environment variables ──────────────────────────────────────────────────────
_REGION              = os.environ.get("DATA_REGION", "")
_TOKENS_TABLE        = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",
                                       "digilux_honeywell_user_device_mapping")

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


# ── Token → userId resolution ──────────────────────────────────────────────────

def _resolve_user_id(access_token: str) -> str | None:
    """
    Look up userId from our access token using the accessToken-index GSI.
    Returns userId string on success, None if not found or expired.
    """
    try:
        resp = _ddb().Table(_TOKENS_TABLE).query(
            IndexName="accessToken-index",
            KeyConditionExpression="accessToken = :at",
            ExpressionAttributeValues={":at": access_token},
            Limit=1,
        )
        items = resp.get("Items", [])
    except ClientError as exc:
        logger.error("DDB_RESOLVE_TOKEN_ERROR error=%s", exc)
        return None

    if not items:
        logger.warning("TOKEN_NOT_FOUND access_token=%s...", access_token[:8])
        return None

    record = items[0]

    # Check if access token has expired
    expires_at = int(record.get("expiresAt", 0))
    if expires_at and expires_at < int(time.time()):
        logger.warning("ACCESS_TOKEN_EXPIRED userId=%s expires_at=%d",
                       record.get("userId"), expires_at)
        return None

    user_id = record.get("userId", "")
    logger.debug("TOKEN_RESOLVED userId=%s", user_id)
    return user_id or None


# ── Device discovery (SYNC) ────────────────────────────────────────────────────

def _get_user_devices(user_id: str) -> list[dict]:
    """
    Fetch the user's sites from digilux_honeywell_user_device_mapping and
    represent each site as a Google Home device (type: scene/thermostat/etc.).

    Returns a list of Google Home device descriptors.
    """
    try:
        resp = _ddb().Table(_DEVICE_MAPPING_TABLE).query(
            KeyConditionExpression="userId = :uid",
            ExpressionAttributeValues={":uid": user_id},
        )
        sites = resp.get("Items", [])
    except ClientError as exc:
        logger.error("DDB_GET_DEVICES_ERROR error=%s userId=%s", exc, user_id)
        return []

    if not sites:
        logger.info("SYNC_NO_DEVICES userId=%s", user_id)
        return []

    devices = []
    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", site_id) or site_id
        if not site_id:
            continue

        # Represent each site as a Smart Home Scene device.
        # Extend this to add thermostats, lights, etc. as device APIs are integrated.
        devices.append({
            "id":   f"site:{site_id}",
            "type": "action.devices.types.SCENE",
            "traits": [
                "action.devices.traits.Scene",
            ],
            "name": {
                "defaultNames": [site_name],
                "name":         site_name,
                "nicknames":    [site_name],
            },
            "willReportState": False,
            "attributes": {
                "sceneReversible": False,
            },
            "deviceInfo": {
                "manufacturer": "Digilux",
            },
            "customData": {
                "siteId": site_id,
            },
        })

    logger.info("SYNC_DEVICES_FOUND userId=%s count=%d", user_id, len(devices))
    return devices


# ── Intent handlers ────────────────────────────────────────────────────────────

def _handle_sync(payload: dict, user_id: str, request_id: str) -> dict:
    """Return the full device list for this user."""
    logger.info("INTENT_SYNC userId=%s request_id=%s", user_id, request_id)
    devices = _get_user_devices(user_id)
    _audit("GH_SYNC", userId=user_id, device_count=len(devices), request_id=request_id)
    return {
        "requestId": request_id,
        "payload": {
            "agentUserId": user_id,
            "devices":     devices,
        },
    }


def _handle_query(payload: dict, user_id: str, request_id: str) -> dict:
    """Return the current state of requested devices."""
    logger.info("INTENT_QUERY userId=%s request_id=%s", user_id, request_id)
    devices_in = (payload.get("devices") or [])
    device_states: dict = {}

    for dev in devices_in:
        dev_id = dev.get("id", "")
        if not dev_id:
            continue
        # TODO: query actual device state from Digilux device control API
        # For now, return online=true as a placeholder
        device_states[dev_id] = {
            "online": True,
            "status": "SUCCESS",
        }
        logger.debug("QUERY_DEVICE_PLACEHOLDER dev_id=%s", dev_id)

    _audit("GH_QUERY", userId=user_id, device_count=len(device_states), request_id=request_id)
    return {
        "requestId": request_id,
        "payload": {
            "devices": device_states,
        },
    }


def _handle_execute(payload: dict, user_id: str, request_id: str) -> dict:
    """Execute commands on devices."""
    logger.info("INTENT_EXECUTE userId=%s request_id=%s", user_id, request_id)
    commands_in = payload.get("commands") or []
    results = []

    for cmd_group in commands_in:
        devices_in    = cmd_group.get("devices", [])
        execution_cmds = cmd_group.get("execution", [])

        for dev in devices_in:
            dev_id = dev.get("id", "")
            logger.info("EXECUTE_CMD dev_id=%s commands=%s userId=%s request_id=%s",
                        dev_id, [c.get("command") for c in execution_cmds],
                        user_id, request_id)
            # TODO: send command to Digilux device control API
            # For now, acknowledge with SUCCESS
            results.append({
                "ids":    [dev_id],
                "status": "SUCCESS",
                "states": {"online": True},
            })
            _audit("GH_EXECUTE_CMD", userId=user_id, dev_id=dev_id,
                   commands=str([c.get("command") for c in execution_cmds]),
                   request_id=request_id)

    return {
        "requestId": request_id,
        "payload": {
            "commands": results,
        },
    }


def _handle_disconnect(payload: dict, user_id: str, request_id: str) -> dict:
    """
    User disconnected from Google Home. Delete the token record to unlink.
    This is Google's reliable disconnect intent — always fires when user removes
    the integration from Google Home.
    """
    logger.info("INTENT_DISCONNECT userId=%s request_id=%s", user_id, request_id)
    try:
        _ddb().Table(_TOKENS_TABLE).delete_item(Key={"userId": user_id})
        logger.info("GH_DISCONNECT_OK userId=%s", user_id)
        _audit("GH_DISCONNECT", userId=user_id, outcome="success", request_id=request_id)
    except ClientError as exc:
        logger.error("GH_DISCONNECT_DDB_ERROR error=%s userId=%s", exc, user_id)
        _audit("GH_DISCONNECT", userId=user_id, outcome="error", error=str(exc),
               request_id=request_id)
    # Google expects an empty payload on DISCONNECT
    return {"requestId": request_id, "payload": {}}


# ── Main handler ───────────────────────────────────────────────────────────────

_INTENT_HANDLERS = {
    "action.devices.SYNC":       _handle_sync,
    "action.devices.QUERY":      _handle_query,
    "action.devices.EXECUTE":    _handle_execute,
    "action.devices.DISCONNECT": _handle_disconnect,
}


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=google_home_fulfillment request_id=%s", request_id)

    if _missing_vars:
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate via Bearer token ──────────────────────────────────────
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        logger.warning("MISSING_BEARER_TOKEN request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    access_token = auth.split(" ", 1)[1].strip()
    user_id = _resolve_user_id(access_token)
    if not user_id:
        logger.warning("INVALID_OR_EXPIRED_TOKEN token=%s... request_id=%s",
                       access_token[:8], request_id)
        return _resp(401, {"error": "Unauthorized"})

    logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)

    # ── 2. Parse request body ──────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _resp(400, {"error": "Invalid JSON body"})

    # Override request_id with Google's requestId if present
    google_request_id = body.get("requestId", request_id)
    inputs = body.get("inputs", [])

    if not inputs:
        logger.warning("NO_INPUTS_IN_REQUEST userId=%s request_id=%s", user_id, request_id)
        return _resp(400, {"error": "No inputs in request"})

    # ── 3. Dispatch each intent ────────────────────────────────────────────────
    # Google sends one input per request in practice, but spec allows multiple
    first_input = inputs[0]
    intent      = first_input.get("intent", "")
    payload_in  = first_input.get("payload", {})

    logger.info("INTENT=%s userId=%s google_request_id=%s",
                intent, user_id, google_request_id)

    handler = _INTENT_HANDLERS.get(intent)
    if not handler:
        logger.warning("UNKNOWN_INTENT intent=%s userId=%s request_id=%s",
                       intent, user_id, request_id)
        return _resp(400, {"error": f"Unknown intent: {intent}"})

    try:
        result = handler(payload_in, user_id, google_request_id)
    except Exception as exc:
        logger.error("INTENT_HANDLER_ERROR intent=%s error=%s userId=%s",
                     intent, exc, user_id)
        return _resp(500, {"error": "Internal server error"})

    logger.info("REQUEST_OK function=google_home_fulfillment intent=%s userId=%s",
                intent, user_id)
    return _resp(200, result)


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
