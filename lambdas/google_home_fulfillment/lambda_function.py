"""
google_home_fulfillment — POST /google-home/fulfillment

Google Smart Home fulfillment webhook. Google calls this endpoint whenever a
user issues a voice command ("Hey Google, turn on the lights") or when Google
needs to discover devices (SYNC) or read their state (QUERY).

Authentication: Google sends our access token as "Authorization: Bearer <token>".
We look up the token in DDB (GSI: accessToken-index) to identify the user.

Supported intents:
  action.devices.SYNC      — return user's full device + scene list (S3-backed)
  action.devices.QUERY     — return device states from device_state DynamoDB table
  action.devices.EXECUTE   — publish MQTT commands to iot/device/{duid}/request
  action.devices.DISCONNECT — user unlinked from Google Home (clean up tokens)

Device integration:
  SYNC reads endpoints.json from S3 (digilux-metadata-bucket) per device.
  QUERY reads the device_state DynamoDB table.
  EXECUTE publishes to the IoT MQTT topic (same scheme as Alexa lambda).
  Endpoint IDs use AES-GCM encryption — same IDs shared with Alexa.
"""
from __future__ import annotations

import base64
import colorsys
import hashlib
import json
import logging
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from Crypto.Cipher import AES

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)


# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION", "")
_TOKENS_TABLE              = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_TOKENS_ACCESS_TOKEN_INDEX = os.environ.get("GH_TOKENS_ACCESS_TOKEN_INDEX", "accessToken-index")
_USER_DEVICE_TABLE         = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_SCENE_TABLE               = os.environ.get("SCENE_TABLE", "digilux_scene_data")
_DEVICE_STATE_TABLE        = os.environ.get("DEVICE_STATE_TABLE", "device_state")
_METADATA_BUCKET           = os.environ.get("METADATA_BUCKET", "digilux-metadata-bucket")
_KEY_SECRET_ARN            = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_KEY_SECRET_REGION         = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")
_DEVICE_MANUFACTURER       = os.environ.get("DEVICE_MANUFACTURER", "Digilux")
_DEVICE_MODEL              = os.environ.get("DEVICE_MODEL", "Digilux Smart Device")

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_fulfillment "
        "— all invocations will return HTTP 500 until these are set",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_fulfillment region=%s "
        "tokens_table=%s tokens_index=%s user_device_table=%s "
        "scene_table=%s device_state_table=%s metadata_bucket=%s manufacturer=%s",
        _REGION, _TOKENS_TABLE, _TOKENS_ACCESS_TOKEN_INDEX,
        _USER_DEVICE_TABLE, _SCENE_TABLE, _DEVICE_STATE_TABLE,
        _METADATA_BUCKET, _DEVICE_MANUFACTURER,
    )

# ── Device-type classification ─────────────────────────────────────────────────
_SWITCH_TYPES = frozenset({1})
_LIGHT_TYPES  = frozenset({2, 4, 13, 14})
_CCT_TYPES    = frozenset({13, 14})
_RGB_TYPES    = frozenset({4, 14})
_FAN_TYPES    = frozenset({3})
_COVER_TYPES  = frozenset({5})

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb: object | None = None
_s3: object | None = None
_iot: object | None = None
_endpoint_key: bytes | None = None
_bridge_cache: dict = {}


# ── AWS clients ────────────────────────────────────────────────────────────────

def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_CLIENT_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        logger.info("DDB_CLIENT_READY region=%s", _REGION)
    else:
        logger.debug("DDB_CLIENT_REUSE (warm container)")
    return _dynamodb


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=_REGION)
    return _s3


def _iot_client():
    global _iot
    if _iot is None:
        _iot = boto3.client("iot-data", region_name=_REGION)
    return _iot


def _get_endpoint_key() -> bytes:
    global _endpoint_key
    if _endpoint_key is None:
        sm = boto3.client("secretsmanager", region_name=_KEY_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_KEY_SECRET_ARN)["SecretString"]
        _endpoint_key = base64.b64decode(json.loads(raw)["key"])
    return _endpoint_key


# ── Token → userId resolution ──────────────────────────────────────────────────

def _resolve_user_id(access_token: str) -> str | None:
    """
    Look up userId from our opaque access token using the accessToken-index GSI.
    Returns userId string on success, None if not found or expired.
    """
    t0 = time.time()
    logger.debug(
        "TOKEN_LOOKUP_START token=%s... table=%s index=%s",
        access_token[:8], _TOKENS_TABLE, _TOKENS_ACCESS_TOKEN_INDEX,
    )
    try:
        resp = _ddb().Table(_TOKENS_TABLE).query(
            IndexName=_TOKENS_ACCESS_TOKEN_INDEX,
            KeyConditionExpression="accessToken = :at",
            ExpressionAttributeValues={":at": access_token},
            Limit=1,
        )
        items = resp.get("Items", [])
    except ClientError as exc:
        logger.error("DDB_RESOLVE_TOKEN_ERROR token=%s... error=%s elapsed_ms=%.0f",
                     access_token[:8], exc, (time.time() - t0) * 1000)
        return None

    elapsed_ms = (time.time() - t0) * 1000
    logger.debug("TOKEN_LOOKUP_DONE token=%s... items_found=%d elapsed_ms=%.0f",
                 access_token[:8], len(items), elapsed_ms)

    if not items:
        logger.warning("TOKEN_NOT_FOUND access_token=%s... elapsed_ms=%.0f",
                       access_token[:8], elapsed_ms)
        return None

    record = items[0]
    now = int(time.time())
    expires_at = int(record.get("expiresAt", 0))
    ttl_remaining = expires_at - now if expires_at else -1
    logger.debug(
        "TOKEN_RECORD_FOUND userId=%s expires_at=%d ttl_remaining=%ds token=%s...",
        record.get("userId"), expires_at, ttl_remaining, access_token[:8],
    )
    if expires_at and expires_at < now:
        logger.warning(
            "ACCESS_TOKEN_EXPIRED userId=%s expires_at=%d expired_ago=%ds token=%s...",
            record.get("userId"), expires_at, now - expires_at, access_token[:8],
        )
        return None

    user_id = record.get("userId", "")
    logger.info("TOKEN_RESOLVED userId=%s ttl_remaining=%ds elapsed_ms=%.0f",
                user_id, ttl_remaining, elapsed_ms)
    return user_id or None


# ── Endpoint ID helpers ────────────────────────────────────────────────────────

def _encrypt_endpoint(duid: str, ieee_addr: str, ep_id: int, ep_type: int) -> str:
    plaintext = f"{duid}::{ieee_addr}::{ep_id}::{ep_type}".encode()
    key = _get_endpoint_key()
    nonce = hashlib.sha256(plaintext).digest()[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + ciphertext + tag).rstrip(b"=").decode()


def _decrypt_endpoint(endpoint_id: str) -> tuple[str, str, int, int]:
    """Returns (duid, ieee_addr, ep_id, ep_type)."""
    key = _get_endpoint_key()
    padded = endpoint_id + "=" * (4 - len(endpoint_id) % 4)
    raw = base64.urlsafe_b64decode(padded)
    nonce, ciphertext, tag = raw[:12], raw[12:-16], raw[-16:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag).decode()
    duid, ieee_addr, ep_id, ep_type = plaintext.split("::")
    return duid, ieee_addr, int(ep_id), int(ep_type)


def _encode_scene(scene_id: str) -> str:
    return f"scene::{scene_id}"


def _parse_scene_endpoint_id(endpoint_id: str) -> str | None:
    if endpoint_id.startswith("scene::"):
        return endpoint_id[len("scene::"):]
    return None


# ── Device endpoint builders (Google Home format) ─────────────────────────────

def _load_zone_map(user_id: str, site_id: str) -> dict:
    zone_map: dict = {}
    prefix = f"{user_id}/{site_id}/zones/"
    try:
        paginator = _s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith("/zone_data.json"):
                    continue
                try:
                    resp = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=obj["Key"])
                    data = json.loads(resp["Body"].read())
                    zid = str(data.get("zoneId", ""))
                    zname = data.get("zoneName", "")
                    if zid and zname:
                        zone_map[zid] = zname
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Could not load zones for site %s: %s", site_id, e)
    return zone_map


def _display_name_for_site(name: str, site_name: str, multi_site: bool) -> str:
    if multi_site and site_name:
        return f"{site_name} - {name}"
    return name


def _build_gh_device(ep_name: str, ep_type: int, endpoint_id: str, site_name: str,
                     manufacturer: str, room_hint: str = "", multi_site: bool = False) -> dict:
    """Build a Google Home device object for one endpoint."""
    if ep_type in _FAN_TYPES:
        gh_type = "action.devices.types.FAN"
        traits  = ["action.devices.traits.OnOff", "action.devices.traits.FanSpeed"]
        attrs   = {
            "availableFanSpeeds": {
                "speeds": [
                    {"speed_name": "1",
                     "speed_values": [{"speed_synonym": ["1", "speed 1", "one", "low"], "lang": "en"}]},
                    {"speed_name": "2",
                     "speed_values": [{"speed_synonym": ["2", "speed 2", "two", "medium"], "lang": "en"}]},
                    {"speed_name": "3",
                     "speed_values": [{"speed_synonym": ["3", "speed 3", "three", "high"], "lang": "en"}]},
                    {"speed_name": "4",
                     "speed_values": [{"speed_synonym": ["4", "speed 4", "four", "max", "maximum"], "lang": "en"}]},
                ],
                "ordered": True,
            },
            "supportsFanSpeedPercent": False,
            "reversible": False,
        }
    elif ep_type in _COVER_TYPES:
        gh_type = "action.devices.types.BLINDS"
        traits  = ["action.devices.traits.OpenClose", "action.devices.traits.StartStop"]
        attrs   = {"discreteOnlyOpenClose": True, "pausable": True}
    elif ep_type in _SWITCH_TYPES:
        gh_type = "action.devices.types.LIGHT"
        traits  = ["action.devices.traits.OnOff"]
        attrs   = {}
    elif ep_type in _LIGHT_TYPES:
        gh_type = "action.devices.types.LIGHT"
        traits  = ["action.devices.traits.OnOff", "action.devices.traits.Brightness"]
        attrs   = {}
        if ep_type in _CCT_TYPES and ep_type not in _RGB_TYPES:
            traits.append("action.devices.traits.ColorSetting")
            attrs["colorTemperatureRange"] = {"temperatureMinK": 2000, "temperatureMaxK": 6500}
        elif ep_type in _RGB_TYPES and ep_type not in _CCT_TYPES:
            traits.append("action.devices.traits.ColorSetting")
            attrs["colorModel"] = "hsv"
        elif ep_type in _CCT_TYPES and ep_type in _RGB_TYPES:
            traits.append("action.devices.traits.ColorSetting")
            attrs["colorModel"] = "hsv"
            attrs["colorTemperatureRange"] = {"temperatureMinK": 2000, "temperatureMaxK": 6500}
    else:
        gh_type = "action.devices.types.SWITCH"
        traits  = ["action.devices.traits.OnOff"]
        attrs   = {}

    display_name = _display_name_for_site(ep_name, site_name, multi_site)
    device: dict = {
        "id":   endpoint_id,
        "type": gh_type,
        "traits": traits,
        "name": {
            "defaultNames": [display_name],
            "name":         display_name,
            "nicknames":    [display_name],
        },
        "willReportState": True,
        "attributes": attrs,
        "deviceInfo": {
            "manufacturer": _DEVICE_MANUFACTURER,
            "model":        _DEVICE_MODEL,
        },
        "customData": {
            "site": site_name or "",
        },
    }
    if room_hint:
        device["roomHint"] = room_hint
    return device


def _build_device_endpoints_gh(user_id: str, site_id: str, site_name: str,
                                duid: str, zone_map: dict | None = None,
                                multi_site: bool = False) -> list[dict]:
    """Read endpoints.json from S3 and return Google Home device objects."""
    prefix = f"{user_id}/{site_id}/devices/{duid}/"
    devices = []
    try:
        paginator = _s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith("/endpoints.json"):
                    continue
                resp = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=obj["Key"])
                ep_list = json.loads(resp["Body"].read())
                for ep in (ep_list if isinstance(ep_list, list) else []):
                    if ep.get("isHidden"):
                        continue
                    ieee_addr = ep.get("subdeviceIeeeAddress", "")
                    ep_id_raw = ep.get("endpointId")
                    ep_type   = int(ep.get("endpointType", 1))
                    if ep_type in {15, 20, 1000}:
                        continue
                    raw_name  = ep.get("endpointName") or "Unknown Device"
                    zone_id   = str(ep.get("zoneId") or "")
                    zone_name = (zone_map or {}).get(zone_id, "") if zone_id and zone_id != "0" else ""
                    if not ieee_addr or ep_id_raw is None:
                        continue
                    endpoint_id = _encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type)
                    devices.append(_build_gh_device(raw_name, ep_type, endpoint_id, site_name,
                                                    _DEVICE_MANUFACTURER, room_hint=zone_name,
                                                    multi_site=multi_site))
    except Exception:
        logger.exception("Failed building GH device list for duid=%s", duid)
    return devices


def _get_all_gh_devices(user_id: str) -> list[dict]:
    """Return all device + scene objects for a user in Google Home format."""
    t0 = time.time()
    devices: list[dict] = []

    udd_table = _ddb().Table(_USER_DEVICE_TABLE)
    sites = udd_table.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
    multi_site = len(sites) > 1
    logger.debug("SYNC_SITES_LOADED userId=%s site_count=%d multi_site=%s",
                 user_id, len(sites), multi_site)

    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", "")
        site_duids = [d.get("duid", "") for d in site.get("devices", []) if d.get("duid")]
        logger.debug("SYNC_SITE_START userId=%s siteId=%s siteName=%s duid_count=%d duids=%s",
                     user_id, site_id, site_name, len(site_duids), site_duids)
        zone_map  = _load_zone_map(user_id, site_id)
        logger.debug("SYNC_ZONE_MAP_LOADED userId=%s siteId=%s zone_count=%d zones=%s",
                     user_id, site_id, len(zone_map), list(zone_map.values()))
        site_ep_count_before = len(devices)
        for device in site.get("devices", []):
            duid = device.get("duid", "")
            if duid:
                eps = _build_device_endpoints_gh(
                    user_id, site_id, site_name, duid, zone_map, multi_site)
                logger.debug("SYNC_DUID_ENDPOINTS duid=%s siteId=%s endpoint_count=%d",
                             duid, site_id, len(eps))
                devices.extend(eps)
        logger.info("SYNC_SITE_DONE userId=%s siteId=%s siteName=%s endpoints_added=%d",
                    user_id, site_id, site_name, len(devices) - site_ep_count_before)

    # Scenes — read from S3 per site
    seen_scene_ids: set = set()
    scene_count = 0
    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", "")
        if not site_id:
            continue
        s3_key = f"{user_id}/{site_id}/scenes/scene_data.json"
        logger.debug("SYNC_SCENES_FETCH userId=%s siteId=%s s3_key=%s", user_id, site_id, s3_key)
        try:
            obj  = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
            data = json.loads(obj["Body"].read())
            site_scenes_added = 0
            if isinstance(data, list):
                for scene in data:
                    scene_id = scene.get("sceneId") or scene.get("id", "")
                    if not scene_id or scene_id in seen_scene_ids:
                        continue
                    seen_scene_ids.add(scene_id)
                    scene_name = _display_name_for_site(
                        scene.get("sceneName") or "Scene", site_name, multi_site)
                    entry: dict = {
                        "id":    _encode_scene(str(scene_id)),
                        "type":  "action.devices.types.SWITCH",
                        "traits": ["action.devices.traits.OnOff"],
                        "name": {
                            "name":      scene_name,
                            "nicknames": [scene_name],
                        },
                        "willReportState": False,
                        "attributes": {},
                        "deviceInfo": {
                            "manufacturer": _DEVICE_MANUFACTURER,
                            "model":        "scene",
                        },
                    }
                    devices.append(entry)
                    scene_count += 1
                    site_scenes_added += 1
            logger.debug("SYNC_SCENES_DONE userId=%s siteId=%s scenes_added=%d",
                         user_id, site_id, site_scenes_added)
        except Exception as exc:
            code = getattr(getattr(exc, "response", {}), "get", lambda *a: "")(
                "Error", {}).get("Code", "")
            if code != "NoSuchKey":
                logger.warning("Scene load failed userId=%s siteId=%s: %s", user_id, site_id, exc)
            else:
                logger.debug("SYNC_NO_SCENES userId=%s siteId=%s (no scene_data.json)",
                             user_id, site_id)

    elapsed_ms = (time.time() - t0) * 1000
    logger.info("SYNC_COMPLETE scenes=%d total_devices=%d userId=%s elapsed_ms=%.0f",
                scene_count, len(devices), user_id, elapsed_ms)
    return devices


# ── Device state helpers ───────────────────────────────────────────────────────

def _get_current_state(duid: str, mac: str, epid: int) -> dict:
    table = _ddb().Table(_DEVICE_STATE_TABLE)
    resp = table.get_item(Key={"deviceId": duid})
    item = resp.get("Item", {})
    try:
        return item["subDevices"][mac]["endpoints"][str(epid)]["state"] or {}
    except (KeyError, TypeError):
        return {}


def _get_bridge_address(user_id: str, duid: str) -> str:
    if duid in _bridge_cache:
        return _bridge_cache[duid]
    try:
        udd = _ddb().Table(_USER_DEVICE_TABLE)
        sites = udd.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
        for site in sites:
            site_id = site.get("siteId", "")
            if not any(d.get("duid") == duid for d in site.get("devices", [])):
                continue
            prefix = f"{user_id}/{site_id}/devices/{duid}/"
            resp = _s3_client().list_objects_v2(Bucket=_METADATA_BUCKET, Prefix=prefix, MaxKeys=5)
            for obj in resp.get("Contents", []):
                parts = obj["Key"].split("/")
                if len(parts) >= 7:
                    _bridge_cache[duid] = parts[4]
                    return parts[4]
    except Exception as exc:
        logger.warning("Could not resolve bridge address for duid=%s: %s", duid, exc)
    return ""


def _update_device_state_db(duid: str, mac: str, epid: int, state: dict) -> None:
    table = _ddb().Table(_DEVICE_STATE_TABLE)
    attr_names = {
        "#sd": "subDevices", "#mac": mac, "#ep": "endpoints",
        "#eid": str(epid), "#st": "state", "#ts": "lastUpdatedTime",
    }
    attr_values: dict = {":ts": str(int(time.time() * 1000))}
    set_parts = ["#ts = :ts"]
    for i, (k, v) in enumerate(state.items()):
        attr_names[f"#k{i}"] = k
        attr_values[f":v{i}"] = v
        set_parts.append(f"#sd.#mac.#ep.#eid.#st.#k{i} = :v{i}")
    try:
        table.update_item(
            Key={"deviceId": duid},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )
    except Exception as exc:
        logger.warning("DynamoDB optimistic update failed: %s", exc)


def _hsv_to_hex(hue_deg, saturation_pct, value_pct) -> str:
    h = (float(hue_deg) % 360) / 360.0
    s = max(0.0, min(100.0, float(saturation_pct))) / 100.0
    v = max(0.0, min(100.0, float(value_pct))) / 100.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def _publish_device_command(duid: str, mac: str, epid: int, eptype: int,
                            state: dict, user_id: str = "") -> None:
    """Publish control command to iot/device/{duid}/request — identical to Alexa lambda."""
    if "onOff" in state and eptype not in _COVER_TYPES:
        state = {**state, "onOff": 1 if state["onOff"] else 0}

    ts = int(time.time() * 1000)
    bridge_addr = _get_bridge_address(user_id, duid) if user_id else ""

    wire_state = state
    if eptype in _RGB_TYPES and "hue" in state and "saturation" in state:
        cmd_type = 139
        wire_state = {"color": _hsv_to_hex(state["hue"], state["saturation"], state.get("level", 100))}
    elif "colorTemp" in state:
        cmd_type = 109
    elif "level" in state:
        cmd_type = 108
    elif eptype in _COVER_TYPES:
        cmd_type = 107
    elif "onOff" in state:
        cmd_type = 131 if state["onOff"] else 132
    else:
        cmd_type = 107

    msg_payload: dict = {"mac": mac, "epid": epid, "epType": eptype, "state": wire_state}
    if bridge_addr:
        msg_payload["zigbeeBridgeAddress"] = bridge_addr

    payload = {
        "cmdType":    cmd_type,
        "sessionId":  str(uuid.uuid4()),
        "msgPayload": msg_payload,
        "requestId":  f"REQ_{ts}_{epid}",
        "timestamp":  ts,
    }
    logger.info("IoT publish topic=iot/device/%s/request payload=%s", duid, json.dumps(payload))
    _iot_client().publish(
        topic=f"iot/device/{duid}/request",
        qos=1,
        payload=json.dumps(payload),
    )


# ── Fan speed helpers ──────────────────────────────────────────────────────────

_FAN_SPEED_TO_LEVEL = {"1": 1, "2": 2, "3": 3, "4": 4}


def _fan_speed_to_level(speed: str) -> int:
    return _FAN_SPEED_TO_LEVEL.get(speed, 2)


def _level_to_fan_speed(level: int) -> str:
    if level <= 1:
        return "1"
    if level == 2:
        return "2"
    if level == 3:
        return "3"
    return "4"


# ── Scene activation ───────────────────────────────────────────────────────────

def _activate_scene(user_id: str, scene_id: str) -> None:
    """Publish scene activation to IoT — same topic/payload as Alexa SceneController."""
    scene_table = _ddb().Table(_SCENE_TABLE)
    resp  = scene_table.query(
        IndexName="GSI_scene",
        KeyConditionExpression=Key("sceneId").eq(str(scene_id)) & Key("userId").eq(user_id),
    )
    items = resp.get("Items", [])
    if not items:
        logger.warning("Scene not found: sceneId=%s userId=%s", scene_id, user_id)
        return

    scene   = items[0]
    site_id = scene.get("siteId", "")
    duid    = scene.get("duid")

    if not duid:
        udd_table = _ddb().Table(_USER_DEVICE_TABLE)
        site_item = udd_table.get_item(Key={"userId": user_id, "siteId": site_id}).get("Item", {})
        duid = next((d.get("duid") for d in site_item.get("devices", []) if d.get("duid")), None)
    if not duid:
        logger.warning("No device found for scene %s", scene_id)
        return

    group_id    = None
    bridge_addr = None
    try:
        s3_key = f"{user_id}/{site_id}/scenes/scene_data.json"
        obj    = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
        scenes = json.loads(obj["Body"].read())
        for s in scenes:
            if int(s.get("sceneId", -1)) == int(scene_id):
                group_id     = s.get("groupId")
                devices_list = s.get("subdevicesMappedList", [])
                if devices_list:
                    bridge_addr = devices_list[0].get("deviceBridgeAddress")
                break
    except Exception as exc:
        logger.warning("S3 scene lookup failed: %s", exc)

    msg_payload: dict = {"sceneId": int(scene_id)}
    if group_id is not None:
        msg_payload["groupId"] = int(group_id)
    if bridge_addr:
        msg_payload["zigbeeBridgeAddress"] = bridge_addr

    _iot_client().publish(
        topic=f"iot/device/{duid}/scene",
        qos=1,
        payload=json.dumps({
            "cmdType":     130,
            "sessionId":   user_id,
            "isProcessed": True,
            "msgPayload":  msg_payload,
        }),
    )
    logger.info("Scene activate published: sceneId=%s duid=%s", scene_id, duid)


# ── EXECUTE helpers ────────────────────────────────────────────────────────────

def _build_iot_state(cmd: str, params: dict, eptype: int,
                     duid: str, mac: str, epid: int) -> dict:
    """Convert a Google Home command + params to the IoT state dict."""
    if cmd == "action.devices.commands.OnOff":
        on = params.get("on", False)
        if eptype in _COVER_TYPES:
            return {"onOff": 2 if on else 4}
        return {"onOff": on}

    elif cmd == "action.devices.commands.BrightnessAbsolute":
        brightness = max(0, min(100, int(params.get("brightness", 100))))
        return {"onOff": brightness > 0, "level": brightness}

    elif cmd == "action.devices.commands.ColorAbsolute":
        color = params.get("color", {})
        kelvin_raw = color.get("temperatureK") or color.get("temperature")
        if kelvin_raw is not None:
            kelvin = max(1000, min(10000, int(kelvin_raw)))
            mireds = max(154, min(500, int(1_000_000 / kelvin)))
            return {"colorTemp": mireds, "onOff": True}
        if "spectrumHsv" in color:
            hsv = color["spectrumHsv"]
            iot_hue   = max(0, min(360, int(round(float(hsv.get("hue", 0))))))
            iot_sat   = max(0, min(100, int(round(float(hsv.get("saturation", 1.0)) * 100))))
            iot_level = max(0, min(100, int(round(float(hsv.get("value", 1.0)) * 100))))
            return {"hue": iot_hue, "saturation": iot_sat, "level": iot_level, "onOff": True}
        if "spectrumRgb" in color:
            rgb = int(color["spectrumRgb"])
            r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            mx = max(r, g, b) / 255.0
            if mx == 0:
                return {"hue": 0, "saturation": 0, "level": 0, "onOff": False}
            mn = min(r, g, b) / 255.0
            sat = (mx - mn) / mx
            r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
            if mx == r_:   hue = 60 * ((g_ - b_) / (mx - mn) % 6)
            elif mx == g_: hue = 60 * ((b_ - r_) / (mx - mn) + 2)
            else:          hue = 60 * ((r_ - g_) / (mx - mn) + 4)
            return {"hue": int(round(hue)) % 360, "saturation": int(round(sat * 100)),
                    "level": int(round(mx * 100)), "onOff": True}
        raise ValueError(f"Unsupported color format: {color}")

    elif cmd == "action.devices.commands.SetFanSpeed":
        fan_speed = params.get("fanSpeed", "2")
        level = _fan_speed_to_level(fan_speed)
        return {"onOff": level > 0, "level": level}

    elif cmd == "action.devices.commands.OpenClose":
        open_pct = int(params.get("openPercent", 0))
        return {"onOff": 2} if open_pct >= 50 else {"onOff": 4}

    elif cmd == "action.devices.commands.StartStop":
        start = params.get("start", True)
        return {"onOff": 2} if start else {"onOff": 0}

    elif cmd == "action.devices.commands.PauseUnpause":
        return {"onOff": 0}

    elif cmd == "action.devices.commands.ActivateScene":
        raise ValueError("Use _activate_scene for scene commands")

    raise ValueError(f"Unknown command: {cmd}")


def _build_gh_return_state(cmd: str, params: dict) -> dict:
    """Build the states dict to return in the EXECUTE response (optimistic)."""
    if cmd == "action.devices.commands.OnOff":
        return {"on": params.get("on", False)}
    if cmd == "action.devices.commands.BrightnessAbsolute":
        brightness = params.get("brightness", 100)
        return {"brightness": brightness, "on": brightness > 0}
    if cmd == "action.devices.commands.ColorAbsolute":
        color = params.get("color", {})
        kelvin_raw = color.get("temperatureK") or color.get("temperature")
        if kelvin_raw is not None:
            return {"color": {"temperatureK": int(kelvin_raw)}, "on": True}
        if "spectrumHsv" in color:
            return {"color": {"spectrumHsv": color["spectrumHsv"]}, "on": True}
        if "spectrumRgb" in color:
            return {"color": {"spectrumRgb": color["spectrumRgb"]}, "on": True}
    if cmd == "action.devices.commands.SetFanSpeed":
        return {"currentFanSpeedSetting": params.get("fanSpeed", "2"), "on": True}
    if cmd == "action.devices.commands.OpenClose":
        open_pct = params.get("openPercent", 0)
        return {"openPercent": open_pct, "isRunning": False, "isPaused": False}
    if cmd in ("action.devices.commands.StartStop", "action.devices.commands.PauseUnpause"):
        return {"isRunning": False, "isPaused": True, "openPercent": 50}
    return {}


def _execute_command_group(user_id: str, device_ids: list[str], cmd: str,
                           params: dict, results: list[dict]) -> None:
    success_ids: list[str] = []
    error_ids:   list[str] = []

    for device_id in device_ids:
        if scene_id := _parse_scene_endpoint_id(device_id):
            if cmd == "action.devices.commands.OnOff" and params.get("on", False):
                logger.info("EXECUTE_SCENE_ACTIVATE sceneId=%s userId=%s cmd=%s",
                            scene_id, user_id, cmd)
                _activate_scene(user_id, scene_id)
                logger.info("EXECUTE_SCENE_OK sceneId=%s userId=%s", scene_id, user_id)
                _audit("GH_SCENE_ACTIVATE", userId=user_id, sceneId=scene_id)
            else:
                logger.debug("EXECUTE_SCENE_SKIP sceneId=%s cmd=%s on=%s (only OnOff+on activates)",
                             scene_id, cmd, params.get("on"))
            success_ids.append(device_id)
            continue

        try:
            duid, mac, epid, eptype = _decrypt_endpoint(device_id)
            logger.debug("EXECUTE_DECRYPT_OK duid=%s mac=%s epid=%d eptype=%d", duid, mac, epid, eptype)
        except Exception as exc:
            logger.warning("EXECUTE_DECRYPT_FAILED device_id=%s... error=%s", device_id[:16], exc)
            error_ids.append(device_id)
            continue

        try:
            state = _build_iot_state(cmd, params, eptype, duid, mac, epid)
            logger.debug("EXECUTE_IOT_STATE duid=%s epid=%d cmd=%s iot_state=%s",
                         duid, epid, cmd, state)
        except ValueError as exc:
            logger.warning("EXECUTE_UNSUPPORTED_CMD cmd=%s duid=%s device_id=%s... error=%s",
                           cmd, duid, device_id[:16], exc)
            error_ids.append(device_id)
            continue

        _publish_device_command(duid, mac, epid, eptype, state, user_id)
        _update_device_state_db(duid, mac, epid, state)
        success_ids.append(device_id)
        logger.info("EXECUTE_DEVICE_OK cmd=%s duid=%s mac=%s epid=%d state=%s",
                    cmd, duid, mac, epid, state)
        _audit("GH_DEVICE_CMD", userId=user_id, cmd=cmd, duid=duid, epid=epid)

    final_state = _build_gh_return_state(cmd, params)

    if success_ids:
        results.append({
            "ids":    success_ids,
            "status": "SUCCESS",
            "states": {**final_state, "online": True},
        })
    if error_ids:
        results.append({
            "ids":       error_ids,
            "status":    "ERROR",
            "errorCode": "commandInsertFailed",
        })


# ── Intent handlers ────────────────────────────────────────────────────────────

def _handle_sync(payload: dict, user_id: str, request_id: str) -> dict:
    t0 = time.time()
    logger.info("INTENT_SYNC_START userId=%s request_id=%s", user_id, request_id)
    devices = _get_all_gh_devices(user_id)
    elapsed_ms = (time.time() - t0) * 1000
    logger.info("INTENT_SYNC_OK userId=%s device_count=%d elapsed_ms=%.0f request_id=%s",
                user_id, len(devices), elapsed_ms, request_id)
    _audit("GH_SYNC", userId=user_id, device_count=len(devices),
           elapsed_ms=int(elapsed_ms), request_id=request_id)
    return {
        "requestId": request_id,
        "payload": {
            "agentUserId": user_id,
            "devices":     devices,
        },
    }


def _handle_query(payload: dict, user_id: str, request_id: str) -> dict:
    t0 = time.time()
    queried_devices = payload.get("devices", [])
    logger.info("INTENT_QUERY_START userId=%s device_count=%d request_id=%s",
                user_id, len(queried_devices), request_id)
    logger.debug("QUERY_DEVICE_IDS userId=%s ids=%s request_id=%s",
                 user_id, [d.get("id", "")[:16] for d in queried_devices], request_id)
    device_states: dict = {}

    for device in queried_devices:
        device_id = device.get("id", "")

        if _parse_scene_endpoint_id(device_id):
            device_states[device_id] = {"online": True, "status": "SUCCESS", "on": False}
            logger.debug("QUERY_SCENE device_id=%s... online=True on=False", device_id[:16])
            continue

        try:
            duid, mac, epid, ep_type = _decrypt_endpoint(device_id)
        except Exception as exc:
            logger.warning("QUERY_DECRYPT_FAILED device_id=%s... error=%s", device_id[:16], exc)
            device_states[device_id] = {"online": False, "status": "ERROR", "errorCode": "deviceNotFound"}
            continue

        logger.debug("QUERY_DEVICE duid=%s mac=%s epid=%d ep_type=%d", duid, mac, epid, ep_type)
        state = _get_current_state(duid, mac, epid)
        logger.debug("QUERY_RAW_STATE duid=%s epid=%d state=%s", duid, epid, state)
        level = int(state.get("level") or 0)
        on_off_raw = state.get("onOff")
        on_off = bool(on_off_raw if on_off_raw is not None else (level > 0))

        gh_state: dict = {"online": True, "status": "SUCCESS"}

        if ep_type in _COVER_TYPES:
            raw_onoff = int(state.get("onOff") or 2)
            open_pct = {2: 100, 4: 0}.get(raw_onoff, 50)
            is_stopped = raw_onoff in (0, 3)
            gh_state["openPercent"] = open_pct
            gh_state["isRunning"]   = False
            gh_state["isPaused"]    = is_stopped
        else:
            gh_state["on"] = on_off
            if ep_type in _LIGHT_TYPES:
                gh_state["brightness"] = level
            if ep_type in _CCT_TYPES and state.get("colorTemp"):
                kelvin = int(1_000_000 / int(state["colorTemp"]))
                kelvin = max(2000, min(6500, kelvin))
                gh_state["color"] = {"temperatureK": kelvin}
            if ep_type in _RGB_TYPES and state.get("hue") is not None:
                gh_state["color"] = {
                    "spectrumHsv": {
                        "hue":        float(state.get("hue", 0)),
                        "saturation": float(state.get("saturation", 100)) / 100.0,
                        "value":      float(level) / 100.0,
                    }
                }
            if ep_type in _FAN_TYPES:
                gh_state["currentFanSpeedSetting"] = _level_to_fan_speed(level)

        device_states[device_id] = gh_state
        logger.debug("QUERY_GH_STATE duid=%s epid=%d gh_state=%s", duid, epid, gh_state)

    elapsed_ms = (time.time() - t0) * 1000
    logger.info("INTENT_QUERY_OK userId=%s queried=%d responded=%d elapsed_ms=%.0f request_id=%s",
                user_id, len(queried_devices), len(device_states), elapsed_ms, request_id)
    _audit("GH_QUERY", userId=user_id, device_count=len(device_states),
           elapsed_ms=int(elapsed_ms), request_id=request_id)
    return {
        "requestId": request_id,
        "payload": {"devices": device_states},
    }


def _handle_execute(payload: dict, user_id: str, request_id: str) -> dict:
    t0 = time.time()
    command_groups = payload.get("commands", [])
    total_targets = sum(len(cg.get("devices", [])) for cg in command_groups)
    logger.info("INTENT_EXECUTE_START userId=%s command_groups=%d total_targets=%d request_id=%s",
                user_id, len(command_groups), total_targets, request_id)
    results: list[dict] = []

    for gi, command_group in enumerate(command_groups):
        target_ids = [d["id"] for d in command_group.get("devices", [])]
        executions = command_group.get("execution", [])
        logger.debug("EXECUTE_GROUP group=%d target_count=%d execution_count=%d targets=%s",
                     gi, len(target_ids), len(executions),
                     [tid[:16] for tid in target_ids])
        for ei, execution in enumerate(executions):
            cmd    = execution.get("command", "")
            params = execution.get("params", {})
            logger.info("EXECUTE_CMD group=%d exec=%d command=%s params=%s target_count=%d userId=%s",
                        gi, ei, cmd, params, len(target_ids), user_id)
            _audit("GH_EXECUTE_CMD", userId=user_id, command=cmd, target_count=len(target_ids),
                   request_id=request_id)
            _execute_command_group(user_id, target_ids, cmd, params, results)

    elapsed_ms = (time.time() - t0) * 1000
    success_count = sum(1 for r in results if r.get("status") == "SUCCESS")
    error_count   = sum(1 for r in results if r.get("status") == "ERROR")
    logger.info(
        "INTENT_EXECUTE_OK userId=%s result_groups=%d success=%d error=%d elapsed_ms=%.0f request_id=%s",
        user_id, len(results), success_count, error_count, elapsed_ms, request_id,
    )
    _audit("GH_EXECUTE", userId=user_id, result_count=len(results),
           success=success_count, errors=error_count,
           elapsed_ms=int(elapsed_ms), request_id=request_id)
    return {
        "requestId": request_id,
        "payload": {"commands": results},
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
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")
    logger.info(
        "REQUEST_START function=google_home_fulfillment source_ip=%s request_id=%s",
        source_ip, request_id,
    )

    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        _audit("GH_FULFILLMENT_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate via Bearer token ──────────────────────────────────────
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        logger.warning("MISSING_BEARER_TOKEN source_ip=%s request_id=%s", source_ip, request_id)
        _audit("GH_FULFILLMENT_AUTH_MISSING", source_ip=source_ip, request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    access_token = auth.split(" ", 1)[1].strip()
    logger.debug("BEARER_TOKEN_RECEIVED token=%s... source_ip=%s request_id=%s",
                 access_token[:8], source_ip, request_id)
    user_id = _resolve_user_id(access_token)
    if not user_id:
        logger.warning(
            "INVALID_OR_EXPIRED_TOKEN token=%s... source_ip=%s request_id=%s",
            access_token[:8], source_ip, request_id,
        )
        _audit("GH_FULFILLMENT_AUTH_FAILED", token_prefix=access_token[:8],
               source_ip=source_ip, request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)
    _audit("GH_FULFILLMENT_AUTH_OK", userId=user_id, request_id=request_id)

    # ── 2. Parse request body ──────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception as exc:
        logger.warning("BODY_PARSE_ERROR reason=%s request_id=%s", exc, request_id)
        return _resp(400, {"error": "Invalid JSON body"})

    google_request_id = body.get("requestId", request_id)
    inputs = body.get("inputs", [])
    logger.debug(
        "REQUEST_BODY_PARSED google_request_id=%s input_count=%d userId=%s request_id=%s",
        google_request_id, len(inputs), user_id, request_id,
    )
    if not inputs:
        logger.warning("NO_INPUTS_IN_REQUEST userId=%s request_id=%s", user_id, request_id)
        _audit("GH_FULFILLMENT_NO_INPUTS", userId=user_id, request_id=request_id)
        return _resp(400, {"error": "No inputs in request"})

    # ── 3. Dispatch intent ─────────────────────────────────────────────────────
    first_input = inputs[0]
    intent      = first_input.get("intent", "")
    payload_in  = first_input.get("payload", {})

    logger.info("INTENT_DISPATCH intent=%s userId=%s google_request_id=%s request_id=%s",
                intent, user_id, google_request_id, request_id)
    _audit("GH_FULFILLMENT_INTENT", intent=intent, userId=user_id,
           google_request_id=google_request_id, request_id=request_id)

    handler = _INTENT_HANDLERS.get(intent)
    if not handler:
        logger.warning("UNKNOWN_INTENT intent=%s userId=%s known_intents=%s request_id=%s",
                       intent, user_id, list(_INTENT_HANDLERS.keys()), request_id)
        _audit("GH_FULFILLMENT_UNKNOWN_INTENT", intent=intent, userId=user_id,
               request_id=request_id)
        return _resp(400, {"error": f"Unknown intent: {intent}"})

    logger.debug("INTENT_HANDLER_FOUND intent=%s handler=%s", intent, handler.__name__)
    t_handler = time.time()
    try:
        result = handler(payload_in, user_id, google_request_id)
    except Exception as exc:
        logger.error("INTENT_HANDLER_ERROR intent=%s error=%s userId=%s request_id=%s",
                     intent, exc, user_id, google_request_id)
        _audit("GH_FULFILLMENT_HANDLER_ERROR", intent=intent, userId=user_id,
               error=str(exc), request_id=google_request_id)
        return _resp(500, {"error": "Internal server error"})

    handler_ms = (time.time() - t_handler) * 1000
    logger.debug("INTENT_HANDLER_COMPLETE intent=%s elapsed_ms=%.0f request_id=%s",
                 intent, handler_ms, google_request_id)
    logger.info("REQUEST_OK function=google_home_fulfillment intent=%s userId=%s elapsed_ms=%.0f",
                intent, user_id, handler_ms)
    _audit("GH_FULFILLMENT_OK", intent=intent, userId=user_id,
           elapsed_ms=int(handler_ms), request_id=google_request_id)
    return _resp(200, result)


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
