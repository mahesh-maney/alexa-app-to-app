"""
Google Home proactive update Lambda for Digilux.

Triggered by DynamoDB stream on user_device_details (same stream as alexa_proactive_discovery).
When devices are added, changed, or removed, calls Google Home Graph API requestSync so the
Google Home app re-fetches the device list from SYNC without the user having to say
"Hey Google, sync my devices."

Also supports reportStateAndNotification when invoked directly (e.g., from the IoT response
handler) to push live device state updates to the Google Home app.

Auth: Google Service Account JSON stored in AWS Secrets Manager. The SA signs a JWT which
is exchanged for a short-lived access token at https://oauth2.googleapis.com/token.
The access token is used for Home Graph API calls (requestSync, reportStateAndNotification).
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Env vars ──────────────────────────────────────────────────────────────────
_DATA_REGION      = os.environ.get("DATA_REGION", "ap-south-1")
_SA_SECRET_ARN    = os.environ.get("GH_SA_SECRET_ARN", "")
_SA_SECRET_REGION = os.environ.get("GH_SA_SECRET_REGION", "eu-west-1")

_USER_DEVICE_TABLE    = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_GH_TOKENS_TABLE      = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_ENDPOINT_KEY_ARN     = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_ENDPOINT_KEY_REGION  = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")
_METADATA_BUCKET      = os.environ.get("METADATA_BUCKET", "digilux-metadata-bucket")

_LIGHT_TYPES = frozenset({2, 4, 13, 14})
_CCT_TYPES   = frozenset({13, 14})
_RGB_TYPES   = frozenset({4, 14})
_FAN_TYPES   = frozenset({3})
_COVER_TYPES = frozenset({5})
_SKIP_TYPES  = frozenset({15, 20, 1000})

_HOMEGRAPH_SYNC_URL  = "https://homegraph.googleapis.com/v1/devices:requestSync"
_HOMEGRAPH_STATE_URL = "https://homegraph.googleapis.com/v1/devices:reportStateAndNotification"
_TOKEN_URL           = "https://oauth2.googleapis.com/token"
_HOMEGRAPH_SCOPE     = "https://www.googleapis.com/auth/homegraph"

# Module-level caches
_sa_creds: dict | None = None
_endpoint_key: bytes | None = None
_dynamodb = None
_s3 = None


# ── AWS clients ───────────────────────────────────────────────────────────────

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


def _get_endpoint_key() -> bytes:
    global _endpoint_key
    if _endpoint_key is None:
        sm = boto3.client("secretsmanager", region_name=_ENDPOINT_KEY_REGION)
        raw = sm.get_secret_value(SecretId=_ENDPOINT_KEY_ARN)["SecretString"]
        _endpoint_key = base64.b64decode(json.loads(raw)["key"])
    return _endpoint_key


def _encrypt_endpoint(duid: str, ieee: str, epid: int, ep_type: int) -> str:
    plaintext = f"{duid}::{ieee}::{epid}::{ep_type}".encode()
    key   = _get_endpoint_key()
    nonce = hashlib.sha256(plaintext).digest()[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + ct + tag).rstrip(b"=").decode()


def _find_user_for_duid(duid: str) -> str | None:
    table = _ddb().Table(_USER_DEVICE_TABLE)
    resp = table.scan(ProjectionExpression="userId, devices")
    for item in resp.get("Items", []):
        for d in item.get("devices", []):
            if d.get("duid") == duid:
                return item["userId"]
    return None


def _get_ep_type_from_s3(user_id: str, duid: str, ieee: str, ep_id: int) -> int | None:
    """Return ep_type from S3 endpoints.json, or None if hidden/excluded/not found."""
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
                    ep_list = json.loads(_s3_client().get_object(
                        Bucket=_METADATA_BUCKET, Key=obj["Key"])["Body"].read())
                    for ep in (ep_list if isinstance(ep_list, list) else []):
                        if (ep.get("subdeviceIeeeAddress") == ieee
                                and int(ep.get("endpointId", -1)) == ep_id):
                            if ep.get("isHidden"):
                                return None
                            ep_type = int(ep.get("endpointType", 1))
                            if ep_type in _SKIP_TYPES:
                                return None
                            return ep_type
    except Exception as exc:
        logger.warning("ep_type lookup failed duid=%s ieee=%s epid=%s: %s", duid, ieee, ep_id, exc)
    return None


def _level_to_fan_speed(level: int) -> str:
    if level <= 1:
        return "1"
    if level == 2:
        return "2"
    if level == 3:
        return "3"
    return "4"


def _status_to_online(status) -> bool:
    """Map DynamoDB endpoint status ('online'/'offline') to GH online bool."""
    if status is None:
        return True  # missing status → treat as online (backward compatible)
    if isinstance(status, bool):
        return status
    return str(status).strip().lower() == "online"


def _state_to_gh(state: dict, ep_type: int, status=None) -> dict:
    """Convert a device state dict (+ endpoint status) to Google Home state."""
    level    = int(state.get("level") or 0)
    on_off_v = state.get("onOff")
    on_off   = bool(on_off_v if on_off_v is not None else (level > 0))

    gh: dict = {"online": _status_to_online(status)}

    if ep_type in _COVER_TYPES:
        # CURTAIN OLD FIX START
        #raw = int(state.get("onOff") or 2)
        #gh["openPercent"] = {2: 100, 4: 0}.get(raw, 50)
        #gh["isRunning"]   = False
        #gh["isPaused"]    = raw in (0, 3)
        #CURTAIN OLD FIX END
        if raw == 2:
            gh["openPercent"] = 100
        elif raw == 4:
            gh["openPercent"] = 0
        elif state.get("openPercent") is not None:
            gh["openPercent"] = max(0, min(100, int(state["openPercent"])))
    else:
        gh["on"] = on_off
        if ep_type in _LIGHT_TYPES:
            gh["brightness"] = level
        if ep_type in _CCT_TYPES and state.get("colorTemp"):
            k = int(1_000_000 / int(state["colorTemp"]))
            gh["color"] = {"temperatureK": max(2000, min(6500, k))}
        if ep_type in _RGB_TYPES and state.get("hue") is not None:
            gh["color"] = {"spectrumHsv": {
                "hue":        float(state.get("hue", 0)),
                "saturation": float(state.get("saturation", 100)) / 100.0,
                "value":      float(level) / 100.0,
            }}
        if ep_type in _FAN_TYPES:
            gh["currentFanSpeedSetting"] = _level_to_fan_speed(level)

    return gh


def _get_sa_creds() -> dict:
    """Load Google Service Account JSON from Secrets Manager (cached per container)."""
    global _sa_creds
    if _sa_creds is None:
        sm = boto3.client("secretsmanager", region_name=_SA_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_SA_SECRET_ARN)["SecretString"]
        _sa_creds = json.loads(raw)
    return _sa_creds


# ── Service Account JWT + token exchange ──────────────────────────────────────

def _build_sa_jwt(sa_creds: dict) -> str:
    """Build a signed JWT for Service Account authentication (RS256)."""
    now = int(time.time())

    header_b64 = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()

    payload_b64 = base64.urlsafe_b64encode(json.dumps({
        "iss":   sa_creds["client_email"],
        "scope": _HOMEGRAPH_SCOPE,
        "aud":   _TOKEN_URL,
        "exp":   now + 3600,
        "iat":   now,
    }).encode()).rstrip(b"=").decode()

    signing_input = f"{header_b64}.{payload_b64}"

    private_key = RSA.import_key(sa_creds["private_key"])
    h           = SHA256.new(signing_input.encode())
    signature   = pkcs1_15.new(private_key).sign(h)
    sig_b64     = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()

    return f"{signing_input}.{sig_b64}"


def _get_homegraph_access_token() -> str:
    """Exchange a Service Account JWT for a Google OAuth2 access token."""
    sa_creds  = _get_sa_creds()
    jwt_token = _build_sa_jwt(sa_creds)

    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":  jwt_token,
    }).encode()

    req = urllib.request.Request(
        _TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError(f"SA token exchange failed {e.code}: {err}") from e


# ── Home Graph API calls ──────────────────────────────────────────────────────

def _post_homegraph(url: str, body: dict, access_token: str) -> None:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp_body = r.read()
            logger.info("Home Graph %s → %s %s", url.split("/")[-1], r.status, resp_body[:200])
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        raise RuntimeError(f"Home Graph {url.split('/')[-1]} failed {e.code}: {err}") from e


def _request_sync(agent_user_id: str, access_token: str) -> None:
    """Tell Google to re-call SYNC for this user (equivalent to Alexa AddOrUpdateReport)."""
    _post_homegraph(_HOMEGRAPH_SYNC_URL, {"agentUserId": agent_user_id}, access_token)
    logger.info("requestSync sent for agentUserId=%s", agent_user_id)


def _report_state(agent_user_id: str, device_states: dict, access_token: str,
                  request_id: str | None = None) -> None:
    """Push live device states to Google (equivalent to Alexa ChangeReport)."""
    body = {
        "requestId": request_id or str(uuid.uuid4()),
        "agentUserId": agent_user_id,
        "payload": {
            "devices": {
                "states": device_states,
            }
        },
    }
    _post_homegraph(_HOMEGRAPH_STATE_URL, body, access_token)
    logger.info("reportStateAndNotification sent for agentUserId=%s devices=%d",
                agent_user_id, len(device_states))


# ── Linked-user helpers ───────────────────────────────────────────────────────

def _is_gh_linked(user_id: str) -> bool:
    """Return True if this user has linked Google Home (has a record in GH_TOKENS_TABLE)."""
    try:
        resp = _ddb().Table(_GH_TOKENS_TABLE).get_item(Key={"userId": user_id})
        return "Item" in resp
    except Exception as exc:
        logger.warning("GH link check failed for userId=%s: %s", user_id, exc)
        return False


def _get_all_linked_users() -> list[str]:
    """Scan GH_TOKENS_TABLE to get all user IDs that have linked Google Home."""
    users: list[str] = []
    try:
        table = _ddb().Table(_GH_TOKENS_TABLE)
        resp  = table.scan(ProjectionExpression="userId")
        for item in resp.get("Items", []):
            if uid := item.get("userId"):
                users.append(uid)
        # Handle pagination
        while "LastEvaluatedKey" in resp:
            resp = table.scan(
                ProjectionExpression="userId",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            for item in resp.get("Items", []):
                if uid := item.get("userId"):
                    users.append(uid)
    except Exception as exc:
        logger.warning("Failed to scan GH tokens table: %s", exc)
    return users


# ── Stream record parsing ─────────────────────────────────────────────────────

def _extract_user_id(record: dict) -> str | None:
    for img_key in ("NewImage", "OldImage"):
        img = record.get("dynamodb", {}).get(img_key, {})
        uid = img.get("userId", {}).get("S")
        if uid:
            return uid
    return None


# ── Device state stream handler ───────────────────────────────────────────────

def _handle_device_state_stream(records: list) -> dict:
    """Handle device_state DynamoDB stream → reportStateAndNotification."""
    for record in records:
        if record.get("eventName") not in ("MODIFY", "INSERT"):
            continue

        new_img = record.get("dynamodb", {}).get("NewImage", {})
        old_img = record.get("dynamodb", {}).get("OldImage", {})

        duid = new_img.get("deviceId", {}).get("S", "")
        if not duid:
            continue

        user_id = new_img.get("userId", {}).get("S") or _find_user_for_duid(duid)
        if not user_id:
            logger.warning("Cannot resolve userId for duid=%s — skipping", duid)
            continue

        if not _is_gh_linked(user_id):
            continue

        new_sub = new_img.get("subDevices", {}).get("M", {})
        old_sub = old_img.get("subDevices", {}).get("M", {}) if old_img else {}

        device_states: dict = {}

        for ieee, mac_val in new_sub.items():
            new_eps = mac_val.get("M", {}).get("endpoints", {}).get("M", {})
            old_eps = old_sub.get(ieee, {}).get("M", {}).get("endpoints", {}).get("M", {})

            for ep_id_str, ep_val in new_eps.items():
                ep_id = int(ep_id_str)
                new_ep = ep_val.get("M", {})
                old_ep = old_eps.get(ep_id_str, {}).get("M", {})

                new_state_raw = new_ep.get("state", {}).get("M", {})
                old_state_raw = old_ep.get("state", {}).get("M", {})

                # status sits beside state on the endpoint: { status, state: {...} }
                new_status = new_ep.get("status", {}).get("S")
                if new_status is None and "BOOL" in new_ep.get("status", {}):
                    new_status = new_ep["status"]["BOOL"]
                old_status = old_ep.get("status", {}).get("S")
                if old_status is None and "BOOL" in old_ep.get("status", {}):
                    old_status = old_ep["status"]["BOOL"]

                if new_state_raw == old_state_raw and new_status == old_status:
                    continue  # nothing changed

                # Deserialise DynamoDB typed values to plain Python
                state: dict = {}
                for k, v in new_state_raw.items():
                    type_key = next(iter(v), None)
                    if type_key == "N":
                        try:
                            state[k] = int(v["N"])
                        except (ValueError, TypeError):
                            pass
                    elif type_key in ("S", "BOOL"):
                        state[k] = list(v.values())[0]

                ep_type = _get_ep_type_from_s3(user_id, duid, ieee, ep_id)
                if ep_type is None:
                    continue

                endpoint_id = _encrypt_endpoint(duid, ieee, ep_id, ep_type)
                device_states[endpoint_id] = _state_to_gh(state, ep_type, status=new_status)

        if not device_states:
            continue

        try:
            token = _get_homegraph_access_token()
            _report_state(user_id, device_states, token)
            logger.info("reportState: userId=%s devices=%d", user_id, len(device_states))
        except Exception as exc:
            logger.error("reportState failed userId=%s: %s", user_id, exc)

    return {"statusCode": 200}


# ── Lambda entry point ────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # Admin: trigger requestSync for all linked users (e.g., after a firmware update)
    if event.get("_admin_sync_all"):
        users = _get_all_linked_users()
        logger.info("Admin sync all: %d linked GH users", len(users))
        try:
            token = _get_homegraph_access_token()
        except Exception as exc:
            logger.error("Failed to get Home Graph token: %s", exc)
            return {"error": str(exc)}
        results = {}
        for uid in users:
            try:
                _request_sync(uid, token)
                results[uid] = "synced"
            except Exception as exc:
                logger.warning("requestSync failed for userId=%s: %s", uid, exc)
                results[uid] = f"error: {exc}"
        return results

    # Direct invocation: push state notification for specific devices
    # Payload: {"_report_state": true, "user_id": "...", "device_states": {"<endpointId>": {...}}}
    if event.get("_report_state"):
        user_id       = event.get("user_id", "")
        device_states = event.get("device_states", {})
        request_id    = event.get("request_id") or str(uuid.uuid4())
        if not user_id or not device_states:
            return {"error": "user_id and device_states required"}
        if not _is_gh_linked(user_id):
            return {"skipped": "user not linked to Google Home"}
        try:
            token = _get_homegraph_access_token()
            _report_state(user_id, device_states, token, request_id=request_id)
            return {"status": "ok", "devices": len(device_states)}
        except Exception as exc:
            logger.error("reportStateAndNotification failed for userId=%s: %s", user_id, exc)
            return {"error": str(exc)}

    # DynamoDB stream records — route by source table
    records = event.get("Records", [])
    if not records:
        return {"statusCode": 200}

    source_arn = records[0].get("eventSourceARN", "")
    if "device_state" in source_arn:
        return _handle_device_state_stream(records)

    # user_device_details stream — requestSync for changed users
    # Collect unique user IDs from stream records
    user_ids_to_sync: set[str] = set()
    for record in records:
        user_id = _extract_user_id(record)
        if user_id:
            user_ids_to_sync.add(user_id)

    if not user_ids_to_sync:
        logger.warning("No userIds found in stream records")
        return {"statusCode": 200}

    # Filter to only linked users before hitting Home Graph API
    linked = [uid for uid in user_ids_to_sync if _is_gh_linked(uid)]
    if not linked:
        logger.info("No GH-linked users in this stream batch — skipping")
        return {"statusCode": 200}

    try:
        token = _get_homegraph_access_token()
    except Exception as exc:
        logger.error("Failed to obtain Home Graph access token: %s", exc)
        return {"statusCode": 500, "error": str(exc)}

    for user_id in linked:
        try:
            _request_sync(user_id, token)
            logger.info("Stream trigger: requestSync sent for userId=%s", user_id)
        except Exception as exc:
            logger.warning("requestSync failed for userId=%s: %s", user_id, exc)

    return {"statusCode": 200, "synced": linked}
