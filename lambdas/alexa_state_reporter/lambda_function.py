"""
Proactive Alexa state change notifications (ChangeReport).

Triggered by DynamoDB Streams on device_state.
When a device state changes (e.g. light turned off from mobile app),
this Lambda tells Alexa immediately so the Alexa app reflects the new state.

Requires device_state records to contain userId (written by write_state_db
once mobile command Lambdas include userId in IoT payloads).
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from Crypto.Cipher import AES

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DATA_REGION       = os.environ.get("DATA_REGION", "ap-south-1")
_LWA_SECRET_ARN    = os.environ.get("LWA_SECRET_ARN", "")
_LWA_SECRET_REGION = os.environ.get("LWA_SECRET_REGION", "eu-west-1")
_KEY_SECRET_ARN    = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_KEY_SECRET_REGION = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")

_USER_DEVICE_TABLE = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_LWA_TOKENS_TABLE  = os.environ.get("LWA_TOKENS_TABLE", "alexa_lwa_tokens")
_METADATA_BUCKET   = os.environ.get("METADATA_BUCKET", "digilux-metadata-bucket")

# ── Device-type classification ────────────────────────────────────────────────
_LIGHT_TYPES = frozenset({2, 4, 13, 14})
_CCT_TYPES   = frozenset({13, 14})
_RGB_TYPES   = frozenset({4, 14})
_FAN_TYPES   = frozenset({3})
_COVER_TYPES = frozenset({5})

_GATEWAY_URL = os.environ.get("ALEXA_GATEWAY_URL", "https://api.eu.amazonalexa.com/v3/events")
_TOKEN_URL   = "https://api.amazon.com/auth/o2/token"

_lwa_creds: dict | None = None
_endpoint_key: bytes | None = None
_dynamodb = None
_s3 = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_DATA_REGION)
    return _dynamodb


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=_DATA_REGION)
    return _s3


def _get_lwa_creds() -> tuple[str, str]:
    global _lwa_creds
    if _lwa_creds is None:
        sm = boto3.client("secretsmanager", region_name=_LWA_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_LWA_SECRET_ARN)["SecretString"]
        _lwa_creds = json.loads(raw)
    return _lwa_creds["client_id"], _lwa_creds["client_secret"]


def _get_endpoint_key() -> bytes:
    global _endpoint_key
    if _endpoint_key is None:
        sm = boto3.client("secretsmanager", region_name=_KEY_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_KEY_SECRET_ARN)["SecretString"]
        _endpoint_key = base64.b64decode(json.loads(raw)["key"])
    return _endpoint_key


def _get_valid_token(user_id: str) -> str | None:
    table = _ddb().Table(_LWA_TOKENS_TABLE)
    resp = table.get_item(Key={"userId": user_id})
    item = resp.get("Item")
    if not item:
        return None
    if int(item["expiresAt"]) > time.time():
        return item["accessToken"]
    client_id, client_secret = _get_lwa_creds()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "refresh_token": item["refreshToken"],
        "client_id": client_id, "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        body = json.loads(r.read())
    new_token = body["access_token"]
    new_expiry = int(time.time()) + int(body.get("expires_in", 3600)) - 60
    table.update_item(Key={"userId": user_id},
        UpdateExpression="SET accessToken = :t, expiresAt = :e",
        ExpressionAttributeValues={":t": new_token, ":e": new_expiry})
    return new_token


def _encrypt_endpoint(duid: str, ieee_addr: str, ep_id: int, ep_type: int) -> str:
    plaintext = f"{duid}::{ieee_addr}::{ep_id}::{ep_type}".encode()
    key = _get_endpoint_key()
    nonce = hashlib.sha256(plaintext).digest()[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + ciphertext + tag).rstrip(b"=").decode()


def _find_user_for_duid(duid: str) -> str | None:
    """
    Reverse lookup: duid → userId by scanning user_device_details.
    devices is a List of Maps — DynamoDB contains() only works on strings/sets,
    so we scan all items and filter in Python.
    """
    table = _ddb().Table(_USER_DEVICE_TABLE)
    resp = table.scan(ProjectionExpression="userId, siteId, devices")
    for item in resp.get("Items", []):
        for d in item.get("devices", []):
            if d.get("duid") == duid:
                return item["userId"]
    return None


def _level_to_fan_mode(level: int) -> str:
    if level <= 33:
        return "FanSpeed.Low"
    if level <= 66:
        return "FanSpeed.Medium"
    return "FanSpeed.High"


def _state_to_properties(state: dict, ep_type: int) -> list[dict]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    props = []

    if "onOff" in state:
        props.append({
            "namespace": "Alexa.PowerController", "name": "powerState",
            "value": "ON" if state["onOff"] else "OFF",
            "timeOfSample": now, "uncertaintyInMilliseconds": 0,
        })
    if ep_type in _LIGHT_TYPES and "level" in state:
        props.append({
            "namespace": "Alexa.BrightnessController", "name": "brightness",
            "value": int(state["level"]),
            "timeOfSample": now, "uncertaintyInMilliseconds": 0,
        })
    if ep_type in _CCT_TYPES and state.get("colorTemp"):
        kelvin = int(1_000_000 / int(state["colorTemp"]))
        props.append({
            "namespace": "Alexa.ColorTemperatureController", "name": "colorTemperatureInKelvin",
            "value": kelvin, "timeOfSample": now, "uncertaintyInMilliseconds": 0,
        })
    if ep_type in _RGB_TYPES and state.get("hue") is not None and state.get("saturation") is not None:
        level = int(state.get("level", 0))
        props.append({
            "namespace": "Alexa.ColorController", "name": "color",
            "value": {
                "hue":        float(state["hue"]),
                "saturation": float(state["saturation"]) / 100.0,
                "brightness": float(level) / 100.0,
            },
            "timeOfSample": now, "uncertaintyInMilliseconds": 0,
        })
    if (ep_type in _FAN_TYPES or ep_type in _COVER_TYPES) and "level" in state:
        level = int(state["level"])
        props.append({
            "namespace": "Alexa.RangeController", "instance": "Digilux.Value",
            "name": "rangeValue", "value": level,
            "timeOfSample": now, "uncertaintyInMilliseconds": 0,
        })
        if ep_type in _FAN_TYPES:
            props.append({
                "namespace": "Alexa.ModeController", "instance": "Digilux.FanSpeed",
                "name": "mode", "value": _level_to_fan_mode(level),
                "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            })
    return props


def _get_ep_type_from_s3(user_id: str, duid: str, ieee_addr: str, ep_id: int) -> int | None:
    """Read ep_type from S3 endpoints.json.

    Returns None when the endpoint is not registered in the mobile app (phantom device),
    is hidden, or is a non-controllable type (coordinator/LQI). Only returns an int ep_type
    for endpoints that are actually registered in Alexa via AddOrUpdateReport.
    """
    try:
        table = _ddb().Table(_USER_DEVICE_TABLE)
        sites = table.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
        for site in sites:
            prefix = f"{user_id}/{site['siteId']}/devices/{duid}/"
            paginator = _s3_client().get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    if not obj["Key"].endswith("/endpoints.json"):
                        continue
                    resp = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=obj["Key"])
                    ep_list = json.loads(resp["Body"].read())
                    for ep in (ep_list if isinstance(ep_list, list) else []):
                        if (ep.get("subdeviceIeeeAddress") == ieee_addr
                                and int(ep.get("endpointId", -1)) == ep_id):
                            if ep.get("isHidden"):
                                return None
                            ep_type = int(ep.get("endpointType", 1))
                            if ep_type in {20, 1000}:  # LQI sensor and coordinator
                                return None
                            return ep_type
    except Exception as e:
        logger.warning("ep_type lookup failed: %s", e)
    return None  # not found in S3 = not registered in app, skip ChangeReport


def _send_change_report(lwa_token: str, endpoint_id: str, properties: list[dict]) -> None:
    payload = {
        "context": {},
        "event": {
            "header": {"namespace": "Alexa", "name": "ChangeReport",
                       "messageId": str(uuid.uuid4()), "payloadVersion": "3"},
            "endpoint": {
                "scope": {"type": "BearerToken", "token": lwa_token},
                "endpointId": endpoint_id,
            },
            "payload": {
                "change": {
                    "cause": {"type": "APP_INTERACTION"},
                    "properties": properties,
                }
            },
        },
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_GATEWAY_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {lwa_token}"},
        method="POST")
    logger.info("ChangeReport POST url=%s endpointId=%s", _GATEWAY_URL, endpoint_id)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 202:
                raise RuntimeError(f"Event Gateway returned {r.status}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Event Gateway {e.code}: {err_body}") from e
    logger.info("ChangeReport sent for endpointId=%s", endpoint_id)


def _send_delete_report(lwa_token: str, endpoint_ids: list[str]) -> None:
    """Alexa.Discovery.DeleteReport — explicitly removes stale endpoints from the
    customer's Alexa device list. Discover.Response simply omitting an endpointId is
    NOT sufficient to remove it (confirmed empirically 2026-08-22: re-running Discover
    after scoping endpoint building to the current bridge only did not clear stale
    duplicate devices already known to Alexa) — this is the only supported mechanism."""
    payload = {
        "event": {
            "header": {"namespace": "Alexa.Discovery", "name": "DeleteReport",
                       "messageId": str(uuid.uuid4()), "payloadVersion": "3"},
            "payload": {
                "endpoints": [{"endpointId": eid} for eid in endpoint_ids],
                "scope": {"type": "BearerToken", "token": lwa_token},
            },
        },
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_GATEWAY_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {lwa_token}"},
        method="POST")
    logger.info("DeleteReport POST url=%s endpointCount=%d", _GATEWAY_URL, len(endpoint_ids))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 202:
                raise RuntimeError(f"Event Gateway returned {r.status}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Event Gateway {e.code}: {err_body}") from e
    logger.info("DeleteReport sent for %d endpoints", len(endpoint_ids))


def _unmarshal_str(v: dict) -> str:
    return v.get("S", "")


def _unmarshal_map(v: dict) -> dict:
    return {k: list(vv.values())[0] for k, vv in v.get("M", {}).items()}


def lambda_handler(event, context):
    # One-off admin invocation: explicitly remove specific stale endpoints from a
    # customer's Alexa account (e.g. after a bridge re-pairing left old sub-devices
    # behind). Real DynamoDB Streams records always carry "Records", never "action",
    # so this can't collide with the normal ChangeReport flow below.
    if event.get("action") == "delete_stale_endpoints":
        user_id = event["userId"]
        duid = event["duid"]
        stale_devices = event["staleDevices"]  # [{ieee_addr, ep_id, ep_type}, ...]
        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            logger.error("No valid LWA token for userId=%s — cannot send DeleteReport", user_id)
            return {"statusCode": 400, "body": "no valid token"}
        endpoint_ids = [
            _encrypt_endpoint(duid, d["ieee_addr"], int(d["ep_id"]), int(d["ep_type"]))
            for d in stale_devices
        ]
        _send_delete_report(lwa_token, endpoint_ids)
        return {"statusCode": 200, "body": f"DeleteReport sent for {len(endpoint_ids)} endpoints"}

    for record in event.get("Records", []):
        if record.get("eventName") not in ("MODIFY", "INSERT"):
            continue

        new_img = record.get("dynamodb", {}).get("NewImage", {})
        old_img = record.get("dynamodb", {}).get("OldImage", {})

        duid = _unmarshal_str(new_img.get("deviceId", {}))
        if not duid:
            continue

        # userId stored directly in device_state if available; else reverse lookup
        user_id = _unmarshal_str(new_img.get("userId", {})) or _find_user_for_duid(duid)
        if not user_id:
            logger.warning("Cannot determine userId for duid=%s — skipping ChangeReport", duid)
            continue

        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            continue

        # Find changed endpoints by comparing old vs new subDevices
        new_sub = new_img.get("subDevices", {}).get("M", {})
        old_sub = old_img.get("subDevices", {}).get("M", {}) if old_img else {}

        for mac_addr, mac_val in new_sub.items():
            new_eps = mac_val.get("M", {}).get("endpoints", {}).get("M", {})
            old_eps = (old_sub.get(mac_addr, {}).get("M", {})
                               .get("endpoints", {}).get("M", {}))

            for ep_id_str, ep_val in new_eps.items():
                ep_id = int(ep_id_str)
                new_state_raw = ep_val.get("M", {}).get("state", {}).get("M", {})
                old_state_raw = old_eps.get(ep_id_str, {}).get("M", {}).get("state", {}).get("M", {})

                if new_state_raw == old_state_raw:
                    continue  # no change

                state = {k: list(v.values())[0] for k, v in new_state_raw.items()
                         if list(v.keys())[0] in ("N", "S", "BOOL")}
                # Convert DynamoDB N strings to numbers
                for k, v in list(state.items()):
                    try:
                        state[k] = int(v)
                    except (TypeError, ValueError):
                        pass

                ep_type = _get_ep_type_from_s3(user_id, duid, mac_addr, ep_id)
                if ep_type is None:
                    continue  # not in app or excluded type — skip ChangeReport
                properties = _state_to_properties(state, ep_type)
                if not properties:
                    continue

                endpoint_id = _encrypt_endpoint(duid, mac_addr, ep_id, ep_type)
                try:
                    _send_change_report(lwa_token, endpoint_id, properties)
                except Exception:
                    logger.exception("ChangeReport failed duid=%s mac=%s ep=%d", duid, mac_addr, ep_id)

    return {"statusCode": 200}
