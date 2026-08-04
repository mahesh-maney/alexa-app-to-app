"""
alexa_link_status — GET /api/v1/alexa/status

Returns whether the authenticated user has a linked Alexa account.

Flow:
  1. Authenticate the caller via Cognito JWT (Authorization: Bearer <token>)
  2. Look up the user's record in DynamoDB (digilux_honeywell_alexa_lwa_tokens)
  3. Return { "linked": true, "linkedAt": <epoch_ms> } or { "linked": false }

Error responses:
  401 — unauthenticated
  500 — server configuration error
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION",                "")
_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",           "")
_USER_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",  "")
_COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION       = os.environ.get("COGNITO_REGION",       _REGION or "")
_COGNITO_ISSUER = (
    f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    if _COGNITO_USER_POOL_ID else ""
)

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "LWA_TOKENS_TABLE": _TOKENS_TABLE,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)

if not _COGNITO_USER_POOL_ID:
    logger.warning("SECURITY_WARNING COGNITO_USER_POOL_ID not set — "
                   "JWT signature verification DISABLED (set for production)")

# ── Module-level caches ────────────────────────────────────────────────────────
_ddb_resource = None
_jwks_cache: dict = {}


def _ddb():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=_REGION)
    return _ddb_resource


# ── Helpers ────────────────────────────────────────────────────────────────────

def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


def _decode_jwt_sub(token: str) -> str | None:
    """Extract sub (or username) from JWT payload without signature verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub") or payload.get("username") or payload.get("cognito:username")
    except Exception:
        return None


def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        return _jwks_cache
    url = f"{_COGNITO_ISSUER}/.well-known/jwks.json"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            _jwks_cache = json.loads(r.read())
    except Exception as exc:
        logger.warning("JWKS_FETCH_ERROR url=%s error=%s", url, exc)
    return _jwks_cache


def _verify_jwt_signature(token: str) -> None:
    """RS256 signature verification against Cognito JWKS. No-op if pool ID not set."""
    if not _COGNITO_USER_POOL_ID:
        return
    try:
        import jose.jwt as jwt
        jwks = _get_jwks()
        jwt.decode(token, jwks, algorithms=["RS256"],
                   audience=None, options={"verify_aud": False})
    except ImportError:
        logger.error("JWT_VERIFY_UNAVAILABLE python-jose not installed — "
                     "add python-jose[cryptography] to requirements.txt")
    except Exception as exc:
        raise ValueError(f"JWT signature invalid: {exc}") from exc


# ── Handler ────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaLinkStatus request_id=%s", request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ───────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        logger.warning("AUTH_FAILED reason=missing_header request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    token = auth_header.split(" ", 1)[1].strip()
    if token.count(".") != 2:
        logger.warning("AUTH_FAILED reason=malformed_jwt request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    try:
        _verify_jwt_signature(token)
    except ValueError:
        logger.warning("AUTH_FAILED reason=invalid_signature request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    user_id = _decode_jwt_sub(token)
    if not user_id:
        logger.warning("AUTH_FAILED reason=no_sub request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)

    # ── 2. Extract optional siteId query param ────────────────────────────────
    site_id = ((event.get("queryStringParameters") or {}).get("siteId") or "").strip()

    # ── 3. Look up status ─────────────────────────────────────────────────────
    if site_id:
        # Per-site status from user-device mapping table
        if not _USER_DEVICE_MAPPING_TABLE:
            logger.error("CONFIG_ERROR USER_DEVICE_MAPPING_TABLE not set userId=%s "
                         "siteId=%s request_id=%s", user_id, site_id, request_id)
            return _resp(500, {"error": "Server configuration error"})
        try:
            result = _ddb().Table(_USER_DEVICE_MAPPING_TABLE).get_item(
                Key={"userId": user_id, "siteId": site_id}
            )
        except Exception as exc:
            logger.error("DDB_ERROR error=%s userId=%s siteId=%s request_id=%s",
                         exc, user_id, site_id, request_id)
            return _resp(500, {"error": "Server configuration error"})

        item = result.get("Item")
        linked = bool(item and item.get("alexaLinked"))
        linked_at = int(item["alexaLinkedAt"]) if item and item.get("alexaLinkedAt") else None

        logger.info("STATUS_CHECK userId=%s siteId=%s linked=%s linkedAt=%s request_id=%s",
                    user_id, site_id, linked, linked_at, request_id)

        response: dict = {"linked": linked, "siteId": site_id}
        if linked_at is not None:
            response["linkedAt"] = linked_at
        return _resp(200, response)

    else:
        # Global (non-site) status from LWA tokens table
        try:
            result = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        except Exception as exc:
            logger.error("DDB_ERROR error=%s userId=%s request_id=%s", exc, user_id, request_id)
            return _resp(500, {"error": "Server configuration error"})

        item = result.get("Item")

        if not item:
            logger.info("STATUS_CHECK userId=%s linked=false request_id=%s", user_id, request_id)
            return _resp(200, {"linked": False})

        linked_at = item.get("linkedAt")
        response = {"linked": True}
        if linked_at is not None:
            response["linkedAt"] = int(linked_at)

        logger.info("STATUS_CHECK userId=%s linked=true linkedAt=%s request_id=%s",
                    user_id, linked_at, request_id)
        return _resp(200, response)
