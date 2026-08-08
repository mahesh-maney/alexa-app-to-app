"""
google_home_start — POST /api/v1/voice/google-home/account-linking/deep-link/start

Initiates a Google Home deep-link account linking session.

Flow:
  1. Authenticate the caller via Cognito JWT (API GW validates; we extract sub)
  2. Generate a cryptographically random state (CSRF token)
  3. Persist the session in DynamoDB with a 10-minute TTL
  4. Return:
       - state          : session CSRF token
       - agentId        : Google Home agent ID (from env var)
       - homeAppDeepLink: URL to open Google Home app at account linking screen
       - webFallbackUrl : OAuth web URL when Google Home app is not installed

The Flutter app opens the homeAppDeepLink (or webFallbackUrl) and then polls
GET /status until linked, then calls /complete to confirm.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
import uuid

import boto3

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)


# ── Environment variables ──────────────────────────────────────────────────────
_REGION          = os.environ.get("DATA_REGION", "")
_SESSIONS_TABLE  = os.environ.get("GH_SESSIONS_TABLE", "google_home_link_sessions")
_SESSION_TTL_S   = int(os.environ.get("SESSION_TTL_SECONDS", "600"))
_AGENT_ID        = os.environ.get("GOOGLE_AGENT_ID", "")
_OAUTH_BASE_URL  = os.environ.get("OAUTH_BASE_URL", "")   # e.g. https://iot.digilux.co.in/smarthome
_GH_CLIENT_ID    = os.environ.get("GOOGLE_CLIENT_ID", "")
_GH_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")  # Google's redirect URI
_GH_SCOPE        = os.environ.get("GOOGLE_SCOPE", "profile")

_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "GOOGLE_AGENT_ID":  _AGENT_ID,
    "OAUTH_BASE_URL":   _OAUTH_BASE_URL,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — invocations will return 500",
                 _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


def _decode_jwt_sub(token: str) -> str:
    """Extract the Cognito sub claim from a JWT. API GW already validated signature."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    padding = "=" * (4 - len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception as exc:
        raise ValueError(f"Could not decode JWT payload: {exc}") from exc
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        raise ValueError("Missing sub/username claim in JWT")
    logger.debug("JWT_DECODED sub=%s", sub)
    return sub


def _build_home_app_deep_link(agent_id: str) -> str:
    """
    Build the Google Home app deep link for account linking.
    Format: https://madeby.google.com/home-app/?deeplink=setup%2Fha_linking%3Fagent_id%3D{agent_id}
    """
    inner = urllib.parse.urlencode({"agent_id": agent_id})
    deeplink_value = urllib.parse.quote(f"setup/ha_linking?{inner}", safe="")
    return f"https://madeby.google.com/home-app/?deeplink={deeplink_value}"


def _build_web_fallback_url(state: str) -> str:
    """
    Build the web OAuth fallback URL for when Google Home app is not installed.
    Points to our OAuth authorize endpoint with pre_auth_state so user doesn't
    need to log in again (they're already authenticated in the Flutter app).
    """
    if not _OAUTH_BASE_URL:
        return ""
    params: dict[str, str] = {
        "pre_auth_state": state,
        "response_type":  "code",
    }
    if _GH_CLIENT_ID:
        params["client_id"] = _GH_CLIENT_ID
    if _GH_REDIRECT_URI:
        params["redirect_uri"] = _GH_REDIRECT_URI
    if _GH_SCOPE:
        params["scope"] = _GH_SCOPE
    base = _OAUTH_BASE_URL.rstrip("/")
    return f"{base}/google-home/oauth/authorize?{urllib.parse.urlencode(params)}"


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=google_home_start request_id=%s", request_id)

    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Extract userId from JWT ─────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth:
        logger.warning("AUTH_MISSING request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    token = auth.split(" ")[-1].strip()
    try:
        user_id = _decode_jwt_sub(token)
        logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)
    except Exception as exc:
        logger.warning("AUTH_FAILED reason=%s request_id=%s", exc, request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Generate session state ──────────────────────────────────────────────
    state = str(uuid.uuid4())
    now = int(time.time())
    expires_at = now + _SESSION_TTL_S

    logger.debug("SESSION_PARAMS userId=%s state=%s ttl=%d request_id=%s",
                 user_id, state, _SESSION_TTL_S, request_id)

    # ── 3. Persist session in DDB ──────────────────────────────────────────────
    try:
        _ddb().Table(_SESSIONS_TABLE).put_item(Item={
            "state":     state,
            "userId":    user_id,
            "status":    "PENDING",
            "createdAt": now,
            "ttl":       expires_at,
        })
        logger.debug("DDB_PUT_OK table=%s state=%s request_id=%s",
                     _SESSIONS_TABLE, state, request_id)
    except Exception as exc:
        logger.error("DDB_PUT_ERROR error=%s request_id=%s", exc, request_id)
        return _resp(500, {"error": "Failed to create linking session"})

    _audit("GH_SESSION_CREATED", userId=user_id, state=state,
           expires_at=expires_at, request_id=request_id)

    # ── 4. Build response ──────────────────────────────────────────────────────
    home_app_deep_link = _build_home_app_deep_link(_AGENT_ID)
    web_fallback_url = _build_web_fallback_url(state)

    logger.info("REQUEST_OK function=google_home_start userId=%s state=%s request_id=%s",
                user_id, state, request_id)

    body: dict = {
        "state":           state,
        "agentId":         _AGENT_ID,
        "homeAppDeepLink": home_app_deep_link,
    }
    if web_fallback_url:
        body["webFallbackUrl"] = web_fallback_url

    return _resp(200, body)


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
