"""
Proactive Alexa Discovery updates.

Triggered by DynamoDB Streams on user_device_details and digilux_scene_data.
Sends AddOrUpdateReport or DeleteReport to the Alexa Event Gateway so Alexa's
device list stays in sync without the user saying "Alexa, discover devices".
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

_DATA_REGION      = os.environ.get("DATA_REGION", "ap-south-1")
_LWA_SECRET_ARN   = os.environ.get("LWA_SECRET_ARN", "")
_LWA_SECRET_REGION = os.environ.get("LWA_SECRET_REGION", "eu-west-1")
_KEY_SECRET_ARN   = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_KEY_SECRET_REGION = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")

_USER_DEVICE_TABLE = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_SCENE_TABLE       = os.environ.get("SCENE_TABLE", "digilux_scene_data")
_LWA_TOKENS_TABLE  = os.environ.get("LWA_TOKENS_TABLE", "alexa_lwa_tokens")
_METADATA_BUCKET   = os.environ.get("METADATA_BUCKET", "digilux-metadata-bucket")

_GATEWAY_URL = os.environ.get("ALEXA_GATEWAY_URL", "https://api.eu.amazonalexa.com/v3/events")

# ── Device-type classification ────────────────────────────────────────────────
_LIGHT_TYPES = frozenset({2, 4, 13, 14})
_CCT_TYPES   = frozenset({13, 14})
_RGB_TYPES   = frozenset({4, 14})
_FAN_TYPES   = frozenset({3})
_COVER_TYPES = frozenset({5})
_TOKEN_URL   = "https://api.amazon.com/auth/o2/token"

# Module-level caches
_lwa_creds: dict | None = None
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


# ── LWA token management ──────────────────────────────────────────────────────

def _get_valid_token(user_id: str) -> str | None:
    table = _ddb().Table(_LWA_TOKENS_TABLE)
    resp = table.get_item(Key={"userId": user_id})
    item = resp.get("Item")
    if not item:
        return None
    if int(item["expiresAt"]) > time.time():
        return item["accessToken"]
    # Refresh
    client_id, client_secret = _get_lwa_creds()
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": item["refreshToken"],
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            logger.warning("userId=%s LWA refresh token invalid (400) — deleting stale record, user must re-link Alexa", user_id)
            table.delete_item(Key={"userId": user_id})
            return None
        raise
    with resp as r:
        body = json.loads(r.read())
    new_token  = body["access_token"]
    new_expiry = int(time.time()) + int(body.get("expires_in", 3600)) - 60
    table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET accessToken = :t, expiresAt = :e",
        ExpressionAttributeValues={":t": new_token, ":e": new_expiry},
    )
    return new_token


# ── Endpoint ID encryption ────────────────────────────────────────────────────

def _encrypt_endpoint(duid: str, ieee_addr: str, ep_id: int, ep_type: int) -> str:
    plaintext = f"{duid}::{ieee_addr}::{ep_id}::{ep_type}".encode()
    key = _get_endpoint_key()
    nonce = hashlib.sha256(plaintext).digest()[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + ciphertext + tag).rstrip(b"=").decode()


def _encode_scene(scene_id: str) -> str:
    return f"scene::{scene_id}"


# ── Device / scene endpoint builders ─────────────────────────────────────────

def _load_zone_map(user_id: str, site_id: str) -> dict:
    """Return {zoneId: zoneName} for every zone in this site (reads S3 once per site)."""
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


def _build_device_endpoints(user_id: str, site_id: str, site_name: str,
                             duid: str, is_primary: bool,
                             zone_map: dict | None = None,
                             multi_site: bool = False,
                             hub_id: str = "") -> list[dict]:
    prefix = f"{user_id}/{site_id}/devices/{duid}/"
    endpoints = []
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
                    if ep_type in {15, 20, 1000}:  # bridge coordinator, LQI sensor — not user-controllable
                        continue
                    raw_name  = ep.get("endpointName") or "Unknown Device"
                    zone_id   = str(ep.get("zoneId") or "")
                    zone_name = (zone_map or {}).get(zone_id, "") if zone_id and zone_id != "0" else ""
                    base_name = f"{zone_name} {raw_name}".strip() if zone_name else raw_name
                    ep_name   = base_name
                    if not ieee_addr or ep_id_raw is None:
                        continue
                    endpoint_id = _encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type)
                    if ep_type in _COVER_TYPES:
                        capabilities = [
                            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                        ]
                    else:
                        capabilities = [
                            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                            {
                                "type": "AlexaInterface", "interface": "Alexa.PowerController",
                                "version": "3",
                                "properties": {
                                    "supported": [{"name": "powerState"}],
                                    "proactivelyReported": True, "retrievable": True,
                                },
                            },
                        ]
                    if ep_type in _LIGHT_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.BrightnessController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "brightness"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                        })
                    if ep_type in _CCT_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.ColorTemperatureController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "colorTemperatureInKelvin"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                        })
                    if ep_type in _RGB_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.ColorController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "color"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                        })
                    if ep_type in _FAN_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.RangeController",
                            "version": "3", "instance": "Digilux.Value",
                            "properties": {
                                "supported": [{"name": "rangeValue"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                            "capabilityResources": {"friendlyNames": [
                                {"@type": "asset", "value": {"assetId": "Alexa.Setting.FanSpeed"}},
                                {"@type": "text",  "value": {"text": "speed", "locale": "en-US"}},
                            ]},
                            "configuration": {
                                "supportedRange": {"minimumValue": 0, "maximumValue": 4, "precision": 1},
                            },
                        })
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.ModeController",
                            "version": "3", "instance": "Digilux.FanSpeed",
                            "properties": {
                                "supported": [{"name": "mode"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                            "capabilityResources": {
                                "friendlyNames": [
                                    {"@type": "asset", "value": {"assetId": "Alexa.Setting.FanSpeed"}},
                                    {"@type": "text",  "value": {"text": "speed", "locale": "en-US"}},
                                ]
                            },
                            "configuration": {
                                "ordered": True,
                                "supportedModes": [
                                    {"value": "FanSpeed.Low", "modeResources": {"friendlyNames": [
                                        {"@type": "asset", "value": {"assetId": "Alexa.Value.Low"}},
                                        {"@type": "text",  "value": {"text": "low", "locale": "en-US"}},
                                    ]}},
                                    {"value": "FanSpeed.Medium", "modeResources": {"friendlyNames": [
                                        {"@type": "asset", "value": {"assetId": "Alexa.Value.Medium"}},
                                        {"@type": "text",  "value": {"text": "medium", "locale": "en-US"}},
                                    ]}},
                                    {"value": "FanSpeed.High", "modeResources": {"friendlyNames": [
                                        {"@type": "asset", "value": {"assetId": "Alexa.Value.High"}},
                                        {"@type": "text",  "value": {"text": "high", "locale": "en-US"}},
                                    ]}},
                                ],
                            },
                        })
                    if ep_type in _COVER_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface", "interface": "Alexa.ModeController",
                            "version": "3", "instance": "Blind.Position",
                            "properties": {
                                "supported": [{"name": "mode"}],
                                "proactivelyReported": True, "retrievable": True,
                            },
                            "capabilityResources": {
                                "friendlyNames": [
                                    {"@type": "text", "value": {"text": "curtain", "locale": "en-US"}},
                                    {"@type": "text", "value": {"text": "blind", "locale": "en-US"}},
                                ]
                            },
                            "configuration": {
                                "ordered": False,
                                "supportedModes": [
                                    {"value": "Blind.Open", "modeResources": {"friendlyNames": [
                                        {"@type": "text", "value": {"text": "Open", "locale": "en-US"}},
                                    ]}},
                                    {"value": "Blind.Stop", "modeResources": {"friendlyNames": [
                                        {"@type": "text", "value": {"text": "Stop", "locale": "en-US"}},
                                    ]}},
                                    {"value": "Blind.Close", "modeResources": {"friendlyNames": [
                                        {"@type": "text", "value": {"text": "Close", "locale": "en-US"}},
                                    ]}},
                                ],
                            },
                        })
                    if ep_type in _FAN_TYPES:
                        display_cat = ["FAN"]
                    elif ep_type in _COVER_TYPES:
                        display_cat = ["INTERIOR_BLIND"]
                    elif ep_type in _LIGHT_TYPES or ep_type == 1:
                        display_cat = ["LIGHT"]
                    else:
                        display_cat = ["OTHER"]
                    endpoints.append({
                        "endpointId":        endpoint_id,
                        "friendlyName":      ep_name,
                        "description":       f"Digilux Device in {site_name}" if site_name else "Digilux Device",
                        "manufacturerName":  ep.get("subDeviceParentManufacturer") or "Digilux",
                        "displayCategories": display_cat,
                        "capabilities":      capabilities,
                        "connections": [],
                        "relationships": {"isChildOf": {"endpointId": hub_id}} if hub_id else {},
                    })
    except Exception as e:
        logger.warning("Failed to build endpoints for duid=%s: %s", duid, e)
    return endpoints


def _hub_eligible_site_ids(sites: list[dict], exclude_site_ids: frozenset[str] = frozenset()) -> frozenset[str]:
    """Site ids that currently qualify for a synthetic HUB endpoint: isAlexaEnabled (default
    False — must be explicitly opted in per site), not excluded, and more than one such site
    exists (single-site rule)."""
    enabled = [s for s in sites if s.get("siteId") not in exclude_site_ids and s.get("isAlexaEnabled", False)]
    if len(enabled) <= 1:
        return frozenset()
    return frozenset(s.get("siteId", "") for s in enabled if s.get("siteId"))


def _get_all_user_endpoints(user_id: str, exclude_site_ids: frozenset[str] = frozenset()) -> list[dict]:
    table = _ddb().Table(_USER_DEVICE_TABLE)
    sites = table.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
    hub_eligible = _hub_eligible_site_ids(sites, exclude_site_ids)
    # isAlexaEnabled defaults to False when absent — a site must be explicitly opted in via
    # /updatesite before it (or its devices/scenes/hub) appear in Alexa at all.
    enabled_site_ids = {s.get("siteId") for s in sites
                         if s.get("siteId") not in exclude_site_ids and s.get("isAlexaEnabled", False)}
    site_name_by_id: dict[str, str] = {}
    endpoints = []
    for site in sites:
        site_id    = site.get("siteId", "")
        site_name  = site.get("siteName", "")
        is_primary = int(site.get("isPrimarySite", 0)) == 1
        if site_id and site_name:
            site_name_by_id[site_id] = site_name
        if site_id not in enabled_site_ids:
            continue
        hub_id = f"site::{site_id}" if site_id in hub_eligible else ""
        if hub_id:
            endpoints.append({
                "endpointId":        hub_id,
                "friendlyName":      site_name or site_id,
                "description":       "Digilux Home Hub",
                "manufacturerName":  "Digilux",
                "displayCategories": ["HUB"],
                "capabilities": [
                    {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                    {
                        "type": "AlexaInterface",
                        "interface": "Alexa.EndpointHealth",
                        "version": "3",
                        "properties": {
                            "supported": [{"name": "connectivity"}],
                            "proactivelyReported": False,
                            "retrievable": False,
                        },
                    },
                ],
                "connections":   [],
                "relationships": {},
            })
        zone_map = _load_zone_map(user_id, site_id)
        for device in site.get("devices", []):
            duid = device.get("duid", "")
            if duid:
                endpoints.extend(_build_device_endpoints(
                    user_id, site_id, site_name, duid, is_primary, zone_map,
                    multi_site=bool(hub_id), hub_id=hub_id))
    # Scenes — scan S3 directly under user prefix to catch all sites (source of truth)
    scenes_s3: list[tuple[str, dict]] = []  # (site_id, scene_dict)
    seen_scene_ids: set = set()
    try:
        paginator = _s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=f"{user_id}/", Delimiter="/"):
            for prefix_obj in page.get("CommonPrefixes", []):
                site_prefix = prefix_obj.get("Prefix", "")
                # Extract site_id from path: "{user_id}/{site_id}/"
                parts = site_prefix.rstrip("/").split("/")
                s_id = parts[-1] if len(parts) >= 2 else ""
                if s_id not in enabled_site_ids:
                    continue
                s3_key = f"{site_prefix}scenes/scene_data.json"
                try:
                    obj = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
                    data = json.loads(obj["Body"].read())
                    if isinstance(data, list):
                        scenes_s3.extend((s_id, sc) for sc in data)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Could not scan S3 for scenes userId=%s: %s", user_id, exc)
    for s_id, scene in scenes_s3:
        scene_id = scene.get("sceneId") or scene.get("id", "")
        if not scene_id or scene_id in seen_scene_ids:
            continue
        seen_scene_ids.add(scene_id)
        scene_name = scene.get("sceneName") or "Scene"
        if len(enabled_site_ids) > 1 and s_id and s_id in site_name_by_id:
            scene_name = f"{site_name_by_id[s_id]} {scene_name}"
        endpoints.append({
                "endpointId":        _encode_scene(scene_id),
                "friendlyName":      scene_name,
                "description":       scene.get("description") or "Digilux Scene",
                "manufacturerName":  "Digilux",
                "displayCategories": ["SCENE_TRIGGER"],
                "capabilities": [
                    {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                    {
                        "type": "AlexaInterface",
                        "interface": "Alexa.SceneController",
                        "version": "3",
                        "supportsDeactivation": False,
                        "proactivelyReported": True,
                    },
                ],
                "connections": [],
                "relationships": {},
            })
    logger.info("Discover: %d total endpoints for userId=%s", len(endpoints), user_id)
    return endpoints


# ── Event Gateway calls ───────────────────────────────────────────────────────

def _post_event(lwa_token: str, payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(_GATEWAY_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {lwa_token}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 202:
                raise RuntimeError(f"Event Gateway returned {r.status}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Event Gateway {e.code}: {err_body}") from e


def _send_add_or_update(lwa_token: str, endpoints: list[dict]) -> None:
    _post_event(lwa_token, {
        "event": {
            "header": {"namespace": "Alexa.Discovery", "name": "AddOrUpdateReport",
                       "messageId": str(uuid.uuid4()), "payloadVersion": "3"},
            "payload": {
                "endpoints": endpoints,
                "scope": {"type": "BearerToken", "token": lwa_token},
            },
        }
    })
    logger.info("AddOrUpdateReport sent: %d endpoints", len(endpoints))


def _send_delete(lwa_token: str, endpoint_ids: list[str], batch_size: int = 1) -> None:
    scope = {"type": "BearerToken", "token": lwa_token}
    for i in range(0, len(endpoint_ids), batch_size):
        batch = endpoint_ids[i:i + batch_size]
        try:
            # endpoints field (not deletedEndpoints) with scope at payload level
            _post_event(lwa_token, {
                "event": {
                    "header": {"namespace": "Alexa.Discovery", "name": "DeleteReport",
                               "messageId": str(uuid.uuid4()), "payloadVersion": "3"},
                    "payload": {
                        "endpoints": [{"endpointId": eid} for eid in batch],
                        "scope": scope,
                    },
                }
            })
            logger.info("DeleteReport batch sent: %d endpoints (offset %d)", len(batch), i)
        except Exception as exc:
            logger.warning("DeleteReport batch %d failed: %s", i, exc)


# ── Stream record parsing ─────────────────────────────────────────────────────

def _extract_user_id(record: dict) -> str | None:
    for img_key in ("NewImage", "OldImage"):
        img = record.get("dynamodb", {}).get(img_key, {})
        uid = img.get("userId", {}).get("S")
        if uid:
            return uid
    return None


def _is_delete(record: dict) -> bool:
    return record.get("eventName") == "REMOVE"


# ── Lambda handler ────────────────────────────────────────────────────────────

def _refresh_for_user(user_id: str) -> None:
    """Send AddOrUpdateReport with current endpoints for one user (called by other Lambdas)."""
    lwa_token = _get_valid_token(user_id)
    if not lwa_token:
        logger.info("userId=%s has not linked Alexa — skipping refresh", user_id)
        return
    endpoints = _get_all_user_endpoints(user_id)
    if endpoints:
        _send_add_or_update(lwa_token, endpoints)
        logger.info("Per-user refresh complete: userId=%s %d endpoints", user_id, len(endpoints))


def _scan_s3_for_all_endpoint_ids(user_id: str) -> set[str]:
    """
    Scan ALL endpoints.json files under user_id/ in the metadata bucket,
    extracting the duid directly from the S3 path.

    Path structure: {user_id}/{site_id}/devices/{duid}/{bridge}/{mac}/endpoints.json

    This bypasses DynamoDB entirely, so it finds endpoint IDs for devices that
    were removed from user_device_details but whose S3 files still exist.
    """
    ids: set[str] = set()
    s3 = _s3_client()
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=f"{user_id}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/endpoints.json"):
                    continue
                # parts: [user_id, site_id, "devices", duid, bridge, mac, "endpoints.json"]
                parts = key.split("/")
                if len(parts) < 7 or parts[2] != "devices":
                    continue
                duid = parts[3]
                try:
                    resp = s3.get_object(Bucket=_METADATA_BUCKET, Key=key)
                    ep_list = json.loads(resp["Body"].read())
                    for ep in (ep_list if isinstance(ep_list, list) else []):
                        if ep.get("isHidden"):
                            continue
                        ieee_addr = ep.get("subdeviceIeeeAddress", "")
                        ep_id_raw = ep.get("endpointId")
                        ep_type   = int(ep.get("endpointType", 1))
                        if not ieee_addr or ep_id_raw is None:
                            continue
                        ids.add(_encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type))
                except Exception as e:
                    logger.warning("Failed to read %s: %s", key, e)
    except Exception as e:
        logger.warning("S3 scan failed for userId=%s: %s", user_id, e)
    return ids


def _collect_site_device_ids(user_id: str, site_id: str) -> set[str]:
    """Same scan/decode pattern as _scan_s3_for_all_endpoint_ids, scoped to one site's devices/
    prefix. Reads S3 directly — not DynamoDB — so it works regardless of the site's current
    isAlexaEnabled value or whether its DynamoDB item still exists."""
    ids: set[str] = set()
    s3 = _s3_client()
    prefix = f"{user_id}/{site_id}/devices/"
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("/endpoints.json"):
                    continue
                parts = key.split("/")
                if len(parts) < 7 or parts[2] != "devices":
                    continue
                duid = parts[3]
                try:
                    resp = s3.get_object(Bucket=_METADATA_BUCKET, Key=key)
                    ep_list = json.loads(resp["Body"].read())
                    for ep in (ep_list if isinstance(ep_list, list) else []):
                        if ep.get("isHidden"):
                            continue
                        ieee_addr = ep.get("subdeviceIeeeAddress", "")
                        ep_id_raw = ep.get("endpointId")
                        ep_type   = int(ep.get("endpointType", 1))
                        if not ieee_addr or ep_id_raw is None:
                            continue
                        ids.add(_encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type))
                except Exception as e:
                    logger.warning("Failed to read %s: %s", key, e)
    except Exception as e:
        logger.warning("Site device scan failed for userId=%s siteId=%s: %s", user_id, site_id, e)
    return ids


def _collect_site_scene_ids(user_id: str, site_id: str) -> set[str]:
    """Reads {user_id}/{site_id}/scenes/scene_data.json directly, returns encoded scene endpoint ids."""
    ids: set[str] = set()
    s3_key = f"{user_id}/{site_id}/scenes/scene_data.json"
    try:
        obj = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
        data = json.loads(obj["Body"].read())
        if isinstance(data, list):
            for scene in data:
                scene_id = scene.get("sceneId") or scene.get("id", "")
                if scene_id:
                    ids.add(_encode_scene(scene_id))
    except Exception as e:
        logger.warning("Site scene scan failed for userId=%s siteId=%s: %s", user_id, site_id, e)
    return ids


def _get_endpoint_ids_from_bucket(user_id: str, bucket: str) -> set[str]:
    """Compute endpoint IDs by reading endpoints.json files from any bucket (same path structure)."""
    ids: set[str] = set()
    table = _ddb().Table(_USER_DEVICE_TABLE)
    sites = table.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
    s3 = boto3.client("s3", region_name=_DATA_REGION)
    for site in sites:
        site_id = site.get("siteId", "")
        for device in site.get("devices", []):
            duid = device.get("duid", "")
            if not duid:
                continue
            prefix = f"{user_id}/{site_id}/devices/{duid}/"
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        if not obj["Key"].endswith("/endpoints.json"):
                            continue
                        try:
                            resp = s3.get_object(Bucket=bucket, Key=obj["Key"])
                            ep_list = json.loads(resp["Body"].read())
                            for ep in (ep_list if isinstance(ep_list, list) else []):
                                if ep.get("isHidden"):
                                    continue
                                ieee_addr = ep.get("subdeviceIeeeAddress", "")
                                ep_id_raw = ep.get("endpointId")
                                ep_type   = int(ep.get("endpointType", 1))
                                if not ieee_addr or ep_id_raw is None:
                                    continue
                                ids.add(_encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type))
                        except Exception:
                            pass
            except Exception:
                pass
    return ids


def _clean_slate_for_user(user_id: str, stale_bucket: str) -> dict:
    """Delete stale endpoints (from stale_bucket) then push correct current ones."""
    lwa_token = _get_valid_token(user_id)
    if not lwa_token:
        return {"skipped": True}

    stale_ids = _get_endpoint_ids_from_bucket(user_id, stale_bucket)
    current_endpoints = _get_all_user_endpoints(user_id)
    current_ids = {ep["endpointId"] for ep in current_endpoints}

    to_delete = stale_ids - current_ids
    if to_delete:
        _send_delete(lwa_token, list(to_delete))
        logger.info("Clean slate: deleted %d stale endpoints for userId=%s", len(to_delete), user_id)

    if current_endpoints:
        _send_add_or_update(lwa_token, current_endpoints)
        logger.info("Clean slate: pushed %d current endpoints for userId=%s", len(current_endpoints), user_id)

    return {"deleted": len(to_delete), "pushed": len(current_endpoints)}


def lambda_handler(event, context):
    # Admin: push updated capabilities to ALL linked users (no user action needed)
    if event.get("_admin_refresh_all"):
        from refresh_all_users import refresh_all_users
        result = refresh_all_users()
        logger.info("Admin refresh complete: %s", result)
        return result

    # Admin: brute-force delete all plausible old endpoint IDs, then push current ones
    if event.get("_admin_brute_delete"):
        # Accept explicit user_ids list to bypass DynamoDB Scan permission requirement
        if event.get("user_ids"):
            users = event["user_ids"]
        else:
            from refresh_all_users import get_all_linked_users
            users = get_all_linked_users()
        ieee_addrs = event.get("ieee_addrs", [])
        ep_ids     = event.get("ep_ids",    list(range(0, 20)))
        ep_types   = event.get("ep_types",  [1, 2, 3, 4, 5, 13, 14, 15, 20, 1000])
        results = {}
        for uid in users:
            lwa_token = _get_valid_token(uid)
            if not lwa_token:
                results[uid] = "no_token"
                continue
            current_eps  = _get_all_user_endpoints(uid)
            current_ids  = {ep["endpointId"] for ep in current_eps}
            # Generate all plausible endpoint IDs from known ieee addresses
            all_possible = set()
            for ieee in ieee_addrs:
                for ep_id in ep_ids:
                    for ep_type in ep_types:
                        all_possible.add(_encrypt_endpoint(event.get("duid", ""), ieee, ep_id, ep_type))
            to_delete = all_possible - current_ids
            if to_delete:
                _send_delete(lwa_token, list(to_delete))
            if current_eps:
                _send_add_or_update(lwa_token, current_eps)
            results[uid] = {"deleted": len(to_delete), "pushed": len(current_eps)}
            logger.info("Brute delete: userId=%s deleted=%d pushed=%d", uid, len(to_delete), len(current_eps))
        return results

    # Pre-delete hook: invoked by delete_device_data BEFORE S3 files are removed.
    # Reads endpoint IDs from the about-to-be-deleted S3 prefix, sends DeleteReport,
    # then sends AddOrUpdateReport with remaining valid endpoints.
    if event.get("_pre_delete_s3_cleanup"):
        user_id = event.get("user_id", "")
        prefix  = event.get("prefix", "")
        if not user_id or not prefix:
            return {"error": "user_id and prefix required"}
        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            return {"skipped": "no_alexa_link"}
        # Read endpoint IDs from the files that are about to be deleted
        to_delete_ids: set[str] = set()
        s3 = _s3_client()
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if not key.endswith("/endpoints.json"):
                        continue
                    parts = key.split("/")
                    if len(parts) < 7 or parts[2] != "devices":
                        continue
                    duid = parts[3]
                    try:
                        resp = s3.get_object(Bucket=_METADATA_BUCKET, Key=key)
                        ep_list = json.loads(resp["Body"].read())
                        for ep in (ep_list if isinstance(ep_list, list) else []):
                            if ep.get("isHidden"):
                                continue
                            ieee_addr = ep.get("subdeviceIeeeAddress", "")
                            ep_id_raw = ep.get("endpointId")
                            ep_type   = int(ep.get("endpointType", 1))
                            if not ieee_addr or ep_id_raw is None:
                                continue
                            to_delete_ids.add(_encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type))
                    except Exception as e:
                        logger.warning("Pre-delete: failed reading %s: %s", key, e)
        except Exception as e:
            logger.warning("Pre-delete: S3 scan failed: %s", e)
        logger.info("Pre-delete cleanup: userId=%s prefix=%s found=%d",
                    user_id, prefix, len(to_delete_ids))
        if to_delete_ids:
            _send_delete(lwa_token, list(to_delete_ids))
        # Push remaining valid endpoints so Alexa is fully up to date
        remaining = _get_all_user_endpoints(user_id)
        # Exclude endpoints that are about to be deleted
        remaining = [ep for ep in remaining if ep["endpointId"] not in to_delete_ids]
        if remaining:
            _send_add_or_update(lwa_token, remaining)
        return {"deleted": len(to_delete_ids), "remaining_pushed": len(remaining)}

    # Disable a single site's Alexa visibility (or pre-delete cleanup before the site itself is
    # removed) — sends DeleteReport for that site's hub + devices + scenes, PLUS any other site's
    # hub that becomes unnecessary as a ripple effect (enabled-site count dropping to <=1), then
    # pushes a fresh AddOrUpdateReport for whatever remains. Only talks to Alexa's Event Gateway —
    # never writes to DynamoDB or S3. Computed from our own DB/S3 state unconditionally (not by
    # diffing against Alexa's current state), so this is idempotent and self-healing even if the
    # site's hub/devices were already out of sync in Alexa for an unrelated reason.
    if event.get("_disable_site_cleanup"):
        user_id, site_id = event.get("user_id", ""), event.get("site_id", "")
        if not user_id or not site_id:
            return {"error": "user_id and site_id required"}
        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            return {"skipped": "no_alexa_link"}

        # Target site's own ids — hub_id needs no lookup (deterministic string); devices/scenes
        # read from S3 directly, independent of the site's current isAlexaEnabled value.
        hub_id = f"site::{site_id}"
        to_delete_ids = {hub_id} | _collect_site_device_ids(user_id, site_id) | _collect_site_scene_ids(user_id, site_id)

        # Ripple: recompute hub-eligibility before vs after excluding this site. Works whether the
        # caller already flipped isAlexaEnabled=False in DDB (disable path) or the record still
        # fully exists (delete-site path, called before deletion) — exclude_site_ids forces it out
        # either way.
        sites = _ddb().Table(_USER_DEVICE_TABLE).query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
        hub_before = _hub_eligible_site_ids(sites)
        hub_after = _hub_eligible_site_ids(sites, exclude_site_ids=frozenset({site_id}))
        ripple_hub_ids = {f"site::{sid}" for sid in (hub_before - hub_after) if sid != site_id}
        to_delete_ids |= ripple_hub_ids

        if to_delete_ids:
            _send_delete(lwa_token, list(to_delete_ids))

        # Fresh AddOrUpdateReport, computed with this site excluded so ripple-affected sites'
        # devices are rebuilt with hub_id="" from the start — no dangling isChildOf reference.
        remaining = _get_all_user_endpoints(user_id, exclude_site_ids=frozenset({site_id}))
        if remaining:
            _send_add_or_update(lwa_token, remaining)

        logger.info(
            "_disable_site_cleanup: userId=%s site_id=%s deleted=%d ripple=%s remaining=%d",
            user_id, site_id, len(to_delete_ids), ripple_hub_ids, len(remaining),
        )
        return {"deleted": len(to_delete_ids), "ripple_hub_deleted": list(ripple_hub_ids),
                "remaining_pushed": len(remaining)}

    # Admin: scan S3 directly for ALL endpoint IDs (correct duid from path), delete stale ones
    if event.get("_admin_s3_scan_delete"):
        user_ids = event.get("user_ids", [])
        if not user_ids:
            return {"error": "user_ids required"}
        results = {}
        for uid in user_ids:
            lwa_token = _get_valid_token(uid)
            if not lwa_token:
                results[uid] = "no_token"
                continue
            # All IDs computed from S3 (includes stale files still on disk)
            all_s3_ids = _scan_s3_for_all_endpoint_ids(uid)
            # Current valid IDs from DynamoDB + S3 (what Alexa SHOULD have)
            current_eps = _get_all_user_endpoints(uid)
            current_ids = {ep["endpointId"] for ep in current_eps}
            stale = all_s3_ids - current_ids
            logger.info("S3 scan: userId=%s s3_total=%d current=%d stale=%d",
                        uid, len(all_s3_ids), len(current_ids), len(stale))
            if stale:
                _send_delete(lwa_token, list(stale))
            if current_eps:
                _send_add_or_update(lwa_token, current_eps)
            results[uid] = {"s3_total": len(all_s3_ids), "stale_deleted": len(stale),
                            "current_pushed": len(current_eps)}
        return results

    # Admin: delete stale endpoints from old bucket, push correct current ones
    if event.get("_admin_clean_slate"):
        stale_bucket = event.get("stale_bucket", "digilux-honeywell-metadata")
        from refresh_all_users import get_all_linked_users
        users = get_all_linked_users()
        results = {}
        for uid in users:
            results[uid] = _clean_slate_for_user(uid, stale_bucket)
        logger.info("Clean slate complete: %s", results)
        return results

    # Single endpoint delete: invoked async by delete_device_data after endpoint-level deletion.
    # Receives raw endpoint data, computes the encrypted endpoint ID, sends DeleteReport,
    # then pushes AddOrUpdateReport for remaining endpoints so Alexa stays in sync.
    if event.get("_delete_endpoint"):
        user_id   = event.get("user_id", "")
        duid      = event.get("duid", "")
        ieee_addr = event.get("ieee_addr", "")
        ep_id     = event.get("ep_id")
        ep_type   = event.get("ep_type", 1)
        if not user_id or not duid or not ieee_addr or ep_id is None:
            return {"error": "missing required fields"}
        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            return {"skipped": "no_alexa_link"}
        endpoint_id = _encrypt_endpoint(duid, ieee_addr, int(ep_id), int(ep_type))
        _send_delete(lwa_token, [endpoint_id])
        remaining = _get_all_user_endpoints(user_id)
        if remaining:
            _send_add_or_update(lwa_token, remaining)
        logger.info("_delete_endpoint: userId=%s duid=%s ep_id=%s remaining=%d",
                    user_id, duid, ep_id, len(remaining))
        return {"deleted": 1, "remaining_pushed": len(remaining)}

    # Admin: delete specific endpoint IDs then push correct AddOrUpdateReport
    if event.get("_delete_endpoint_ids"):
        uid = event.get("user_id", "")
        ids_to_delete = event.get("endpoint_ids", [])
        if not uid or not ids_to_delete:
            return {"error": "user_id and endpoint_ids required"}
        lwa_token = _get_valid_token(uid)
        if not lwa_token:
            return {"error": "no LWA token for user"}
        _send_delete(lwa_token, ids_to_delete)
        _refresh_for_user(uid)
        return {"statusCode": 200, "deleted": ids_to_delete}

    # Per-user refresh: invoked by create_update_device / create_update_data after S3 writes
    if "_refresh_user_id" in event:
        _refresh_for_user(event["_refresh_user_id"])
        return {"statusCode": 200}

    for record in event.get("Records", []):
        user_id = _extract_user_id(record)
        if not user_id:
            logger.warning("Could not extract userId from stream record — skipping")
            continue

        lwa_token = _get_valid_token(user_id)
        if not lwa_token:
            logger.info("userId=%s has not linked Alexa — skipping proactive update", user_id)
            continue

        try:
            event_name = record.get("eventName", "")
            source_arn = record.get("eventSourceARN", "")

            if event_name == "REMOVE":
                old_img = record.get("dynamodb", {}).get("OldImage", {})
                to_delete: list[str] = []

                if _SCENE_TABLE in source_arn:
                    # Scene deleted — endpoint ID is deterministic
                    scene_id = (old_img.get("sceneId", {}).get("N")
                                or old_img.get("sceneId", {}).get("S"))
                    if scene_id:
                        to_delete.append(_encode_scene(str(int(float(scene_id)))))
                        logger.info("Stream REMOVE scene: userId=%s sceneId=%s", user_id, scene_id)

                elif _USER_DEVICE_TABLE in source_arn:
                    # Device/site deleted — read S3 endpoint files (still present if stream
                    # fires before S3 cleanup; gracefully skips if already gone)
                    site_id = old_img.get("siteId", {}).get("S", "")
                    for dev in old_img.get("devices", {}).get("L", []):
                        duid = dev.get("M", {}).get("duid", {}).get("S")
                        if not duid:
                            continue
                        prefix = f"{user_id}/{site_id}/devices/{duid}/"
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
                                        ieee = ep.get("subdeviceIeeeAddress", "")
                                        ep_id = ep.get("endpointId")
                                        ep_type = int(ep.get("endpointType", 1))
                                        if ieee and ep_id is not None:
                                            to_delete.append(_encrypt_endpoint(duid, ieee, int(ep_id), ep_type))
                        except Exception as exc:
                            logger.warning("S3 endpoint read failed duid=%s: %s", duid, exc)
                    logger.info("Stream REMOVE device: userId=%s site=%s endpoints_to_delete=%d",
                                user_id, site_id, len(to_delete))

                if to_delete:
                    _send_delete(lwa_token, to_delete)

                # Push remaining valid endpoints so Alexa is fully up to date
                remaining = _get_all_user_endpoints(user_id)
                if remaining:
                    _send_add_or_update(lwa_token, remaining)
                else:
                    logger.info("No endpoints remaining for userId=%s after delete", user_id)

            else:
                # INSERT or MODIFY — detect removed devices, then push updated list
                if event_name == "MODIFY" and _USER_DEVICE_TABLE in source_arn:
                    old_img = record.get("dynamodb", {}).get("OldImage", {})
                    new_img = record.get("dynamodb", {}).get("NewImage", {})
                    old_duids = {d.get("M", {}).get("duid", {}).get("S")
                                 for d in old_img.get("devices", {}).get("L", [])}
                    new_duids = {d.get("M", {}).get("duid", {}).get("S")
                                 for d in new_img.get("devices", {}).get("L", [])}
                    removed_duids = old_duids - new_duids
                    if removed_duids:
                        site_id = old_img.get("siteId", {}).get("S", "")
                        to_delete: list[str] = []
                        for duid in removed_duids:
                            if not duid:
                                continue
                            prefix = f"{user_id}/{site_id}/devices/{duid}/"
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
                                            ieee = ep.get("subdeviceIeeeAddress", "")
                                            ep_id = ep.get("endpointId")
                                            ep_type = int(ep.get("endpointType", 1))
                                            if ieee and ep_id is not None:
                                                to_delete.append(_encrypt_endpoint(duid, ieee, int(ep_id), ep_type))
                            except Exception as exc:
                                logger.warning("S3 endpoint read failed duid=%s: %s", duid, exc)
                        if to_delete:
                            _send_delete(lwa_token, to_delete)
                            logger.info("Stream MODIFY removed devices: userId=%s duids=%s deleted=%d",
                                        user_id, removed_duids, len(to_delete))

                endpoints = _get_all_user_endpoints(user_id)
                if endpoints:
                    _send_add_or_update(lwa_token, endpoints)
                logger.info("Stream %s: userId=%s endpoints=%d", event_name, user_id, len(endpoints) if endpoints else 0)

        except Exception:
            logger.exception("Failed proactive update for userId=%s", user_id)

    return {"statusCode": 200}
