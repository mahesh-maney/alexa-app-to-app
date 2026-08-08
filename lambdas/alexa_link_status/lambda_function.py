"""
alexa_link_status — GET /api/v1/alexa/status

Returns whether the authenticated user has a linked Alexa account.

Without ?siteId (global check):
  Reads from digilux_honeywell_alexa_lwa_tokens.
  Response: { "linked": bool, "linkedAt": <epoch> }

With ?siteId=<id> (per-site check):
  Reads alexaLinked/alexaLinkedAt from digilux_honeywell_user_device_mapping,
  and isAlexaEnabled from user_device_details (set by create_and_update_data_function).
  When alexaLinked=true, validates the LWA refresh token against Amazon. If the
  token is revoked (skill disabled by user), auto-clears alexaLinked and returns
  linked=false — compensating for Amazon not sending SkillDisabled events reliably.
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
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "DEBUG").upper(), logging.DEBUG))

# Audit logger — always INFO regardless of LOG_LEVEL so audit events are never suppressed
_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    """Emit a structured audit log entry. Always written at INFO level."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION",                "")
_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",           "")
_USER_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",  "")
_USER_DEVICE_DETAILS_TABLE = os.environ.get("USER_DEVICE_DETAILS_TABLE",  "user_device_details")
_COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION       = os.environ.get("COGNITO_REGION",       _REGION or "")
_LWA_SECRET_ARN       = os.environ.get("LWA_SECRET_ARN",
                            "arn:aws:secretsmanager:eu-west-1:986906626244:secret:digilux/alexa/lwa-7RDOUm")
_KMS_KEY_ARN          = os.environ.get("KMS_KEY_ARN", "")
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

logger.info(
    "CONFIG_INIT region=%s tokens_table=%s mapping_table=%s details_table=%s "
    "kms_configured=%s cognito_pool=%s lwa_secret_arn=%s",
    _REGION, _TOKENS_TABLE, _USER_DEVICE_MAPPING_TABLE, _USER_DEVICE_DETAILS_TABLE,
    bool(_KMS_KEY_ARN), _COGNITO_USER_POOL_ID or "(not set)", _LWA_SECRET_ARN,
)

if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)
else:
    logger.info("CONFIG_OK all required env vars present")

if not _COGNITO_USER_POOL_ID:
    logger.warning("SECURITY_WARNING COGNITO_USER_POOL_ID not set — "
                   "JWT signature verification DISABLED (set for production)")

if not _KMS_KEY_ARN:
    logger.warning("SECURITY_WARNING KMS_KEY_ARN not set — "
                   "refresh tokens will be read as plaintext (set for production)")

# ── Module-level caches ────────────────────────────────────────────────────────
_ddb_resource = None
_kms_client = None
_jwks_cache: dict = {}
_lwa_secret_cache: dict = {}


def _ddb():
    global _ddb_resource
    if _ddb_resource is None:
        logger.debug("DDB_INIT cold_start region=%s", _REGION)
        _ddb_resource = boto3.resource("dynamodb", region_name=_REGION)
    return _ddb_resource


def _kms():
    global _kms_client
    if _kms_client is None:
        logger.debug("KMS_INIT cold_start region=%s", _REGION)
        _kms_client = boto3.client("kms", region_name=_REGION)
    return _kms_client


def _decrypt_field(value: str) -> str:
    """Decrypt a KMS-encrypted base64 string. Returns value unchanged if KMS not configured."""
    if not _KMS_KEY_ARN:
        logger.debug("KMS_DECRYPT_SKIP kms_not_configured — using plaintext value")
        return value
    if not value:
        logger.debug("KMS_DECRYPT_SKIP empty_value")
        return value
    try:
        logger.debug("KMS_DECRYPT_START value_len=%d", len(value))
        ciphertext = base64.b64decode(value.encode())
        response = _kms().decrypt(CiphertextBlob=ciphertext)
        plaintext = response["Plaintext"].decode()
        logger.debug("KMS_DECRYPT_OK plaintext_len=%d", len(plaintext))
        return plaintext
    except Exception as exc:
        logger.error("KMS_DECRYPT_ERROR error=%s value_prefix=%s", exc, value[:20])
        return value


def _get_lwa_secret() -> dict:
    """Fetch LWA client_id/client_secret from Secrets Manager (cached per container)."""
    global _lwa_secret_cache
    if _lwa_secret_cache:
        logger.debug("LWA_SECRET_CACHE_HIT keys=%s", list(_lwa_secret_cache.keys()))
        return _lwa_secret_cache
    logger.info("LWA_SECRET_FETCH secret_arn=%s", _LWA_SECRET_ARN)
    try:
        sm = boto3.client("secretsmanager", region_name="eu-west-1")
        value = sm.get_secret_value(SecretId=_LWA_SECRET_ARN)["SecretString"]
        _lwa_secret_cache = json.loads(value)
        logger.info("LWA_SECRET_FETCH_OK keys=%s client_id_prefix=%s",
                    list(_lwa_secret_cache.keys()),
                    (_lwa_secret_cache.get("client_id") or "")[:12])
    except Exception as exc:
        logger.error("LWA_SECRET_FETCH_ERROR secret_arn=%s error=%s", _LWA_SECRET_ARN, exc)
    return _lwa_secret_cache


def _validate_lwa_token(user_id: str, request_id: str) -> bool:
    """
    Verify the user's LWA refresh token is still valid by attempting a refresh.

    Amazon revokes LWA tokens when a user disables the Alexa skill. A 400/401
    from the LWA token endpoint means the skill is disabled. We then auto-clear
    alexaLinked in DynamoDB so the Flutter app shows the correct state on the
    next status check — compensating for Amazon not delivering SkillDisabled
    events reliably to Smart Home skill endpoints.

    Returns True  → token valid, skill still linked.
    Returns False → token revoked, skill disabled.
    Returns True on any non-auth error (network, Secrets Manager) to avoid
    false-positive unlinks.
    """
    logger.info("LWA_VALIDATE_START userId=%s request_id=%s", user_id, request_id)
    _audit("LWA_VALIDATE_BEGIN",
           userId=user_id,
           table=_TOKENS_TABLE,
           request_id=request_id)

    secret = _get_lwa_secret()
    if not secret.get("client_id") or not secret.get("client_secret"):
        logger.warning("LWA_VALIDATE_SKIP reason=missing_credentials userId=%s request_id=%s",
                       user_id, request_id)
        _audit("LWA_VALIDATE_SKIPPED",
               userId=user_id,
               reason="lwa_credentials_unavailable",
               outcome="assumed_valid_to_avoid_false_positive",
               request_id=request_id)
        return True  # Can't validate — don't false-positive

    logger.debug("LWA_VALIDATE_DDB_FETCH table=%s userId=%s request_id=%s",
                 _TOKENS_TABLE, user_id, request_id)
    try:
        token_item = _ddb().Table(_TOKENS_TABLE).get_item(
            Key={"userId": user_id}
        ).get("Item")
    except Exception as exc:
        logger.error("LWA_VALIDATE_DDB_ERROR table=%s userId=%s error=%s request_id=%s",
                     _TOKENS_TABLE, user_id, exc, request_id)
        return True

    if not token_item:
        logger.info("LWA_VALIDATE_NO_TOKEN userId=%s table=%s — no token record, "
                    "skill was never linked via this flow request_id=%s",
                    user_id, _TOKENS_TABLE, request_id)
        _audit("LWA_TOKEN_RECORD_MISSING",
               userId=user_id,
               table=_TOKENS_TABLE,
               outcome="treating_as_unlinked",
               request_id=request_id)
        return False

    logger.debug("LWA_VALIDATE_TOKEN_FOUND userId=%s token_keys=%s request_id=%s",
                 user_id, list(token_item.keys()), request_id)

    raw_refresh = token_item.get("refreshToken", "")
    logger.debug("LWA_VALIDATE_DECRYPT_START userId=%s raw_token_len=%d request_id=%s",
                 user_id, len(raw_refresh), request_id)
    refresh_token = _decrypt_field(raw_refresh)

    if not refresh_token:
        logger.warning("LWA_VALIDATE_NO_REFRESH_TOKEN userId=%s "
                       "raw_present=%s request_id=%s",
                       user_id, bool(raw_refresh), request_id)
        return True  # Can't validate without refresh token

    logger.debug("LWA_VALIDATE_DECRYPT_OK userId=%s plaintext_len=%d request_id=%s",
                 user_id, len(refresh_token), request_id)

    logger.info("LWA_VALIDATE_TOKEN_REFRESH_ATTEMPT userId=%s "
                "endpoint=https://api.amazon.com/auth/o2/token request_id=%s",
                user_id, request_id)

    payload = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     secret["client_id"],
        "client_secret": secret["client_secret"],
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.amazon.com/auth/o2/token",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            new_tokens = json.loads(resp.read())

        expires_in = new_tokens.get("expires_in", "unknown")
        token_type = new_tokens.get("token_type", "unknown")
        logger.info("LWA_TOKEN_REFRESH_SUCCESS userId=%s token_type=%s expires_in=%s request_id=%s",
                    user_id, token_type, expires_in, request_id)
        _audit("LWA_TOKEN_REFRESH_SUCCESS",
               userId=user_id,
               token_type=token_type,
               expires_in=expires_in,
               outcome="skill_still_active",
               request_id=request_id)

        # Refresh succeeded — update stored access token for future use
        logger.debug("LWA_ACCESS_TOKEN_UPDATE_START userId=%s table=%s request_id=%s",
                     user_id, _TOKENS_TABLE, request_id)
        _ddb().Table(_TOKENS_TABLE).update_item(
            Key={"userId": user_id},
            UpdateExpression="SET accessToken = :a",
            ExpressionAttributeValues={":a": new_tokens["access_token"]},
        )
        logger.info("LWA_TOKEN_VALID userId=%s — skill still linked, "
                    "access token refreshed request_id=%s", user_id, request_id)
        _audit("LWA_ACCESS_TOKEN_UPDATED",
               userId=user_id,
               table=_TOKENS_TABLE,
               request_id=request_id)
        return True

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        if exc.code in (400, 401):
            logger.info("LWA_TOKEN_REVOKED userId=%s http=%s body=%s — "
                        "skill disabled by user request_id=%s",
                        user_id, exc.code, body, request_id)
            _audit("LWA_TOKEN_REFRESH_REJECTED",
                   userId=user_id,
                   http_status=exc.code,
                   amazon_response=body[:120],
                   outcome="skill_disabled",
                   request_id=request_id)
            return False
        logger.warning("LWA_VALIDATE_HTTP_ERROR userId=%s http=%s body=%s request_id=%s",
                       user_id, exc.code, body, request_id)
        _audit("LWA_TOKEN_REFRESH_HTTP_ERROR",
               userId=user_id,
               http_status=exc.code,
               amazon_response=body[:120],
               outcome="assumed_valid_to_avoid_false_positive",
               request_id=request_id)
        return True  # Non-auth HTTP error — don't false-positive

    except Exception as exc:
        logger.error("LWA_VALIDATE_ERROR userId=%s error=%s error_type=%s request_id=%s",
                     user_id, exc, type(exc).__name__, request_id)
        _audit("LWA_TOKEN_REFRESH_ERROR",
               userId=user_id,
               error=str(exc)[:120],
               error_type=type(exc).__name__,
               outcome="assumed_valid_to_avoid_false_positive",
               request_id=request_id)
        return True  # Network/timeout — don't false-positive


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
            logger.warning("JWT_DECODE_FAILED reason=wrong_part_count parts=%d", len(parts))
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub") or payload.get("username") or payload.get("cognito:username")
        logger.debug("JWT_DECODE_OK sub=%s iss=%s exp=%s",
                     sub, payload.get("iss"), payload.get("exp"))
        return sub
    except Exception as exc:
        logger.error("JWT_DECODE_ERROR error=%s", exc)
        return None


def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache:
        logger.debug("JWKS_CACHE_HIT key_count=%d", len(_jwks_cache.get("keys", [])))
        return _jwks_cache
    url = f"{_COGNITO_ISSUER}/.well-known/jwks.json"
    logger.info("JWKS_FETCH url=%s", url)
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            _jwks_cache = json.loads(r.read())
        logger.info("JWKS_FETCH_OK key_count=%d", len(_jwks_cache.get("keys", [])))
    except Exception as exc:
        logger.error("JWKS_FETCH_ERROR url=%s error=%s", url, exc)
    return _jwks_cache


def _verify_jwt_signature(token: str) -> None:
    """RS256 signature verification against Cognito JWKS. No-op if pool ID not set."""
    if not _COGNITO_USER_POOL_ID:
        logger.debug("JWT_SIGNATURE_VERIFY_SKIPPED reason=no_cognito_pool_id")
        return
    try:
        import jose.jwt as jwt
        logger.debug("JWT_SIGNATURE_VERIFY_START algorithm=RS256")
        jwks = _get_jwks()
        jwt.decode(token, jwks, algorithms=["RS256"],
                   audience=None, options={"verify_aud": False})
        logger.debug("JWT_SIGNATURE_VERIFY_OK")
    except ImportError:
        logger.error("JWT_VERIFY_UNAVAILABLE python-jose not installed — "
                     "add python-jose[cryptography] to requirements.txt")
    except Exception as exc:
        raise ValueError(f"JWT signature invalid: {exc}") from exc


# ── Handler ────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    source_ip  = ((event.get("requestContext") or {}).get("identity") or {}).get("sourceIp", "unknown")
    user_agent = ((event.get("headers") or {}).get("User-Agent") or
                  (event.get("headers") or {}).get("user-agent") or "unknown")

    logger.info("REQUEST_START function=alexaLinkStatus request_id=%s", request_id)
    _audit("REQUEST_RECEIVED",
           function="alexaLinkStatus",
           http_method=event.get("httpMethod", "unknown"),
           path=event.get("path", "unknown"),
           source_ip=source_ip,
           user_agent=user_agent[:80],
           query_params=event.get("queryStringParameters"),
           request_id=request_id)

    logger.debug("EVENT_RAW httpMethod=%s path=%s queryParams=%s headers_keys=%s request_id=%s",
                 event.get("httpMethod"), event.get("path"),
                 event.get("queryStringParameters"),
                 list((event.get("headers") or {}).keys()),
                 request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        _audit("CONFIG_ERROR",
               reason="missing_required_env_vars",
               missing=_missing_vars,
               request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ───────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""
    logger.debug("AUTH_HEADER_CHECK present=%s request_id=%s",
                 bool(auth_header), request_id)

    if not auth_header.lower().startswith("bearer "):
        logger.warning("AUTH_FAILED reason=missing_or_malformed_bearer_header request_id=%s",
                       request_id)
        _audit("AUTH_FAILURE",
               reason="missing_or_malformed_bearer_header",
               source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    token = auth_header.split(" ", 1)[1].strip()
    logger.debug("AUTH_TOKEN_PARTS parts=%d token_len=%d request_id=%s",
                 token.count(".") + 1, len(token), request_id)

    if token.count(".") != 2:
        logger.warning("AUTH_FAILED reason=malformed_jwt parts=%d request_id=%s",
                       token.count(".") + 1, request_id)
        _audit("AUTH_FAILURE",
               reason="malformed_jwt",
               jwt_parts=token.count(".") + 1,
               source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    logger.debug("AUTH_JWT_SIGNATURE_VERIFY_START request_id=%s", request_id)
    try:
        _verify_jwt_signature(token)
        logger.debug("AUTH_JWT_SIGNATURE_VERIFY_OK request_id=%s", request_id)
    except ValueError as exc:
        logger.warning("AUTH_FAILED reason=invalid_signature detail=%s request_id=%s",
                       exc, request_id)
        _audit("AUTH_FAILURE",
               reason="invalid_jwt_signature",
               detail=str(exc)[:120],
               source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    user_id = _decode_jwt_sub(token)
    if not user_id:
        logger.warning("AUTH_FAILED reason=no_sub_in_jwt request_id=%s", request_id)
        _audit("AUTH_FAILURE",
               reason="no_sub_in_jwt",
               source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)
    _audit("AUTH_SUCCESS",
           userId=user_id,
           source_ip=source_ip,
           request_id=request_id)

    # ── 2. Extract optional siteId query param ────────────────────────────────
    query_params = event.get("queryStringParameters") or {}
    site_id = (query_params.get("siteId") or "").strip()
    logger.info("REQUEST_PARAMS userId=%s siteId=%s mode=%s request_id=%s",
                user_id, site_id or "(none)", "per-site" if site_id else "global", request_id)
    _audit("STATUS_QUERY_RECEIVED",
           userId=user_id,
           siteId=site_id or "(none)",
           mode="per-site" if site_id else "global",
           source_ip=source_ip,
           request_id=request_id)

    # ── 3. Look up status ─────────────────────────────────────────────────────
    if site_id:
        # ── Per-site status ──────────────────────────────────────────────────
        if not _USER_DEVICE_MAPPING_TABLE:
            logger.error("CONFIG_ERROR USER_DEVICE_MAPPING_TABLE not set userId=%s "
                         "siteId=%s request_id=%s", user_id, site_id, request_id)
            _audit("CONFIG_ERROR",
                   reason="USER_DEVICE_MAPPING_TABLE_not_set",
                   userId=user_id,
                   siteId=site_id,
                   request_id=request_id)
            return _resp(500, {"error": "Server configuration error"})

        logger.debug("DDB_GET_MAPPING_START table=%s userId=%s siteId=%s request_id=%s",
                     _USER_DEVICE_MAPPING_TABLE, user_id, site_id, request_id)
        try:
            result = _ddb().Table(_USER_DEVICE_MAPPING_TABLE).get_item(
                Key={"userId": user_id, "siteId": site_id}
            )
        except Exception as exc:
            logger.error("DDB_GET_MAPPING_ERROR table=%s userId=%s siteId=%s error=%s request_id=%s",
                         _USER_DEVICE_MAPPING_TABLE, user_id, site_id, exc, request_id)
            _audit("DDB_READ_FAILURE",
                   table=_USER_DEVICE_MAPPING_TABLE,
                   userId=user_id,
                   siteId=site_id,
                   error=str(exc)[:120],
                   request_id=request_id)
            return _resp(500, {"error": "Server configuration error"})

        item = result.get("Item")
        logger.debug("DDB_GET_MAPPING_RESULT userId=%s siteId=%s item_found=%s "
                     "item_keys=%s request_id=%s",
                     user_id, site_id, bool(item), list(item.keys()) if item else [], request_id)

        linked    = bool(item and item.get("alexaLinked"))
        linked_at = int(item["alexaLinkedAt"]) if item and item.get("alexaLinkedAt") else None
        logger.info("DDB_MAPPING_STATE userId=%s siteId=%s alexaLinked=%s alexaLinkedAt=%s request_id=%s",
                    user_id, site_id, linked, linked_at, request_id)
        _audit("DDB_STATE_READ",
               table=_USER_DEVICE_MAPPING_TABLE,
               userId=user_id,
               siteId=site_id,
               alexaLinked=linked,
               alexaLinkedAt=linked_at,
               request_id=request_id)

        # If DDB says linked, validate the LWA refresh token is still valid.
        if linked:
            logger.info("LWA_VALIDATION_REQUIRED userId=%s siteId=%s — "
                        "DDB says linked, verifying token with Amazon request_id=%s",
                        user_id, site_id, request_id)
            _audit("LWA_TOKEN_VALIDATION_STARTED",
                   userId=user_id,
                   siteId=site_id,
                   reason="ddb_shows_linked",
                   endpoint="https://api.amazon.com/auth/o2/token",
                   request_id=request_id)

            still_valid = _validate_lwa_token(user_id, request_id)
            logger.info("LWA_VALIDATION_RESULT userId=%s siteId=%s still_valid=%s request_id=%s",
                        user_id, site_id, still_valid, request_id)

            if still_valid:
                _audit("LWA_TOKEN_VALID",
                       userId=user_id,
                       siteId=site_id,
                       outcome="skill_still_linked",
                       request_id=request_id)
            else:
                _audit("LWA_TOKEN_REVOKED",
                       userId=user_id,
                       siteId=site_id,
                       outcome="skill_disabled_by_user",
                       action="auto_unlink_initiated",
                       request_id=request_id)

                logger.info("SKILL_DISABLED_DETECTED userId=%s siteId=%s — "
                            "clearing alexaLinked in DDB and deleting token record request_id=%s",
                            user_id, site_id, request_id)
                linked    = False
                linked_at = None
                try:
                    logger.debug("SKILL_DISABLED_CLEAR_MAPPING userId=%s siteId=%s request_id=%s",
                                 user_id, site_id, request_id)
                    _ddb().Table(_USER_DEVICE_MAPPING_TABLE).update_item(
                        Key={"userId": user_id, "siteId": site_id},
                        UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                        ExpressionAttributeValues={":f": False},
                    )
                    _audit("ALEXA_UNLINKED",
                           userId=user_id,
                           siteId=site_id,
                           trigger="lwa_token_revoked",
                           action="alexaLinked_cleared_in_ddb",
                           table=_USER_DEVICE_MAPPING_TABLE,
                           request_id=request_id)

                    logger.debug("SKILL_DISABLED_DELETE_TOKEN userId=%s table=%s request_id=%s",
                                 user_id, _TOKENS_TABLE, request_id)
                    _ddb().Table(_TOKENS_TABLE).delete_item(Key={"userId": user_id})
                    _audit("LWA_TOKEN_RECORD_DELETED",
                           userId=user_id,
                           siteId=site_id,
                           trigger="lwa_token_revoked",
                           table=_TOKENS_TABLE,
                           request_id=request_id)

                    logger.info("SKILL_DISABLED_AUTO_CLEARED userId=%s siteId=%s — "
                                "DDB state reset, app will show Link Account request_id=%s",
                                user_id, site_id, request_id)
                    _audit("AUTO_UNLINK_COMPLETE",
                           userId=user_id,
                           siteId=site_id,
                           outcome="success",
                           request_id=request_id)
                except Exception as exc:
                    logger.error("SKILL_DISABLED_CLEAR_ERROR userId=%s siteId=%s "
                                 "error=%s error_type=%s request_id=%s",
                                 user_id, site_id, exc, type(exc).__name__, request_id)
                    _audit("AUTO_UNLINK_FAILURE",
                           userId=user_id,
                           siteId=site_id,
                           error=str(exc)[:120],
                           error_type=type(exc).__name__,
                           request_id=request_id)
        else:
            logger.info("LWA_VALIDATION_SKIPPED userId=%s siteId=%s — "
                        "DDB says not linked, no token check needed request_id=%s",
                        user_id, site_id, request_id)
            _audit("LWA_TOKEN_VALIDATION_SKIPPED",
                   userId=user_id,
                   siteId=site_id,
                   reason="ddb_shows_not_linked",
                   request_id=request_id)

        # Fetch isAlexaEnabled from user_device_details table
        logger.debug("DDB_GET_DETAILS_START table=%s userId=%s siteId=%s request_id=%s",
                     _USER_DEVICE_DETAILS_TABLE, user_id, site_id, request_id)
        is_alexa_enabled = False
        try:
            details_result = _ddb().Table(_USER_DEVICE_DETAILS_TABLE).get_item(
                Key={"userId": user_id, "siteId": site_id}
            )
            details_item = details_result.get("Item")
            if details_item:
                is_alexa_enabled = bool(details_item.get("isAlexaEnabled", False))
                logger.debug("DDB_GET_DETAILS_RESULT userId=%s siteId=%s "
                             "isAlexaEnabled=%s request_id=%s",
                             user_id, site_id, is_alexa_enabled, request_id)
            else:
                logger.debug("DDB_GET_DETAILS_RESULT userId=%s siteId=%s "
                             "item_found=False — isAlexaEnabled defaults to False request_id=%s",
                             user_id, site_id, request_id)
        except Exception as exc:
            logger.warning("DDB_GET_DETAILS_ERROR table=%s userId=%s siteId=%s "
                           "error=%s — isAlexaEnabled defaults to False request_id=%s",
                           _USER_DEVICE_DETAILS_TABLE, user_id, site_id, exc, request_id)
            _audit("DDB_READ_FAILURE",
                   table=_USER_DEVICE_DETAILS_TABLE,
                   userId=user_id,
                   siteId=site_id,
                   error=str(exc)[:120],
                   note="isAlexaEnabled_defaulted_to_false",
                   request_id=request_id)

        response: dict = {"linked": linked, "siteId": site_id, "isAlexaEnabled": is_alexa_enabled}
        if linked_at is not None:
            response["linkedAt"] = linked_at

        logger.info("RESPONSE_OK userId=%s siteId=%s linked=%s linkedAt=%s "
                    "isAlexaEnabled=%s status=200 request_id=%s",
                    user_id, site_id, linked, linked_at, is_alexa_enabled, request_id)
        _audit("STATUS_RESPONSE_SENT",
               userId=user_id,
               siteId=site_id,
               linked=linked,
               linkedAt=linked_at,
               isAlexaEnabled=is_alexa_enabled,
               http_status=200,
               request_id=request_id)
        return _resp(200, response)

    else:
        # ── Global (non-site) status ──────────────────────────────────────────
        logger.debug("DDB_GET_TOKEN_START table=%s userId=%s request_id=%s",
                     _TOKENS_TABLE, user_id, request_id)
        try:
            result = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        except Exception as exc:
            logger.error("DDB_GET_TOKEN_ERROR table=%s userId=%s error=%s request_id=%s",
                         _TOKENS_TABLE, user_id, exc, request_id)
            _audit("DDB_READ_FAILURE",
                   table=_TOKENS_TABLE,
                   userId=user_id,
                   error=str(exc)[:120],
                   request_id=request_id)
            return _resp(500, {"error": "Server configuration error"})

        item = result.get("Item")
        logger.debug("DDB_GET_TOKEN_RESULT userId=%s item_found=%s "
                     "item_keys=%s request_id=%s",
                     user_id, bool(item), list(item.keys()) if item else [], request_id)

        if not item:
            logger.info("RESPONSE_OK userId=%s linked=false — no token record "
                        "in %s status=200 request_id=%s", user_id, _TOKENS_TABLE, request_id)
            _audit("STATUS_RESPONSE_SENT",
                   userId=user_id,
                   siteId="(global)",
                   linked=False,
                   reason="no_lwa_token_record",
                   http_status=200,
                   request_id=request_id)
            return _resp(200, {"linked": False})

        linked_at   = item.get("linkedAt")
        link_method = item.get("linkMethod", "unknown")
        logger.info("RESPONSE_OK userId=%s linked=true linkedAt=%s linkMethod=%s "
                    "status=200 request_id=%s",
                    user_id, linked_at, link_method, request_id)
        _audit("STATUS_RESPONSE_SENT",
               userId=user_id,
               siteId="(global)",
               linked=True,
               linkedAt=linked_at,
               linkMethod=link_method,
               http_status=200,
               request_id=request_id)

        response = {"linked": True}
        if linked_at is not None:
            response["linkedAt"] = int(linked_at)
        return _resp(200, response)
