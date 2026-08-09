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
from botocore.exceptions import ClientError

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    """Emit a structured audit log line. These are ALWAYS logged regardless of LOG_LEVEL."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)


# ── Environment variables ──────────────────────────────────────────────────────
_REGION         = os.environ.get("DATA_REGION", "")
_SESSIONS_TABLE = os.environ.get("GH_SESSIONS_TABLE", "google_home_link_sessions")
_SESSION_TTL_S  = int(os.environ.get("SESSION_TTL_SECONDS", "600"))
_AGENT_ID       = os.environ.get("GOOGLE_AGENT_ID", "")
_OAUTH_BASE_URL = os.environ.get("OAUTH_BASE_URL", "")
_GH_CLIENT_ID   = os.environ.get("GOOGLE_CLIENT_ID", "")
_GH_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
_GH_SCOPE       = os.environ.get("GOOGLE_SCOPE", "profile")

_REQUIRED_VARS = {
    "DATA_REGION":     _REGION,
    "GOOGLE_AGENT_ID": _AGENT_ID,
    "OAUTH_BASE_URL":  _OAUTH_BASE_URL,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_start "
        "— all invocations will return HTTP 500 until these are set",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_start region=%s sessions_table=%s "
        "agent_id=%s ttl_seconds=%d oauth_base_url=%s "
        "web_fallback_configured=%s",
        _REGION, _SESSIONS_TABLE, _AGENT_ID, _SESSION_TTL_S, _OAUTH_BASE_URL,
        bool(_GH_CLIENT_ID and _GH_REDIRECT_URI),
    )

if not _GH_CLIENT_ID:
    logger.warning(
        "CONFIG_WARNING GOOGLE_CLIENT_ID not set — webFallbackUrl will omit client_id"
    )
if not _GH_REDIRECT_URI:
    logger.warning(
        "CONFIG_WARNING GOOGLE_REDIRECT_URI not set — webFallbackUrl will omit redirect_uri"
    )

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_CLIENT_INIT region=%s table=%s (cold start)", _REGION, _SESSIONS_TABLE)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        logger.info("DDB_CLIENT_READY region=%s", _REGION)
    else:
        logger.debug("DDB_CLIENT_REUSE region=%s (warm container)", _REGION)
    return _dynamodb


def _decode_jwt_sub(token: str) -> str:
    """Extract the Cognito sub claim from a JWT. API GW already validated signature."""
    logger.debug("JWT_DECODE_START token_len=%d token_prefix=%s", len(token), token[:12])
    parts = token.split(".")
    if len(parts) != 3:
        logger.debug("JWT_INVALID_FORMAT part_count=%d expected=3", len(parts))
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    try:
        padding = "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception as exc:
        logger.debug("JWT_PAYLOAD_DECODE_FAILED reason=%s", exc)
        raise ValueError(f"Could not decode JWT payload: {exc}") from exc
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        logger.debug("JWT_MISSING_SUB available_claims=%s", list(claims.keys()))
        raise ValueError("Missing sub/username claim in JWT")
    claim_source = "sub" if claims.get("sub") else "username"
    token_use    = claims.get("token_use", "unknown")
    iss          = claims.get("iss", "")
    exp          = claims.get("exp", 0)
    logger.debug(
        "JWT_DECODE_OK claim_source=%s token_use=%s iss=%s exp=%d sub_len=%d",
        claim_source, token_use, iss, exp, len(sub),
    )
    return sub


def _build_home_app_deep_link(agent_id: str) -> str:
    """Build the Google Home app deep link for account linking."""
    inner = urllib.parse.urlencode({"agent_id": agent_id})
    deeplink_value = urllib.parse.quote(f"setup/ha_linking?{inner}", safe="")
    url = f"https://madeby.google.com/home-app/?deeplink={deeplink_value}"
    logger.debug("GHA_DEEP_LINK_BUILT agent_id=%s url=%s", agent_id, url)
    return url


def _build_web_fallback_url(state: str) -> str:
    """Build the web OAuth fallback URL for when the Google Home app is not installed."""
    if not _OAUTH_BASE_URL:
        logger.debug("WEB_FALLBACK_SKIPPED reason=OAUTH_BASE_URL_not_set")
        return ""
    params: dict[str, str] = {
        "pre_auth_state": state,
        "response_type":  "code",
    }
    if _GH_CLIENT_ID:
        params["client_id"] = _GH_CLIENT_ID
    else:
        logger.debug("WEB_FALLBACK_NO_CLIENT_ID reason=GOOGLE_CLIENT_ID_not_set")
    if _GH_REDIRECT_URI:
        params["redirect_uri"] = _GH_REDIRECT_URI
    else:
        logger.debug("WEB_FALLBACK_NO_REDIRECT_URI reason=GOOGLE_REDIRECT_URI_not_set")
    if _GH_SCOPE:
        params["scope"] = _GH_SCOPE
    base = _OAUTH_BASE_URL.rstrip("/")
    url = f"{base}/google-home/oauth/authorize?{urllib.parse.urlencode(params)}"
    logger.debug(
        "WEB_FALLBACK_BUILT base=%s param_count=%d url_len=%d",
        base, len(params), len(url),
    )
    return url


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method = (event.get("httpMethod") or "POST").upper()
    path   = event.get("path", "")
    source_ip = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_start method=%s path=%s "
        "source_ip=%s request_id=%s",
        method, path, source_ip, request_id,
    )

    # ── 0. Config guard ────────────────────────────────────────────────────────
    if _missing_vars:
        logger.error(
            "CONFIG_ERROR missing=%s function=google_home_start request_id=%s "
            "— cannot process request",
            _missing_vars, request_id,
        )
        _audit("GH_START_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Extract and validate JWT ────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth = (
        headers.get("Authorization")
        or headers.get("authorization")
        or ""
    )
    logger.debug(
        "AUTH_HEADER_CHECK present=%s request_id=%s",
        bool(auth), request_id,
    )

    if not auth:
        logger.warning(
            "AUTH_MISSING no Authorization header function=google_home_start "
            "source_ip=%s request_id=%s",
            source_ip, request_id,
        )
        _audit("GH_START_AUTH_MISSING", source_ip=source_ip, request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    scheme = auth.split(" ")[0] if " " in auth else auth
    token  = auth.split(" ")[-1].strip()
    logger.debug(
        "AUTH_TOKEN_PRESENT scheme=%s token_len=%d token_prefix=%s request_id=%s",
        scheme, len(token), token[:12], request_id,
    )

    try:
        user_id = _decode_jwt_sub(token)
        logger.info(
            "AUTH_OK function=google_home_start userId=%s request_id=%s",
            user_id, request_id,
        )
        _audit("GH_START_AUTH_OK", userId=user_id, request_id=request_id)
    except Exception as exc:
        logger.warning(
            "AUTH_FAILED reason=%s scheme=%s token_prefix=%s "
            "source_ip=%s request_id=%s",
            exc, scheme, token[:12], source_ip, request_id,
        )
        _audit("GH_START_AUTH_FAILED", reason=str(exc), source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Generate session state (CSRF token) ─────────────────────────────────
    state = str(uuid.uuid4())
    now   = int(time.time())
    expires_at = now + _SESSION_TTL_S

    logger.debug(
        "SESSION_GENERATED userId=%s state=%s created_at=%d expires_at=%d "
        "ttl_seconds=%d request_id=%s",
        user_id, state, now, expires_at, _SESSION_TTL_S, request_id,
    )

    # ── 3. Persist session in DynamoDB ─────────────────────────────────────────
    session_item = {
        "state":     state,
        "userId":    user_id,
        "status":    "PENDING",
        "createdAt": now,
        "ttl":       expires_at,
    }
    logger.debug(
        "DDB_PUT_START table=%s userId=%s state=%s request_id=%s",
        _SESSIONS_TABLE, user_id, state, request_id,
    )
    try:
        _ddb().Table(_SESSIONS_TABLE).put_item(Item=session_item)
        logger.info(
            "DDB_PUT_OK table=%s userId=%s state=%s expires_at=%d request_id=%s",
            _SESSIONS_TABLE, user_id, state, expires_at, request_id,
        )
        _audit("GH_SESSION_CREATED", userId=user_id, state=state,
               status="PENDING", created_at=now, expires_at=expires_at,
               ttl_seconds=_SESSION_TTL_S, request_id=request_id)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_PUT_ERROR table=%s error_code=%s error=%s "
            "userId=%s state=%s request_id=%s",
            _SESSIONS_TABLE, error_code, exc, user_id, state, request_id,
        )
        _audit("GH_SESSION_CREATE_FAILED", userId=user_id, state=state,
               error_code=error_code, request_id=request_id)
        return _resp(500, {"error": "Failed to create linking session"})
    except Exception as exc:
        logger.error(
            "DDB_PUT_UNEXPECTED_ERROR table=%s error=%s userId=%s state=%s request_id=%s",
            _SESSIONS_TABLE, exc, user_id, state, request_id,
        )
        _audit("GH_SESSION_CREATE_FAILED", userId=user_id, state=state,
               error="unexpected", request_id=request_id)
        return _resp(500, {"error": "Failed to create linking session"})

    # ── 4. Build response URLs ─────────────────────────────────────────────────
    logger.debug(
        "BUILDING_URLS agent_id=%s oauth_base=%s request_id=%s",
        _AGENT_ID, _OAUTH_BASE_URL, request_id,
    )
    home_app_deep_link = _build_home_app_deep_link(_AGENT_ID)
    web_fallback_url   = _build_web_fallback_url(state)

    logger.info(
        "URL_BUILD_OK home_app_deep_link_len=%d web_fallback_present=%s request_id=%s",
        len(home_app_deep_link), bool(web_fallback_url), request_id,
    )

    body: dict = {
        "state":           state,
        "agentId":         _AGENT_ID,
        "homeAppDeepLink": home_app_deep_link,
    }
    if web_fallback_url:
        body["webFallbackUrl"] = web_fallback_url

    logger.info(
        "REQUEST_OK function=google_home_start userId=%s state=%s "
        "home_app_deep_link_present=true web_fallback_present=%s request_id=%s",
        user_id, state, bool(web_fallback_url), request_id,
    )
    _audit("GH_START_OK", userId=user_id, state=state,
           agent_id=_AGENT_ID, web_fallback_present=bool(web_fallback_url),
           request_id=request_id)

    return _resp(200, body)


def _resp(status_code: int, body: dict) -> dict:
    logger.debug("RESPONSE status=%d body_keys=%s", status_code, list(body.keys()))
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
