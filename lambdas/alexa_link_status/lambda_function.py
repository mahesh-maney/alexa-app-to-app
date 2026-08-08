"""
alexa_link_status — GET /api/v1/alexa/status

Returns whether the authenticated user has a linked Alexa account.

Without ?siteId (global check):
  Reads from digilux_honeywell_alexa_lwa_tokens.
  Response: { "linked": bool, "linkedAt": <epoch> }

With ?siteId=<id> (per-site check):
  Reads alexaLinked/alexaLinkedAt from digilux_honeywell_user_device_mapping,
  and isAlexaEnabled from user_device_details (set by create_and_update_data_function).
  Response: { "linked": bool, "siteId": "...", "linkedAt": <epoch>, "isAlexaEnabled": bool }

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
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION",                "")
_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",           "")
_USER_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",  "")
_USER_DEVICE_DETAILS_TABLE = os.environ.get("USER_DEVICE_DETAILS_TABLE",  "user_device_details")
_LWA_SECRET_ARN            = os.environ.get("LWA_SECRET_ARN",             "")
_LWA_SECRET_REGION         = os.environ.get("LWA_SECRET_REGION",          "eu-west-1")
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
_lwa_secret_cache: dict = {}  # { client_id, client_secret }


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


# ── LWA token validation ───────────────────────────────────────────────────────

def _get_lwa_secret() -> dict:
    """Fetch LWA client_id/client_secret from Secrets Manager (cached per container)."""
    global _lwa_secret_cache
    if _lwa_secret_cache:
        return _lwa_secret_cache
    if not _LWA_SECRET_ARN:
        return {}
    try:
        sm = boto3.client("secretsmanager", region_name=_LWA_SECRET_REGION)
        secret = json.loads(
            sm.get_secret_value(SecretId=_LWA_SECRET_ARN)["SecretString"]
        )
        _lwa_secret_cache = secret
        return secret
    except Exception as exc:
        logger.warning("LWA_SECRET_FETCH_ERROR error=%s", exc)
        return {}


def _validate_and_heal_lwa(user_id: str, site_id: str | None, request_id: str) -> bool:
    """
    Validate the user's stored LWA refresh token against Amazon.

    Returns True if the token is valid (skill still enabled).
    Returns False if Amazon returns invalid_grant (skill was disabled).
    Returns True on any other error (fail-open — avoid false 'unlinked' reports).

    When invalid_grant is detected and site_id is provided, clears alexaLinked
    in digilux_honeywell_user_device_mapping immediately (self-healing).
    """
    secret = _get_lwa_secret()
    if not secret:
        logger.warning("LWA_VALIDATE_SKIPPED no LWA secret userId=%s", user_id)
        return True  # fail-open

    try:
        token_item = _ddb().Table(_TOKENS_TABLE).get_item(
            Key={"userId": user_id}
        ).get("Item")
    except Exception as exc:
        logger.warning("LWA_VALIDATE_DDB_ERROR error=%s userId=%s", exc, user_id)
        return True  # fail-open

    if not token_item:
        # No LWA token record — skill was never linked or tokens already deleted
        logger.info("LWA_VALIDATE_NO_RECORD userId=%s request_id=%s", user_id, request_id)
        return False

    refresh_token = token_item.get("refreshToken")
    if not refresh_token:
        logger.warning("LWA_VALIDATE_NO_REFRESH_TOKEN userId=%s request_id=%s", user_id, request_id)
        return True  # can't validate without refresh token, fail-open

    try:
        data = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     secret["client_id"],
            "client_secret": secret["client_secret"],
        }).encode()
        req = urllib.request.Request(
            "https://api.amazon.com/auth/o2/token",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            logger.info("LWA_VALIDATE_OK userId=%s request_id=%s", user_id, request_id)
            return True

    except urllib.error.HTTPError as exc:
        body = {}
        try:
            body = json.loads(exc.read())
        except Exception:
            pass
        if exc.code == 400 and body.get("error") == "invalid_grant":
            logger.info(
                "LWA_VALIDATE_REVOKED userId=%s — skill disabled, healing alexaLinked "
                "request_id=%s", user_id, request_id,
            )
            # Self-heal: clear alexaLinked flag for this site (or all sites)
            _clear_alexa_linked(user_id, site_id, request_id)
            return False
        logger.warning("LWA_VALIDATE_HTTP_ERROR code=%s body=%s userId=%s request_id=%s",
                       exc.code, body, user_id, request_id)
        return True  # fail-open

    except Exception as exc:
        logger.warning("LWA_VALIDATE_ERROR error=%s userId=%s request_id=%s",
                       exc, user_id, request_id)
        return True  # fail-open


def _clear_alexa_linked(user_id: str, site_id: str | None, request_id: str) -> None:
    """Clear alexaLinked on the given site (or all sites if site_id is None)."""
    if not _USER_DEVICE_MAPPING_TABLE:
        return
    mapping_table = _ddb().Table(_USER_DEVICE_MAPPING_TABLE)
    try:
        if site_id:
            mapping_table.update_item(
                Key={"userId": user_id, "siteId": site_id},
                UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                ExpressionAttributeValues={":f": False},
            )
            logger.info("ALEXA_LINKED_CLEARED userId=%s siteId=%s request_id=%s",
                        user_id, site_id, request_id)
        else:
            from boto3.dynamodb.conditions import Key as DdbKey
            result = mapping_table.query(
                KeyConditionExpression=DdbKey("userId").eq(user_id)
            )
            for site in result.get("Items", []):
                sid = site.get("siteId")
                if not sid:
                    continue
                mapping_table.update_item(
                    Key={"userId": user_id, "siteId": sid},
                    UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                    ExpressionAttributeValues={":f": False},
                )
                logger.info("ALEXA_LINKED_CLEARED userId=%s siteId=%s request_id=%s",
                            user_id, sid, request_id)
    except Exception as exc:
        logger.error("ALEXA_LINKED_CLEAR_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)


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

        # If DynamoDB says linked, validate LWA token to detect Alexa-initiated unlink
        if linked and _LWA_SECRET_ARN:
            linked = _validate_and_heal_lwa(user_id, site_id, request_id)
            if not linked:
                linked_at = None

        # Fetch isAlexaEnabled from user_device_details table
        is_alexa_enabled = False
        try:
            details_result = _ddb().Table(_USER_DEVICE_DETAILS_TABLE).get_item(
                Key={"userId": user_id, "siteId": site_id}
            )
            details_item = details_result.get("Item")
            if details_item:
                is_alexa_enabled = bool(details_item.get("isAlexaEnabled", False))
        except Exception as exc:
            logger.warning("DDB_ERROR table=%s error=%s userId=%s siteId=%s request_id=%s",
                           _USER_DEVICE_DETAILS_TABLE, exc, user_id, site_id, request_id)

        logger.info("STATUS_CHECK userId=%s siteId=%s linked=%s linkedAt=%s isAlexaEnabled=%s request_id=%s",
                    user_id, site_id, linked, linked_at, is_alexa_enabled, request_id)

        response: dict = {"linked": linked, "siteId": site_id, "isAlexaEnabled": is_alexa_enabled}
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
