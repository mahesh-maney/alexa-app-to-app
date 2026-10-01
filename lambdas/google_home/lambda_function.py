"""
Google Home Smart Home Action fulfillment Lambda for Digilux.

Handles:
  action.devices.SYNC    — return all devices + scenes in Google Home format
  action.devices.QUERY   — return current device state for requested endpoint IDs
  action.devices.EXECUTE — execute a command (OnOff, Brightness, ColorTemp, FanSpeed, OpenClose, Scene)

Auth: Bearer token in Authorization header is a Cognito JWT; _decode_jwt_sub extracts userId.
IoT: Same MQTT topic/payload as Alexa — _publish_device_command is identical.
Endpoint IDs: Same AES-GCM encryption scheme as Alexa — IDs are shared across both skills.
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
from Crypto.Cipher import AES

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Env vars ──────────────────────────────────────────────────────────────────
_DATA_REGION       = os.environ.get("DATA_REGION", "ap-south-1")
_KEY_SECRET_ARN    = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_KEY_SECRET_REGION = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")

_USER_DEVICE_TABLE  = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_SCENE_TABLE        = os.environ.get("SCENE_TABLE", "digilux_scene_data")
_DEVICE_STATE_TABLE = os.environ.get("DEVICE_STATE_TABLE", "device_state")
_METADATA_BUCKET    = os.environ.get("METADATA_BUCKET", "digilux-metadata-bucket")
_GH_TOKENS_TABLE    = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_GH_PROACTIVE_FN    = os.environ.get("GH_PROACTIVE_FUNCTION", "")
_DEVICE_MANUFACTURER = os.environ.get("DEVICE_MANUFACTURER", "Digilux")
_DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "Digilux Smart Device")

# ── Device-type classification (same frozensets as Alexa lambda) ──────────────
_SWITCH_TYPES = frozenset({1})
_LIGHT_TYPES = frozenset({2, 4, 13, 14})
_CCT_TYPES   = frozenset({13, 14})
_RGB_TYPES   = frozenset({4, 14})
_FAN_TYPES   = frozenset({3})
_COVER_TYPES = frozenset({5})

# Module-level caches
_endpoint_key: bytes | None = None
_dynamodb = None
_s3 = None
_iot = None
_bridge_cache: dict = {}


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


def _iot_client():
    global _iot
    if _iot is None:
        _iot = boto3.client("iot-data", region_name=_DATA_REGION)
    return _iot


def _get_endpoint_key() -> bytes:
    global _endpoint_key
    if _endpoint_key is None:
        sm = boto3.client("secretsmanager", region_name=_KEY_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_KEY_SECRET_ARN)["SecretString"]
        _endpoint_key = base64.b64decode(json.loads(raw)["key"])
    return _endpoint_key


# ── JWT / auth helpers ────────────────────────────────────────────────────────

def _decode_jwt_sub(token: str) -> str:
    """Extract sub (Cognito userId) from a JWT without verifying signature."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWT format")
    padding = "=" * (4 - len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        raise ValueError("Missing sub in JWT claims")
    return sub


# ── Endpoint ID helpers ───────────────────────────────────────────────────────

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
    """Google's Smart Home SYNC schema has no structure/hub-hint field the
    way Alexa's endpoint graph has displayCategories=HUB + isChildOf — Google
    Home's own multi-home concept ("Structures") lives entirely in the user's
    app and isn't assignable via this API. So for multi-site users, the only
    available disambiguation is the visible name itself; single-site users
    are unaffected (name unchanged, same as today)."""
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
            # CCT only (type 13): omit colorModel — temperature slider only
            traits.append("action.devices.traits.ColorSetting")
            attrs["colorTemperatureRange"] = {"temperatureMinK": 2000, "temperatureMaxK": 6500}
        elif ep_type in _RGB_TYPES and ep_type not in _CCT_TYPES:
            # RGB only (type 4): color picker only
            traits.append("action.devices.traits.ColorSetting")
            attrs["colorModel"] = "hsv"
        elif ep_type in _CCT_TYPES and ep_type in _RGB_TYPES:
            # CCT+RGB (type 14): both sliders — colorTemperatureRange enables temp, hsv enables color
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
            "name": display_name,
            "nicknames": [display_name],
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
                    endpoint_id  = _encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type)
                    manufacturer = _DEVICE_MANUFACTURER
                    devices.append(_build_gh_device(raw_name, ep_type, endpoint_id, site_name, manufacturer,
                                                     room_hint=zone_name, multi_site=multi_site))   
    except Exception:
        logger.exception("Failed building GH device list for duid=%s", duid)
    return devices


def _get_all_gh_devices(user_id: str) -> list[dict]:
    """Return all device + scene objects for a user in Google Home format."""
    devices: list[dict] = []

    udd_table = _ddb().Table(_USER_DEVICE_TABLE)
    sites = udd_table.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])

    # Mirrors lambda_src/alexa's multi_site flag (there it drives a HUB
    # endpoint + isChildOf; here, since Google's schema has no equivalent
    # hierarchy field, it drives a name prefix instead — see
    # _display_name_for_site). Single-site users: no behavior change.
    multi_site = len(sites) > 1

    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", "")
        zone_map  = _load_zone_map(user_id, site_id)
        for device in site.get("devices", []):
            duid = device.get("duid", "")
            if duid:
                devices.extend(_build_device_endpoints_gh(user_id, site_id, site_name, duid, zone_map, multi_site))

    # Scenes — read from S3 per site using the site list already fetched above
    seen_scene_ids: set = set()
    scene_count = 0
    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", "")
        if not site_id:
            continue
        s3_key = f"{user_id}/{site_id}/scenes/scene_data.json"
        try:
            obj  = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
            data = json.loads(obj["Body"].read())
            if isinstance(data, list):
                for scene in data:
                    scene_id = scene.get("sceneId") or scene.get("id", "")
                    if not scene_id or scene_id in seen_scene_ids:
                        continue
                    seen_scene_ids.add(scene_id)
                    scene_name = _display_name_for_site(scene.get("sceneName") or "Scene", site_name, multi_site)
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
        except Exception as exc:
            code = getattr(getattr(exc, "response", {}), "get", lambda *a: "")(
                "Error", {}).get("Code", "")
            if code != "NoSuchKey":
                logger.warning("Scene load failed userId=%s siteId=%s: %s", user_id, site_id, exc)
    logger.info("SYNC scenes: %d for userId=%s", scene_count, user_id)

    logger.info("SYNC: %d total devices for userId=%s", len(devices), user_id)
    return devices


# ── Device state helpers (same DynamoDB table as Alexa) ───────────────────────

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
    """Convert hue(0-360)/saturation(0-100)/value(0-100) to a '#RRGGBB' string."""
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

    # cmdType is determined by what changed: 139=RGB color (hex, color-capable endpoints only),
    # 131/132=onOff (Z2M_APP_DEVICE_CONTROL_STATE_ON/OFF_CHANGE), 108=level, 109=colorTemp,
    # 107=curtain open/stop/close (tri-state, not a binary on/off — keep separate from the Z2M
    # on/off enum, which is specifically for a two-state ON/OFF change). Identical to Alexa lambda.
    # A color-set state dict also carries "level" (brightness) alongside hue/saturation, so this
    # check must come BEFORE the "level" check below, and only fires for _RGB_TYPES endpoints —
    # the `state` dict this function received (also written to device_state via
    # _update_device_state_db by the caller) is left untouched as hue/saturation/level; only the
    # wire payload actually published is hex-converted.
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


# ── Google Home tokens (track linked users) ───────────────────────────────────

def _record_gh_link(user_id: str) -> None:
    """Upsert a record in google_home_tokens when a user links Google Home."""
    try:
        _ddb().Table(_GH_TOKENS_TABLE).put_item(Item={
            "userId":      user_id,
            "agentUserId": user_id,
            "linkedAt":    int(time.time()),
        })
    except Exception as exc:
        logger.warning("Could not record GH link for userId=%s: %s", user_id, exc)


# ── Intent handlers ───────────────────────────────────────────────────────────

def handle_sync(request_id: str, user_id: str) -> dict:
    """action.devices.SYNC — return all devices for the user."""
    _record_gh_link(user_id)

    devices = _get_all_gh_devices(user_id)
    return {
        "requestId": request_id,
        "payload": {
            "agentUserId": user_id,
            "devices":     devices,
        },
    }


def handle_query(request_id: str, user_id: str, payload: dict) -> dict:
    """action.devices.QUERY — return current state for each requested device ID."""
    device_states: dict = {}
    for device in payload.get("devices", []):
        device_id = device.get("id", "")

        if _parse_scene_endpoint_id(device_id):
            # Scene switches always report as off (stateless — tapping ON fires the scene)
            device_states[device_id] = {"online": True, "status": "SUCCESS", "on": False}
            continue

        try:
            duid, mac, epid, ep_type = _decrypt_endpoint(device_id)
        except Exception:
            device_states[device_id] = {"online": False, "status": "ERROR", "errorCode": "deviceNotFound"}
            continue

        state = _get_current_state(duid, mac, epid)
        level = int(state.get("level") or 0)
        on_off_raw = state.get("onOff")
        on_off = bool(on_off_raw if on_off_raw is not None else (level > 0))

        gh_state: dict = {"online": True, "status": "SUCCESS"}

        if ep_type in _COVER_TYPES:
            raw_onoff = int(state.get("onOff") or 2)
            # 2=open/opening → 100%, 0/3=stopped → 50%, 4=close/closing → 0%
            is_stopped = raw_onoff in (0, 3)
            open_pct = {2: 100, 4: 0}.get(raw_onoff, 50)
            gh_state["openPercent"] = open_pct
            gh_state["isRunning"] = False
            gh_state["isPaused"] = is_stopped
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
                fan_mode = _level_to_fan_speed(level)
                gh_state["currentFanSpeedSetting"] = fan_mode

        device_states[device_id] = gh_state

    return {
        "requestId": request_id,
        "payload": {"devices": device_states},
    }


def handle_execute(request_id: str, user_id: str, payload: dict) -> dict:
    """action.devices.EXECUTE — execute commands on devices."""
    results: list[dict] = []

    for command_group in payload.get("commands", []):
        target_ids = [d["id"] for d in command_group.get("devices", [])]
        for execution in command_group.get("execution", []):
            cmd    = execution.get("command", "")
            params = execution.get("params", {})
            _execute_command_group(user_id, target_ids, cmd, params, results)

    return {
        "requestId": request_id,
        "payload": {"commands": results},
    }


def _execute_command_group(user_id: str, device_ids: list[str], cmd: str,
                            params: dict, results: list[dict]) -> None:
    success_ids: list[str] = []
    error_ids:   list[str] = []

    for device_id in device_ids:
        # Scene switch: OnOff ON → activate scene; OFF → no-op
        if scene_id := _parse_scene_endpoint_id(device_id):
            if cmd == "action.devices.commands.OnOff" and params.get("on", False):
                _activate_scene(user_id, scene_id)
                logger.info("Scene activated: sceneId=%s userId=%s", scene_id, user_id)
            success_ids.append(device_id)
            continue

        try:
            duid, mac, epid, eptype = _decrypt_endpoint(device_id)
        except Exception:
            error_ids.append(device_id)
            continue

        try:
            state = _build_iot_state(cmd, params, eptype, duid, mac, epid)
        except ValueError as exc:
            logger.warning("Unsupported command %s for device %s: %s", cmd, device_id, exc)
            error_ids.append(device_id)
            continue

        _publish_device_command(duid, mac, epid, eptype, state, user_id)
        _update_device_state_db(duid, mac, epid, state)
        success_ids.append(device_id)
        logger.info("Execute %s device=%s state=%s", cmd, device_id, state)

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
        # Google Home sends either "temperatureK" or "temperature" (both mean Kelvin)
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
            r_, g_, b_ = r/255.0, g/255.0, b/255.0
            if mx == r_: hue = 60 * ((g_ - b_) / (mx - mn) % 6)
            elif mx == g_: hue = 60 * ((b_ - r_) / (mx - mn) + 2)
            else: hue = 60 * ((r_ - g_) / (mx - mn) + 4)
            return {"hue": int(round(hue)) % 360, "saturation": int(round(sat * 100)),
                    "level": int(round(mx * 100)), "onOff": True}
        raise ValueError(f"Unsupported color format: {color}")

    elif cmd == "action.devices.commands.SetFanSpeed":
        fan_speed = params.get("fanSpeed", "Medium")
        level = _fan_speed_to_level(fan_speed)
        return {"onOff": level > 0, "level": level}

    elif cmd == "action.devices.commands.OpenClose":
        open_pct = int(params.get("openPercent", 0))
        if open_pct >= 50:
            return {"onOff": 2}   # open
        return {"onOff": 4}       # close

    elif cmd == "action.devices.commands.StartStop":
        start = params.get("start", True)
        if not start:
            return {"onOff": 0}   # stop
        return {"onOff": 2}       # start = open

    elif cmd == "action.devices.commands.PauseUnpause":
        return {"onOff": 0}       # pause/stop curtain

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
        return {"currentFanSpeedSetting": params.get("fanSpeed", "Medium"), "on": True}
    if cmd == "action.devices.commands.OpenClose":
        open_pct = params.get("openPercent", 0)
        return {"openPercent": open_pct, "isRunning": False, "isPaused": False}
    if cmd in ("action.devices.commands.StartStop", "action.devices.commands.PauseUnpause"):
        return {"isRunning": False, "isPaused": True, "openPercent": 50}
    if cmd == "action.devices.commands.ActivateScene":
        return {}
    return {}


def _activate_scene(user_id: str, scene_id: str) -> None:
    """Publish scene activation to IoT — same topic/payload as Alexa SceneController."""
    # GSI_scene key is type S — sceneId must be queried as a string.
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
                group_id    = s.get("groupId")
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


# ── Fan speed helpers ─────────────────────────────────────────────────────────

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


# ── Lambda entry point ────────────────────────────────────────────────────────

def lambda_handler(event, context):
    headers = event.get("headers", {}) or {}
    auth    = headers.get("Authorization", "") or headers.get("authorization", "")
    token   = auth.split(" ")[-1].strip() if auth else ""

    # Log full request for debugging — remove once stable
    logger.info("FULL_EVENT headers=%s body=%s token_prefix=%s",
                json.dumps(dict(headers)),
                str(event.get("body", ""))[:500],
                token[:40] if token else "NONE")

    try:
        user_id = _decode_jwt_sub(token)
    except Exception as exc:
        logger.warning("JWT decode failed token_prefix=%s error=%s", token[:40] if token else "NONE", exc)
        return {
            "statusCode": 401,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Unauthorized"}),
        }

    try:
        body       = json.loads(event.get("body") or "{}")
        request_id = body.get("requestId", str(uuid.uuid4()))
        inputs     = body.get("inputs", [])
        if not inputs:
            return _api_response(400, {"error": "No inputs in request"})

        intent = inputs[0].get("intent", "")
        logger.info("Google Home intent: %s userId=%s", intent, user_id)

        if intent == "action.devices.SYNC":
            result = handle_sync(request_id, user_id)

        elif intent == "action.devices.QUERY":
            result = handle_query(request_id, user_id, inputs[0].get("payload", {}))

        elif intent == "action.devices.EXECUTE":
            result = handle_execute(request_id, user_id, inputs[0].get("payload", {}))

        elif intent == "action.devices.DISCONNECT":
            # User unlinked Google Home — remove their record
            try:
                _ddb().Table(_GH_TOKENS_TABLE).delete_item(Key={"userId": user_id})
                logger.info("Google Home unlinked for userId=%s", user_id)
            except Exception as exc:
                logger.warning("Could not delete GH tokens for userId=%s: %s", user_id, exc)
            return _api_response(200, {})

        else:
            logger.warning("Unknown Google Home intent: %s", intent)
            return _api_response(400, {"error": f"Unknown intent: {intent}"})

        return _api_response(200, result)

    except Exception:
        logger.exception("Unhandled error for userId=%s", user_id)
        return _api_response(500, {"error": "Internal error — check Lambda logs"})


def _api_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
