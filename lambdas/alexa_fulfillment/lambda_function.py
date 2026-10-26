"""
Alexa Smart Home Lambda — entry handler for Digilux.

Handles:
  Alexa.Authorization.AcceptGrant                    — account linking
  Alexa.Discovery.Discover                           — return all devices + scenes
  Alexa.SceneController.Activate                     — execute a scene via IoT
  Alexa.PowerController.TurnOn/TurnOff               — toggle device on/off
  Alexa.BrightnessController.SetBrightness           — set absolute brightness 0-100
  Alexa.BrightnessController.AdjustBrightness        — relative brightness delta
  Alexa.RangeController.SetRangeValue/AdjustRangeValue — fan speed / cover position 0-100
  Alexa.ModeController.SetMode/AdjustMode            — fan speed preset (Low/Medium/High)
  Alexa.ColorTemperatureController.SetColorTemperature — CCT lights (Kelvin↔Mired)
  Alexa.ColorController.SetColor                     — RGB lights (HSB colour)
  Alexa.ReportState                                  — return current device state
"""
from __future__ import annotations
import base64
import colorsys
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
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Env vars (injected by CDK ApiStack _alexa_env block) ─────────────────────
_DATA_REGION       = os.environ.get("DATA_REGION", "ap-south-1")
_LWA_SECRET_ARN    = os.environ.get("LWA_SECRET_ARN", "")
_LWA_SECRET_REGION = os.environ.get("LWA_SECRET_REGION", "eu-west-1")
_KEY_SECRET_ARN    = os.environ.get("ENDPOINT_KEY_SECRET_ARN", "")
_KEY_SECRET_REGION = os.environ.get("ENDPOINT_KEY_REGION", "eu-west-1")
# Same secret get_data_function uses to decrypt deviceMacAddress on user_device_details records.
_DEVICE_SECRET_ARN    = os.environ.get("DEVICE_TABLE_SECRET_ARN", "")
_DEVICE_SECRET_REGION = os.environ.get("DEVICE_TABLE_SECRET_REGION", _DATA_REGION)

_LWA_TOKENS_TABLE   = os.environ.get("LWA_TOKENS_TABLE",         "alexa_lwa_tokens")
_USER_DEVICE_TABLE  = os.environ.get("USER_DEVICE_DETAILS_TABLE", "user_device_details")
_SCENE_TABLE        = os.environ.get("SCENE_TABLE",               "digilux_scene_data")
_DEVICE_STATE_TABLE = os.environ.get("DEVICE_STATE_TABLE",        "device_state")
_METADATA_BUCKET    = os.environ.get("METADATA_BUCKET",           "digilux-metadata-bucket")
_CREATE_UPDATE_FN   = os.environ.get("CREATE_UPDATE_FN",          "")

_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
_GATEWAY_URL = os.environ.get("ALEXA_GATEWAY_URL", "https://api.eu.amazonalexa.com/v3/events")

# ── Device-type classification ────────────────────────────────────────────────
_LIGHT_TYPES = frozenset({2, 4, 13, 14})   # BrightnessController
_CCT_TYPES   = frozenset({13, 14})          # ColorTemperatureController (colorTemp in Mireds)
_RGB_TYPES   = frozenset({4, 14})           # ColorController (hue/saturation)
_FAN_TYPES   = frozenset({3})               # RangeController + ModeController
_COVER_TYPES = frozenset({5})               # RangeController (positional)

# Module-level client caches (warm across invocations)
_lwa_creds: dict | None = None
_endpoint_key: bytes | None = None
_device_secret: bytes | None = None
_dynamodb = None
_s3 = None
_iot = None
_bridge_cache: dict = {}  # {duid: bridge_addr} — warm across invocations
_lambda_client = None


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


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=_DATA_REGION)
    return _lambda_client


def _notify_alexa_enabled(user_id: str, site_id: str) -> None:
    """Invoke CREATE_UPDATE_FN async to set isAlexaEnabled=true for one site."""
    try:
        _get_lambda_client().invoke(
            FunctionName=_CREATE_UPDATE_FN,
            InvocationType="Event",
            Payload=json.dumps({
                "userId":         user_id,
                "siteId":         site_id,
                "isAlexaEnabled": True,
            }).encode(),
        )
        logger.info("AcceptGrant: isAlexaEnabled=true sent userId=%s siteId=%s", user_id, site_id)
    except Exception as exc:
        logger.warning("AcceptGrant: notify failed userId=%s siteId=%s error=%s", user_id, site_id, exc)


def _notify_alexa_enabled_on_accept_grant(user_id: str) -> None:
    """
    After AcceptGrant, set isAlexaEnabled=true on the correct site(s).

    Strategy:
      1. Query user_device_details for all site records of this user.
      2. Find the record with siteSelected=true — set by the Flutter app when
         the user picks a site before initiating Alexa linking.
      3. If found → enable only that site (targeted).
      4. If not found (user linked directly from Alexa app/web/voice without
         going through Flutter first) → enable all sites (safe fallback).

    Non-fatal — failure is logged only, linking is already complete.
    """
    if not _CREATE_UPDATE_FN:
        logger.warning("AcceptGrant: CREATE_UPDATE_FN not set — skipping isAlexaEnabled update")
        return
    try:
        sites = _ddb().Table(_USER_DEVICE_TABLE).query(
            KeyConditionExpression=Key("userId").eq(user_id)
        ).get("Items", [])
    except Exception as exc:
        logger.warning("AcceptGrant: failed to query sites for userId=%s error=%s", user_id, exc)
        return

    selected = [s for s in sites if s.get("siteSelected") and s.get("siteId")]

    if selected:
        logger.info("AcceptGrant: siteSelected found — enabling %d site(s) for userId=%s",
                    len(selected), user_id)
        for site in selected:
            _notify_alexa_enabled(user_id, site["siteId"])
    else:
        logger.info("AcceptGrant: siteSelected not set — enabling all %d site(s) for userId=%s "
                    "(fallback: user linked without Flutter context)", len(sites), user_id)
        for site in sites:
            site_id = site.get("siteId", "")
            if site_id:
                _notify_alexa_enabled(user_id, site_id)


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


def _get_device_secret() -> bytes:
    """Secret get_data_function uses to encrypt deviceMacAddress on user_device_details
    records — same secret, same encoding, so we can decrypt it here too."""
    global _device_secret
    if _device_secret is None:
        sm = boto3.client("secretsmanager", region_name=_DEVICE_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_DEVICE_SECRET_ARN)["SecretString"]
        _device_secret = json.loads(raw)["device_table_secret"].strip('"').encode()
    return _device_secret


def _decrypt_device_mac(payload_encoded: str) -> str:
    """Reverse get_data_function's decrypt_text — same AES-GCM layout
    (12-byte nonce + 16-byte tag + ciphertext, all base64-encoded together)."""
    try:
        data = base64.b64decode(payload_encoded)
        nonce, tag, ciphertext = data[:12], data[12:28], data[28:]
        cipher = AES.new(_get_device_secret(), AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except Exception:
        logger.exception("Failed to decrypt deviceMacAddress")
        return ""


def _get_valid_token(user_id: str) -> str | None:
    """Same refresh-on-expiry pattern as alexa_state_reporter — ported here rather than
    reused there so this one-off admin action runs on infrastructure with a confirmed-
    working Crypto layer (alexa_state_reporter is currently missing its pycrypto layer
    entirely — a separate, pre-existing bug, not something to route around silently)."""
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


def _send_delete_report(lwa_token: str, endpoint_ids: list[str]) -> None:
    """Alexa.Discovery.DeleteReport — explicitly removes stale endpoints from the
    customer's Alexa device list. Confirmed empirically 2026-08-22 that Discover.Response
    simply omitting an endpointId does NOT remove it — this is the only supported
    mechanism for actually clearing a stale/duplicate device out of the Alexa app."""
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


# ── JWT / token helpers ───────────────────────────────────────────────────────

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


def _exchange_lwa_code(code: str) -> dict:
    """Exchange LWA authorization code for access + refresh tokens."""
    client_id, client_secret = _get_lwa_creds()
    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ── Endpoint ID helpers ───────────────────────────────────────────────────────

def _encrypt_endpoint(duid: str, ieee_addr: str, ep_id: int, ep_type: int) -> str:
    plaintext = f"{duid}::{ieee_addr}::{ep_id}::{ep_type}".encode()
    key = _get_endpoint_key()
    nonce = hashlib.sha256(plaintext).digest()[:12]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return base64.urlsafe_b64encode(nonce + ciphertext + tag).rstrip(b"=").decode()


def _encode_scene(scene_id: str) -> str:
    return f"scene::{scene_id}"


def _parse_scene_endpoint_id(endpoint_id: str) -> str | None:
    """Return sceneId if the endpointId is a scene, else None."""
    logger.info("Endpoint Id as scene = %s", endpoint_id)
    if endpoint_id.startswith("scene::"):
        return endpoint_id[len("scene::"):]
    return None


# ── Endpoint builders ─────────────────────────────────────────────────────────

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


def _build_device_endpoints(user_id: str, site_id: str, site_name: str, duid: str,
                             zone_map: dict | None = None, hub_id: str = "") -> list[dict]:
    """Read endpoints.json from S3 for a device and build Alexa endpoint objects.

    Scoped to the device's current bridge only (via _get_bridge_address /
    deviceMacAddress) — a device that's been re-paired keeps its old bridge's
    endpoints.json files sitting in S3 under the old bridge subfolder, and scanning
    the whole devices/{duid}/ prefix would surface those stale devices again,
    duplicating every currently-connected device once per past re-pairing."""
    endpoints = []
    bridge_addr = _get_bridge_address(user_id, duid)
    if not bridge_addr:
        logger.warning("No current bridge address for duid=%s userId=%s — skipping "
                       "endpoint discovery rather than falling back to a full scan", duid, user_id)
        return endpoints
    prefix = f"{user_id}/{site_id}/devices/{duid}/{bridge_addr}/"
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
                    if ep_type in {15, 20, 1000}:  # bridge coordinator / LQI — skip
                        continue
                    raw_name  = ep.get("endpointName") or "Unknown Device"
                    zone_id   = str(ep.get("zoneId") or "")
                    zone_name = (zone_map or {}).get(zone_id, "") if zone_id and zone_id != "0" else ""
                    ep_name   = f"{zone_name} {raw_name}".strip() if zone_name else raw_name
                    if not ieee_addr or ep_id_raw is None:
                        continue
                    endpoint_id = _encrypt_endpoint(duid, ieee_addr, int(ep_id_raw), ep_type)
                    # Curtains use ModeController only (Open/Stop/Close) — no PowerController
                    if ep_type in _COVER_TYPES:
                        capabilities = [
                            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                        ]
                    else:
                        capabilities = [
                            {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                            {
                                "type": "AlexaInterface",
                                "interface": "Alexa.PowerController",
                                "version": "3",
                                "properties": {
                                    "supported": [{"name": "powerState"}],
                                    "proactivelyReported": True,
                                    "retrievable": True,
                                },
                            },
                        ]
                    if ep_type in _LIGHT_TYPES:  # brightness for all light variants
                        capabilities.append({
                            "type": "AlexaInterface",
                            "interface": "Alexa.BrightnessController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "brightness"}],
                                "proactivelyReported": True,
                                "retrievable": True,
                            },
                        })
                    if ep_type in _CCT_TYPES:  # colour temperature (Kelvin)
                        capabilities.append({
                            "type": "AlexaInterface",
                            "interface": "Alexa.ColorTemperatureController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "colorTemperatureInKelvin"}],
                                "proactivelyReported": True,
                                "retrievable": True,
                            },
                        })
                    if ep_type in _RGB_TYPES:  # colour (HSB)
                        capabilities.append({
                            "type": "AlexaInterface",
                            "interface": "Alexa.ColorController",
                            "version": "3",
                            "properties": {
                                "supported": [{"name": "color"}],
                                "proactivelyReported": True,
                                "retrievable": True,
                            },
                        })
                    if ep_type in _FAN_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface",
                            "interface": "Alexa.RangeController",
                            "version": "3",
                            "instance": "Digilux.Value",
                            "properties": {
                                "supported": [{"name": "rangeValue"}],
                                "proactivelyReported": True,
                                "retrievable": True,
                            },
                            "capabilityResources": {"friendlyNames": [
                                {"@type": "asset", "value": {"assetId": "Alexa.Setting.FanSpeed"}},
                                {"@type": "text",  "value": {"text": "speed", "locale": "en-US"}},
                            ]},
                            "configuration": {
                                "supportedRange": {"minimumValue": 1, "maximumValue": 4, "precision": 1},
                            },
                        })
                        #capabilities.append({
                         #   "type": "AlexaInterface",
                          #  "interface": "Alexa.ModeController",
                          #  "version": "3",
                          #  "instance": "Digilux.FanSpeed",
                          #  "properties": {
                          #      "supported": [{"name": "mode"}],
                           #     "proactivelyReported": True,
                           #     "retrievable": True,
                           # },
                            #"capabilityResources": {
                             #   "friendlyNames": [
                              #      {"@type": "asset", "value": {"assetId": "Alexa.Setting.FanSpeed"}},
                               #     {"@type": "text",  "value": {"text": "speed", "locale": "en-US"}},
                                #]
                            #},
                            #"configuration": {
                             #   "ordered": True,
                              #  "supportedModes": [
                               #     {"value": "FanSpeed.Low", "modeResources": {"friendlyNames": [
                                #        {"@type": "asset", "value": {"assetId": "Alexa.Value.Low"}},
                                 #       {"@type": "text",  "value": {"text": "low", "locale": "en-US"}},
                                  #  ]}},
                                   # {"value": "FanSpeed.Medium", "modeResources": {"friendlyNames": [
                                    #    {"@type": "asset", "value": {"assetId": "Alexa.Value.Medium"}},
                                     #   {"@type": "text",  "value": {"text": "medium", "locale": "en-US"}},
                                    #]}},
                                    #{"value": "FanSpeed.High", "modeResources": {"friendlyNames": [
                                     #   {"@type": "asset", "value": {"assetId": "Alexa.Value.High"}},
                                      #  {"@type": "text",  "value": {"text": "high", "locale": "en-US"}},
                                   # ]}},
                               # ],
                           # },
                       # })
                    if ep_type in _COVER_TYPES:
                        capabilities.append({
                            "type": "AlexaInterface",
                            "interface": "Alexa.ModeController",
                            "version": "3",
                            "instance": "Blind.Position",
                            "properties": {
                                "supported": [{"name": "mode"}],
                                "proactivelyReported": True,
                                "retrievable": True,
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
                        "description":       f"Digilux device in {site_name}" if site_name else "Digilux Device",
                        "manufacturerName":  ep.get("subDeviceParentManufacturer") or "Digilux",
                        "displayCategories": display_cat,
                        "capabilities":      capabilities,
                        "connections":       [],
                        "relationships":     {"isChildOf": {"endpointId": hub_id}} if hub_id else {},
                    })
    except Exception:
        logger.exception("Failed building device endpoints duid=%s", duid)
    return endpoints


def _hub_eligible_site_ids(sites: list[dict], exclude_site_ids: frozenset[str] = frozenset()) -> frozenset[str]:
    """Site ids that currently qualify for a synthetic HUB endpoint: isAlexaEnabled (default
    False — must be explicitly opted in per site), not excluded, and more than one such site
    exists (single-site rule)."""
    enabled = [s for s in sites if s.get("siteId") not in exclude_site_ids and s.get("isAlexaEnabled", False)]
    if len(enabled) <= 1:
        return frozenset()
    return frozenset(s.get("siteId", "") for s in enabled if s.get("siteId"))


def _get_all_user_endpoints(user_id: str) -> list[dict]:
    """Return all device + scene endpoints for a user (used by Discover)."""
    endpoints: list[dict] = []

    # Devices — iterate all sites → all devices in each site
    udd_table = _ddb().Table(_USER_DEVICE_TABLE)
    sites = udd_table.query(
        KeyConditionExpression=Key("userId").eq(user_id)
    ).get("Items", [])

    hub_eligible = _hub_eligible_site_ids(sites)
    # isAlexaEnabled defaults to False when absent — a site must be explicitly opted in via
    # /updatesite before it (or its devices/scenes/hub) appear in Alexa at all.
    enabled_site_ids = {s.get("siteId") for s in sites if s.get("isAlexaEnabled", False)}
    site_name_by_id: dict[str, str] = {}
    for site in sites:
        site_id   = site.get("siteId", "")
        site_name = site.get("siteName", "")
        if site_id and site_name:
            site_name_by_id[site_id] = site_name
        if site_id not in enabled_site_ids:
            continue
        zone_map  = _load_zone_map(user_id, site_id)
        hub_id    = f"site::{site_id}" if site_id in hub_eligible else ""
        if hub_id:
            endpoints.append({
                "endpointId":        hub_id,
                "friendlyName":      site_name or site_id,
                "description":       "Digilux Home Hub",
                "manufacturerName":  "Digilux",
                "displayCategories": ["HUB"],
                "capabilities": [
                    {"type": "AlexaInterface", "interface": "Alexa", "version": "3"},
                    {"type": "AlexaInterface", "interface": "Alexa.EndpointHealth",
                     "version": "3",
                     "properties": {"supported": [{"name": "connectivity"}],
                                    "proactivelyReported": False, "retrievable": False}},
                ],
                "connections": [], "relationships": {},
            })
        for device in site.get("devices", []):
            duid = device.get("duid", "")
            if duid:
                endpoints.extend(_build_device_endpoints(user_id, site_id, site_name, duid, zone_map, hub_id))

    # Scenes — scan S3 directly under user prefix to catch all sites (source of truth)
    seen_scene_ids: set = set()
    scenes_s3: list[tuple[str, dict]] = []  # (site_id, scene_dict)
    try:
        paginator = _s3_client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_METADATA_BUCKET, Prefix=f"{user_id}/", Delimiter="/"):
            for prefix_obj in page.get("CommonPrefixes", []):
                site_prefix = prefix_obj.get("Prefix", "")
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
            "connections":   [],
            "relationships": {},
        })

    logger.info("Discover: %d total endpoints for userId=%s", len(endpoints), user_id)
    return endpoints


# ── Response builders ─────────────────────────────────────────────────────────

def _response_header(namespace: str, name: str, correlation_token: str | None = None) -> dict:
    h = {
        "namespace":      namespace,
        "name":           name,
        "messageId":      str(uuid.uuid4()),
        "payloadVersion": "3",
    }
    if correlation_token:
        h["correlationToken"] = correlation_token
    return h


def _error_response(req_header: dict, error_type: str, message: str) -> dict:
    return {
        "event": {
            "header": _response_header("Alexa", "ErrorResponse",
                                       req_header.get("correlationToken")),
            "endpoint": {"endpointId": req_header.get("endpointId", "")},
            "payload": {"type": error_type, "message": message},
        }
    }


# ── Directive handlers ────────────────────────────────────────────────────────

def handle_accept_grant(directive: dict) -> dict:
    """
    Alexa.Authorization.AcceptGrant
    Exchange the LWA authorization code for access/refresh tokens and store
    them in alexa_lwa_tokens keyed by the Cognito userId extracted from the
    grantee bearer token (Cognito access token from account linking).
    """
    payload = directive.get("payload", {})
    code        = payload.get("grant",   {}).get("code",  "")
    user_token  = payload.get("grantee", {}).get("token", "")

    user_id = _decode_jwt_sub(user_token)
    logger.info("AcceptGrant for userId=%s", user_id)

    tokens = _exchange_lwa_code(code)

    table = _ddb().Table(_LWA_TOKENS_TABLE)
    table.put_item(Item={
        "userId":       user_id,
        "accessToken":  tokens["access_token"],
        "refreshToken": tokens["refresh_token"],
        "expiresAt":    0,  # force immediate refresh on first proactive call
    })
    logger.info("LWA tokens stored for userId=%s", user_id)

    _notify_alexa_enabled_on_accept_grant(user_id)

    return {
        "event": {
            "header":  _response_header("Alexa.Authorization", "AcceptGrant.Response"),
            "payload": {},
        }
    }


def handle_discover(directive: dict) -> dict:
    """
    Alexa.Discovery.Discover
    Return all device endpoints (from S3 endpoints.json) and scene endpoints
    (from scene_data DynamoDB userId-index) for the requesting user.
    """
    token   = directive.get("payload", {}).get("scope", {}).get("token", "")
    user_id = _decode_jwt_sub(token)
    logger.info("Discover for userId=%s", user_id)

    endpoints = _get_all_user_endpoints(user_id)

    return {
        "event": {
            "header":  _response_header("Alexa.Discovery", "Discover.Response"),
            "payload": {"endpoints": endpoints},
        }
    }


def handle_scene_activate(directive: dict) -> dict:
    """
    Alexa.SceneController.Activate
    Publishes full scene payload directly to iot/device/{duid}/scene.
    isProcessed=true prevents the IoT rule from re-triggering lambda4iotrelay.
    """
    header       = directive.get("header", {})
    endpoint     = directive.get("endpoint", {})
    endpoint_id  = endpoint.get("endpointId", "")
    token        = endpoint.get("scope", {}).get("token", "")
    corr_token   = header.get("correlationToken", "")

    user_id = _decode_jwt_sub(token)

    scene_id = _parse_scene_endpoint_id(endpoint_id)
    if not scene_id:
        return _error_response(header, "NO_SUCH_ENDPOINT",
                               f"endpointId is not a scene: {endpoint_id}")

    # Resolve uniqueSceneId + siteId via GSI_scene (sceneId + userId)
    # GSI_scene key is type S — sceneId must be queried as a string.
    scene_table = _ddb().Table(_SCENE_TABLE)
    logger.info("Scene activate published command triggering: sceneId=%s",scene_id)
    resp  = scene_table.query(
        IndexName="GSI_scene",
        KeyConditionExpression=Key("sceneId").eq(int(scene_id)) & Key("userId").eq(user_id),
    )
    items = resp.get("Items", [])
    if not items:
        return _error_response(header, "NO_SUCH_ENDPOINT",
                               f"Scene not found: {scene_id}")

    scene           = items[0]
    unique_scene_id = str(scene["uniqueSceneId"])
    site_id         = scene.get("siteId", "")

    # Resolve duid
    duid = scene.get("duid")
    if not duid:
        if not site_id:
            return _error_response(header, "ENDPOINT_UNREACHABLE",
                                   "Scene has no duid or siteId")
        udd_table = _ddb().Table(_USER_DEVICE_TABLE)
        site_item = udd_table.get_item(Key={"userId": user_id, "siteId": site_id}).get("Item", {})
        devices   = site_item.get("devices", [])
        duid      = next((d.get("duid") for d in devices if d.get("duid")), None)
    if not duid:
        return _error_response(header, "ENDPOINT_UNREACHABLE",
                               f"No device found for scene {scene_id}")

    # Look up groupId, zigbeeBridgeAddress and canonical uniqueSceneId from S3
    group_id       = None
    bridge_addr    = None
    s3_unique_id   = unique_scene_id  # fallback to DDB value
    try:
        s3_key = f"{user_id}/{site_id}/scenes/scene_data.json"
        obj    = _s3_client().get_object(Bucket=_METADATA_BUCKET, Key=s3_key)
        scenes = json.loads(obj["Body"].read())
        for s in scenes:
            if int(s.get("sceneId", -1)) == int(scene_id):
                s3_unique_id  = str(s.get("uniqueSceneId", unique_scene_id))
                group_id      = s.get("groupId")
                devices_list  = s.get("subdevicesMappedList", [])
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

    publish_payload = {
        "cmdType":     130,
        "sessionId":   user_id,
        "isProcessed": True,
        "msgPayload":  msg_payload,
    }

    _iot_client().publish(
        topic=f"iot/device/{duid}/scene",
        qos=1,
        payload=json.dumps(publish_payload),
    )
    logger.info("Scene activate published: sceneId=%s duid=%s payload=%s",
                scene_id, duid, json.dumps(publish_payload))

    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "context": {},
        "event": {
            "header": _response_header("Alexa.SceneController", "ActivationStarted", corr_token),
            "endpoint": {
                "scope":      {"type": "BearerToken", "token": token},
                "endpointId": endpoint_id,
            },
            "payload": {
                "cause":     {"type": "VOICE_INTERACTION"},
                "timestamp": now,
            },
        },
    }


# ── Device control helpers ────────────────────────────────────────────────────

def _decrypt_endpoint(endpoint_id: str) -> tuple[str, str, int, int]:
    """Reverse _encrypt_endpoint — returns (duid, ieee_addr, ep_id, ep_type)."""
    try:
        key = _get_endpoint_key()
        padded = endpoint_id + "=" * (4 - len(endpoint_id) % 4)
        raw = base64.urlsafe_b64decode(padded)
        nonce, ciphertext, tag = raw[:12], raw[12:-16], raw[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag).decode()
        duid, ieee_addr, ep_id, ep_type = plaintext.split("::")
        logger.info("Decrypted endpointId=%s -> duid=%s mac=%s epid=%s eptype=%s",
                    endpoint_id, duid, ieee_addr, ep_id, ep_type)
        return duid, ieee_addr, int(ep_id), int(ep_type)
    except Exception:
        logger.exception("Failed to decrypt endpointId=%s (key region=%s secret=%s)",
                          endpoint_id, _KEY_SECRET_REGION, _KEY_SECRET_ARN)
        raise


def _get_current_state(duid: str, mac: str, epid: int) -> dict:
    """Read full state dict from device_state for a specific endpoint."""
    table = _ddb().Table(_DEVICE_STATE_TABLE)
    resp = table.get_item(Key={"deviceId": duid})
    item = resp.get("Item", {})
    try:
        return item["subDevices"][mac]["endpoints"][str(epid)]["state"] or {}
    except (KeyError, TypeError):
        return {}


def _get_current_level(duid: str, mac: str, epid: int) -> int:
    """Read current brightness/range level (0-100) from device_state."""
    return int(_get_current_state(duid, mac, epid).get("level", 0))


def _get_bridge_address(user_id: str, duid: str) -> str:
    """Return the device's current bridge MAC address — decrypted from deviceMacAddress
    on its user_device_details record (same field/secret get_data_function uses), not
    inferred from S3 folder structure. A device can have stale endpoints.json files left
    behind under old bridge subfolders from prior re-pairings; deviceMacAddress is the
    single source of truth for which one is actually current."""
    if duid in _bridge_cache:
        return _bridge_cache[duid]
    try:
        udd = _ddb().Table(_USER_DEVICE_TABLE)
        sites = udd.query(KeyConditionExpression=Key("userId").eq(user_id)).get("Items", [])
        for site in sites:
            device = next((d for d in site.get("devices", []) if d.get("duid") == duid), None)
            if not device:
                continue
            encrypted_mac = device.get("deviceMacAddress", "")
            if not encrypted_mac:
                return ""
            bridge_addr = _decrypt_device_mac(encrypted_mac)
            if bridge_addr:
                _bridge_cache[duid] = bridge_addr
            return bridge_addr
    except Exception as exc:
        logger.warning("Could not resolve bridge address for duid=%s: %s", duid, exc)
    return ""


def _update_device_state_db(duid: str, mac: str, epid: int, state: dict) -> None:
    """Optimistically write Alexa-commanded state to DynamoDB so mobile reflects it immediately."""
    table = _ddb().Table(_DEVICE_STATE_TABLE)
    attr_names = {
        "#sd":  "subDevices",
        "#mac": mac,
        "#ep":  "endpoints",
        "#eid": str(epid),
        "#st":  "state",
        "#ts":  "lastUpdatedTime",
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
        logger.info("DynamoDB state updated duid=%s mac=%s epid=%s state=%s", duid, mac, epid, state)
    except Exception as exc:
        logger.warning("DynamoDB optimistic update failed: %s", exc)


def _hsv_to_hex(hue_deg, saturation_pct, value_pct) -> str:
    """Convert hue(0-360)/saturation(0-100)/value(0-100) to a '#RRGGBB' string."""
    h = (float(hue_deg) % 360) / 360.0
    s = max(0.0, min(100.0, float(saturation_pct))) / 100.0
    v = max(0.0, min(100.0, float(value_pct))) / 100.0
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def _publish_device_command(duid: str, mac: str, epid: int, eptype: int, state: dict, user_id: str = "") -> None:
    """Publish a control command to iot/device/{duid}/request."""
    logger.info("_publish_device_command enter duid=%s mac=%s epid=%s eptype=%s state=%s user_id=%s",
                duid, mac, epid, eptype, state, user_id)
    # Curtains use onOff 2/3/4 (open/stop/close) — do not coerce to bool
    if "onOff" in state and eptype not in _COVER_TYPES:
        state = {**state, "onOff": 1 if state["onOff"] else 0}

    # 1. Send command to physical device
    ts = int(time.time() * 1000)
    logger.info("Resolving bridge address for duid=%s user_id=%s", duid, user_id)
    bridge_addr = _get_bridge_address(user_id, duid) if user_id else ""
    logger.info("Resolved bridge_addr=%s for duid=%s", bridge_addr, duid)

    # cmdType is determined by what changed: 139=RGB color (hex, color-capable endpoints only),
    # 131/132=onOff (Z2M_APP_DEVICE_CONTROL_STATE_ON/OFF_CHANGE), 108=level, 109=colorTemp,
    # 107=curtain open/stop/close (tri-state, not a binary on/off — keep separate from the Z2M
    # on/off enum, which is specifically for a two-state ON/OFF change).
    # A color-set state dict also carries "level" (brightness) alongside hue/saturation, so this
    # check must come BEFORE the "level" check below, and only fires for _RGB_TYPES endpoints —
    # the `state` dict callers built (used for DynamoDB optimistic writes elsewhere) is left
    # untouched as hue/saturation/level; only the wire payload actually published is hex-converted.
    wire_state = state
    if eptype in _RGB_TYPES and "hue" in state and "saturation" in state:
        cmd_type = 139
        wire_state = {"color": _hsv_to_hex(state["hue"], state["saturation"], state.get("level", 100))}
        logger.info("cmd_type=139 (RGB color) duid=%s wire_state=%s", duid, wire_state)
    elif "colorTemp" in state:
        cmd_type = 109
        logger.info("cmd_type=109 (colorTemp) duid=%s", duid)
    elif "level" in state:
        cmd_type = 108
        logger.info("cmd_type=108 (level) duid=%s", duid)
    elif eptype in _COVER_TYPES:
        cmd_type = 107
        logger.info("cmd_type=107 (cover) duid=%s", duid)
    elif "onOff" in state:
        cmd_type = 131 if state["onOff"] else 132
        logger.info("cmd_type=%s (onOff) duid=%s", cmd_type, duid)
    else:
        cmd_type = 107
        logger.info("cmd_type=107 (fallback, no recognized state key) duid=%s state=%s", duid, state)

    msg_payload: dict = {
        "mac":    mac,
        "epid":   epid,
        "epType": eptype,
        "state":  wire_state,
    }
    if bridge_addr:
        msg_payload["zigbeeBridgeAddress"] = bridge_addr
    payload = {
        "cmdType":    cmd_type,
        "sessionId":  str(uuid.uuid4()),
        "msgPayload": msg_payload,
        "requestId":  f"REQ_{ts}_{epid}",
        "timestamp":  ts,
    }
    logger.info("Built IoT payload duid=%s requestId=%s", duid, payload["requestId"])
    logger.info("IoT publish topic=iot/device/%s/request payload=%s", duid, json.dumps(payload))
    _iot_client().publish(
        topic=f"iot/device/{duid}/request",
        qos=1,
        payload=json.dumps(payload),
    )
    logger.info("IoT publish call returned without error duid=%s requestId=%s", duid, payload["requestId"])
    logger.info("_publish_device_command exit duid=%s requestId=%s", duid, payload["requestId"])

    # 2. Synthetic /response disabled — device now physically responds to cmdType=107 commands
    #    and publishes its own /response. Re-enable below if device stops responding.
    # synthetic = {
    #     "mac":    mac,
    #     "epid":   epid,
    #     "epType": eptype,
    #     "state":  full_state,
    # }
    # try:
    #     _iot_client().publish(
    #         topic=f"iot/device/{duid}/response",
    #         qos=0,
    #         payload=json.dumps(synthetic),
    #     )
    #     logger.info("Synthetic response published for mobile refresh epid=%s state=%s", epid, full_state)
    # except Exception as exc:
    #     logger.warning("Synthetic response publish failed: %s", exc)


def _alexa_response(directive: dict, properties: list[dict]) -> dict:
    """Build a standard Alexa.Response for PowerController / BrightnessController."""
    header     = directive.get("header", {})
    endpoint   = directive.get("endpoint", {})
    token      = endpoint.get("scope", {}).get("token", "")
    corr_token = header.get("correlationToken", "")
    now        = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "context": {
            "properties": [
                {**p, "timeOfSample": now, "uncertaintyInMilliseconds": 0}
                for p in properties
            ]
        },
        "event": {
            "header": _response_header("Alexa", "Response", corr_token),
            "endpoint": {
                "scope":      {"type": "BearerToken", "token": token},
                "endpointId": endpoint.get("endpointId", ""),
            },
            "payload": {},
        },
    }


# ── Device directive handlers ─────────────────────────────────────────────────

def handle_power_controller(directive: dict) -> dict:
    """Alexa.PowerController.TurnOn / TurnOff."""
    header      = directive.get("header", {})
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    name        = header.get("name", "")
    logger.info("handle_power_controller enter name=%s endpointId=%s", name, endpoint_id)

    on = (name == "TurnOn")
    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    logger.info("handle_power_controller decrypted duid=%s mac=%s epid=%s eptype=%s", duid, mac, epid, eptype)
    try:
        user_id = _decode_jwt_sub(token)
        logger.info("handle_power_controller resolved user_id=%s", user_id)
    except Exception:
        logger.exception("handle_power_controller could not decode user_id from token")
        user_id = ""
    # Curtain: 2=open, 4=close (no boolean onOff)
    if eptype in _COVER_TYPES:
        state = {"onOff": 2 if on else 4}
    else:
        state = {"onOff": on}
    logger.info("handle_power_controller publishing state=%s duid=%s", state, duid)
    _publish_device_command(duid, mac, epid, eptype, state, user_id)
    logger.info("handle_power_controller publish returned duid=%s", duid)
    logger.info("PowerController %s duid=%s mac=%s epid=%s", name, duid, mac, epid)

    return _alexa_response(directive, [{
        "namespace": "Alexa.PowerController",
        "name":      "powerState",
        "value":     "ON" if on else "OFF",
    }])


def handle_brightness_set(directive: dict) -> dict:
    """Alexa.BrightnessController.SetBrightness — "Alexa, set bedroom to 50%"."""
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    brightness  = max(0, min(100, int(directive.get("payload", {}).get("brightness", 100))))

    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""
    _publish_device_command(duid, mac, epid, eptype, {"onOff": brightness > 0, "level": brightness}, user_id)
    logger.info("SetBrightness %d%% duid=%s mac=%s epid=%s", brightness, duid, mac, epid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.BrightnessController", "name": "brightness", "value": brightness},
        {"namespace": "Alexa.PowerController",      "name": "powerState",
         "value": "ON" if brightness > 0 else "OFF"},
    ])


def handle_brightness_adjust(directive: dict) -> dict:
    """Alexa.BrightnessController.AdjustBrightness — "Alexa, dim the lights by 20%"."""
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    delta       = int(directive.get("payload", {}).get("brightnessDelta", 0))

    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""
    current   = _get_current_level(duid, mac, epid)
    new_level = max(0, min(100, current + delta))
    _publish_device_command(duid, mac, epid, eptype, {"onOff": new_level > 0, "level": new_level}, user_id)
    logger.info("AdjustBrightness delta=%d current=%d→%d duid=%s", delta, current, new_level, duid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.BrightnessController", "name": "brightness", "value": new_level},
        {"namespace": "Alexa.PowerController",      "name": "powerState",
         "value": "ON" if new_level > 0 else "OFF"},
    ])


def handle_report_state(directive: dict) -> dict:
    """Alexa.ReportState — Alexa queries current device state to show UI controls."""
    header      = directive.get("header", {})
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    corr_token  = header.get("correlationToken", "")
    now         = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    try:
        duid, mac, epid, ep_type = _decrypt_endpoint(endpoint_id)
    except Exception:
        return _error_response(header, "NO_SUCH_ENDPOINT", f"Cannot decrypt endpointId: {endpoint_id}")

    state  = _get_current_state(duid, mac, epid)
    level  = int(state.get("level") or 0)
    on_off_raw = state.get("onOff")
    on_off = bool(on_off_raw if on_off_raw is not None else (level > 0))

    # Curtains: only report ModeController (Open/Stop/Close) — no PowerController
    if ep_type in _COVER_TYPES:
        raw_onoff = int(state.get("onOff") or 2)
        onoff_to_mode = {2: "Blind.Open", 3: "Blind.Stop", 4: "Blind.Close"}
        cur_mode = onoff_to_mode.get(raw_onoff, "Blind.Open")
        properties = [
            {"namespace": "Alexa.ModeController", "instance": "Blind.Position",
             "name": "mode", "value": cur_mode,
             "timeOfSample": now, "uncertaintyInMilliseconds": 0},
        ]
    else:
        properties = [
            {"namespace": "Alexa.PowerController", "name": "powerState",
             "value": "ON" if on_off else "OFF",
             "timeOfSample": now, "uncertaintyInMilliseconds": 0},
        ]
        if ep_type in _LIGHT_TYPES:
            properties.append({
                "namespace": "Alexa.BrightnessController", "name": "brightness",
                "value": level, "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            })
        if ep_type in _CCT_TYPES and state.get("colorTemp"):
            kelvin = int(1_000_000 / int(state["colorTemp"]))
            properties.append({
                "namespace": "Alexa.ColorTemperatureController", "name": "colorTemperatureInKelvin",
                "value": kelvin, "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            })
        if ep_type in _RGB_TYPES and state.get("hue") is not None and state.get("saturation") is not None:
            properties.append({
                "namespace": "Alexa.ColorController", "name": "color",
                "value": {
                    "hue":        float(state["hue"]),
                    "saturation": float(state["saturation"]) / 100.0,
                    "brightness": float(level) / 100.0,
                },
                "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            })
        if ep_type in _FAN_TYPES:
            properties.append({
                "namespace": "Alexa.RangeController", "instance": "Digilux.Value",
                "name": "rangeValue", "value": level,
                "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            })
            #properties.append({
             #   "namespace": "Alexa.ModeController", "instance": "Digilux.FanSpeed",
             #   "name": "mode", "value": _level_to_fan_mode(level),
             #   "timeOfSample": now, "uncertaintyInMilliseconds": 0,
            #})

    logger.info("ReportState endpointId=%s duid=%s epid=%d ep_type=%d level=%d",
                endpoint_id, duid, epid, ep_type, level)
    return {
        "context": {"properties": properties},
        "event": {
            "header": {
                "namespace": "Alexa", "name": "StateReport",
                "messageId": str(uuid.uuid4()),
                "correlationToken": corr_token,
                "payloadVersion": "3",
            },
            "endpoint": {
                "scope":      {"type": "BearerToken", "token": token},
                "endpointId": endpoint_id,
            },
            "payload": {},
        },
    }


_FAN_MODE_TO_LEVEL = {"FanSpeed.Low": 1, "FanSpeed.Medium": 2, "FanSpeed.High": 4}
_FAN_MODES = ["FanSpeed.Low", "FanSpeed.Medium", "FanSpeed.High"]

_BLIND_MODE_TO_ONOFF = {"Blind.Open": 2, "Blind.Stop": 3, "Blind.Close": 4}
_BLIND_MODES = ["Blind.Open", "Blind.Stop", "Blind.Close"]

def _level_to_fan_mode(level: int) -> str:
    if level <= 1:
        return "FanSpeed.Low"
    if level <= 2:
        return "FanSpeed.Medium"
    return "FanSpeed.High"


def handle_mode_controller(directive: dict) -> dict:
    """Alexa.ModeController.SetMode / AdjustMode — fan Low/Medium/High speed presets."""
    header      = directive.get("header", {})
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    name        = header.get("name", "")
    payload     = directive.get("payload", {})

    token   = endpoint.get("scope", {}).get("token", "")
    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""

    # Curtain: Open/Stop/Close modes → onOff 2/3/4
    if eptype in _COVER_TYPES:
        if name == "SetMode":
            mode = payload.get("mode", "Blind.Open")
        else:
            delta   = int(payload.get("modeDelta", 0))
            # find current mode from last known state (default Open)
            current_onoff = int(_get_current_state(duid, mac, epid).get("onOff", 2))
            onoff_to_mode = {2: "Blind.Open", 3: "Blind.Stop", 4: "Blind.Close"}
            cur_idx = _BLIND_MODES.index(onoff_to_mode.get(current_onoff, "Blind.Open"))
            mode = _BLIND_MODES[max(0, min(len(_BLIND_MODES) - 1, cur_idx + delta))]
        cmd_state = {"onOff": _BLIND_MODE_TO_ONOFF.get(mode, 2)}
        _publish_device_command(duid, mac, epid, eptype, cmd_state, user_id)
        logger.info("ModeController curtain %s mode=%s duid=%s", name, mode, duid)
        return _alexa_response(directive, [
            {"namespace": "Alexa.ModeController", "instance": "Blind.Position",
             "name": "mode", "value": mode},
        ])

    # Fan: Low/Medium/High
    if name == "SetMode":
        mode  = payload.get("mode", "FanSpeed.Medium")
        level = _FAN_MODE_TO_LEVEL.get(mode, 2)
    else:
        delta   = int(payload.get("modeDelta", 0))
        current = _get_current_level(duid, mac, epid)
        idx     = _FAN_MODES.index(_level_to_fan_mode(current))
        mode    = _FAN_MODES[max(0, min(len(_FAN_MODES) - 1, idx + delta))]
        level   = _FAN_MODE_TO_LEVEL[mode]

    _publish_device_command(duid, mac, epid, eptype, {"onOff": level > 0, "level": level}, user_id)
    logger.info("ModeController %s mode=%s level=%d duid=%s", name, mode, level, duid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.ModeController", "instance": "Digilux.FanSpeed",
         "name": "mode", "value": mode},
        {"namespace": "Alexa.RangeController", "instance": "Digilux.Value",
         "name": "rangeValue", "value": level},
        {"namespace": "Alexa.PowerController", "name": "powerState", "value": "ON" if level > 0 else "OFF"},
    ])


def handle_range_controller(directive: dict) -> dict:
    """Alexa.RangeController.SetRangeValue / AdjustRangeValue — fan speed / curtain level.

    Voice commands:
      "Alexa, set [fan] speed to 50"       → SetRangeValue   (rangeValue = 0-100)
      "Alexa, increase [fan] speed by 20"  → AdjustRangeValue (rangeValueDelta > 0)
      "Alexa, decrease [fan] speed by 10"  → AdjustRangeValue (rangeValueDelta < 0)
      "Alexa, increase [fan] speed"        → AdjustRangeValue (rangeValueDelta = 10)
    """
    header      = directive.get("header", {})
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    name        = header.get("name", "")
    payload     = directive.get("payload", {})

    token   = endpoint.get("scope", {}).get("token", "")
    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""

    if name == "SetRangeValue":
        if eptype in _FAN_TYPES:
            # Alexa range and device level both use 1-4.
            new_level = max(
                1,
                min(4, int(round(float(payload.get("rangeValue", 1)))))
            )
        else:
            alexa_val = max(0, min(100, int(payload.get("rangeValue", 0))))
            new_level = alexa_val

    else:  # AdjustRangeValue
        delta = int(payload.get("rangeValueDelta", 0))
        current = _get_current_level(duid, mac, epid)

        if eptype in _FAN_TYPES:
            # Change speed by the requested delta; clamp to 1-4.
            current = max(1, min(4, int(current)))
            new_level = max(1, min(4, current + delta))
        else:
            new_level = max(0, min(100, current + delta))

    # Curtain: no level, use onOff 2=open / 4=close
    if eptype in _COVER_TYPES:
        cmd_state = {"onOff": 2 if new_level > 0 else 4}
    else:
        cmd_state = {"onOff": new_level > 0, "level": new_level}

    _publish_device_command(duid, mac, epid, eptype, cmd_state, user_id)
    logger.info("RangeController %s level=%d duid=%s mac=%s epid=%s", name, new_level, duid, mac, epid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.RangeController", "instance": "Digilux.Value",
         "name": "rangeValue", "value": new_level},
        {"namespace": "Alexa.PowerController", "name": "powerState",
         "value": "ON" if new_level > 0 else "OFF"},
    ])


def handle_color_temperature(directive: dict) -> dict:
    """Alexa.ColorTemperatureController.SetColorTemperature — CCT lights.

    Alexa sends Kelvin; firmware expects Mireds (colorTemp = 1,000,000 / Kelvin).
    Typical range: 154 Mireds (6500K cool) → 370 Mireds (2703K warm).
    """
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    kelvin      = int(directive.get("payload", {}).get("colorTemperatureInKelvin", 4000))
    kelvin      = max(1000, min(10000, kelvin))
    mireds      = max(154, min(500, int(1_000_000 / kelvin)))

    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""
    _publish_device_command(duid, mac, epid, eptype, {"colorTemp": mireds, "onOff": True}, user_id)
    logger.info("SetColorTemperature %dK (%d Mireds) duid=%s epid=%s", kelvin, mireds, duid, epid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.ColorTemperatureController",
         "name": "colorTemperatureInKelvin", "value": kelvin},
        {"namespace": "Alexa.PowerController", "name": "powerState", "value": "ON"},
    ])


def handle_color(directive: dict) -> dict:
    """Alexa.ColorController.SetColor — RGB lights.

    Alexa sends HSB: hue 0-360 (float), saturation 0-1 (float), brightness 0-1 (float).
    IoT expects:    hue 0-360 (int), saturation 0-100 (int), level 0-100 (int).
    """
    endpoint    = directive.get("endpoint", {})
    endpoint_id = endpoint.get("endpointId", "")
    token       = endpoint.get("scope", {}).get("token", "")
    color       = directive.get("payload", {}).get("color", {})

    alexa_hue = float(color.get("hue",        0.0))
    alexa_sat = float(color.get("saturation", 1.0))
    alexa_bri = float(color.get("brightness", 1.0))

    iot_hue   = max(0, min(360, int(round(alexa_hue))))
    iot_sat   = max(0, min(100, int(round(alexa_sat * 100))))
    iot_level = max(0, min(100, int(round(alexa_bri * 100))))

    duid, mac, epid, eptype = _decrypt_endpoint(endpoint_id)
    try:
        user_id = _decode_jwt_sub(token)
    except Exception:
        user_id = ""
    _publish_device_command(duid, mac, epid, eptype, {
        "hue": iot_hue, "saturation": iot_sat, "level": iot_level, "onOff": True,
    }, user_id)
    logger.info("SetColor H=%d S=%d B=%d duid=%s epid=%s", iot_hue, iot_sat, iot_level, duid, epid)

    return _alexa_response(directive, [
        {"namespace": "Alexa.ColorController", "name": "color",
         "value": {"hue": alexa_hue, "saturation": alexa_sat, "brightness": alexa_bri}},
        {"namespace": "Alexa.PowerController", "name": "powerState", "value": "ON"},
    ])


# ── Lambda entry point ────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # One-off admin invocation: explicitly remove specific stale endpoints from a
    # customer's Alexa account (e.g. after a bridge re-pairing left old sub-devices
    # behind). Real Alexa directives always carry "directive", never "action", so
    # this can't collide with normal directive dispatch below.
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

    directive = event.get("directive", {})
    header    = directive.get("header", {})
    namespace = header.get("namespace", "")
    name      = header.get("name", "")
    logger.info("Alexa directive: %s/%s", namespace, name)

    try:
        if namespace == "Alexa.Authorization" and name == "AcceptGrant":
            logger.info("Dispatching to handle_accept_grant")
            result = handle_accept_grant(directive)
            logger.info("handle_accept_grant returned OK")
            return result

        if namespace == "Alexa.Discovery" and name == "Discover":
            logger.info("Dispatching to handle_discover")
            result = handle_discover(directive)
            logger.info("handle_discover returned OK")
            return result

        if namespace == "Alexa.SceneController" and name == "Activate":
            logger.info("Dispatching to handle_scene_activate")
            result = handle_scene_activate(directive)
            logger.info("handle_scene_activate returned OK")
            return result

        if namespace == "Alexa.PowerController" and name in ("TurnOn", "TurnOff"):
            logger.info("Dispatching to handle_power_controller")
            result = handle_power_controller(directive)
            logger.info("handle_power_controller returned OK")
            return result

        if namespace == "Alexa.BrightnessController" and name == "SetBrightness":
            logger.info("Dispatching to handle_brightness_set")
            result = handle_brightness_set(directive)
            logger.info("handle_brightness_set returned OK")
            return result

        if namespace == "Alexa.BrightnessController" and name == "AdjustBrightness":
            logger.info("Dispatching to handle_brightness_adjust")
            result = handle_brightness_adjust(directive)
            logger.info("handle_brightness_adjust returned OK")
            return result

        if namespace == "Alexa.RangeController" and name in ("SetRangeValue", "AdjustRangeValue"):
            logger.info("Dispatching to handle_range_controller")
            result = handle_range_controller(directive)
            logger.info("handle_range_controller returned OK")
            return result

        if namespace == "Alexa.ModeController" and name in ("SetMode", "AdjustMode"):
            logger.info("Dispatching to handle_mode_controller")
            result = handle_mode_controller(directive)
            logger.info("handle_mode_controller returned OK")
            return result

        if namespace == "Alexa.ColorTemperatureController" and name == "SetColorTemperature":
            logger.info("Dispatching to handle_color_temperature")
            result = handle_color_temperature(directive)
            logger.info("handle_color_temperature returned OK")
            return result

        if namespace == "Alexa.ColorController" and name == "SetColor":
            logger.info("Dispatching to handle_color")
            result = handle_color(directive)
            logger.info("handle_color returned OK")
            return result

        if namespace == "Alexa" and name == "ReportState":
            logger.info("Dispatching to handle_report_state")
            result = handle_report_state(directive)
            logger.info("handle_report_state returned OK")
            return result

        logger.warning("Unhandled directive: %s/%s", namespace, name)
        return _error_response(header, "INVALID_DIRECTIVE",
                               f"Unsupported namespace/name: {namespace}/{name}")

    except Exception:
        logger.exception("Unhandled error in directive %s/%s", namespace, name)
        return _error_response(header, "INTERNAL_ERROR",
                               "An internal error occurred. Check Lambda logs.")
