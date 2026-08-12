"""
alexa_unlink — DELETE /api/v1/alexa/unlink

Unlinks a user's Alexa account from Digilux.

Flow:
  1. Authenticate the caller via Cognito JWT (Authorization: Bearer <token>)
  2. Look up the user's LWA tokens in DynamoDB
  3. If no tokens found, clear per-site mapping (if siteId given) and return 200 (idempotent)
  4. Disable the Alexa skill for this user (best-effort — failure is logged, not fatal)
  5. Revoke the refresh token with Amazon LWA (best-effort — failure is logged, not fatal)
  6. Delete the token record from DynamoDB
  7. Emit [AUDIT] ACCOUNT_UNLINKED
  8. Return { "unlinked": true }

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
_LWA_SECRET_ARN            = os.environ.get("LWA_SECRET_ARN",             "")
_LWA_SECRET_REGION    = os.environ.get("LWA_SECRET_REGION",    "")
_LWA_REVOKE_URL       = os.environ.get("LWA_REVOKE_URL",       "")
_SKILL_ENABLEMENT_URL = os.environ.get("SKILL_ENABLEMENT_URL", "")
_ALEXA_SKILL_ID       = os.environ.get("ALEXA_SKILL_ID",       "")
_KMS_KEY_ARN          = os.environ.get("KMS_KEY_ARN",          "")
_COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION       = os.environ.get("COGNITO_REGION",       _REGION or "")
_CREATE_UPDATE_FN     = os.environ.get("CREATE_UPDATE_FN",  "")
_LWA_HTTP_TIMEOUT     = int(os.environ.get("LWA_HTTP_TIMEOUT", "10"))  # optional
_COGNITO_ISSUER = (
    f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    if _COGNITO_USER_POOL_ID else ""
)

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "LWA_TOKENS_TABLE": _TOKENS_TABLE,
    "LWA_SECRET_ARN":   _LWA_SECRET_ARN,
    "LWA_SECRET_REGION": _LWA_SECRET_REGION,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)

# ── Startup security warnings ──────────────────────────────────────────────────
if not _COGNITO_USER_POOL_ID:
    logger.warning("SECURITY_WARNING COGNITO_USER_POOL_ID not set — "
                   "JWT signature verification DISABLED (set for production)")
if not _KMS_KEY_ARN:
    logger.warning("SECURITY_WARNING KMS_KEY_ARN not set — "
                   "tokens stored/read in plaintext (set for production)")
if not _LWA_REVOKE_URL:
    logger.warning("SECURITY_WARNING LWA_REVOKE_URL not set — "
                   "refresh token will NOT be revoked with Amazon on unlink")
if not _SKILL_ENABLEMENT_URL or not _ALEXA_SKILL_ID:
    logger.warning("SECURITY_WARNING SKILL_ENABLEMENT_URL or ALEXA_SKILL_ID not set — "
                   "skill will NOT be disabled on unlink")

# Module-level caches
_dynamodb        = None
_lwa_creds: tuple[str, str] | None = None
_kms_cache       = None
_jwks_cache: dict | None = None
_lambda_client   = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    else:
        logger.debug("DDB_REUSE region=%s (warm container)", _REGION)
    return _dynamodb


def _kms():
    global _kms_cache
    if _kms_cache is None:
        logger.debug("KMS_INIT region=%s (cold start)", _REGION)
        _kms_cache = boto3.client("kms", region_name=_REGION)
    return _kms_cache


def _decrypt_field(value: str) -> str:
    """Decrypt a KMS-encrypted base64 string back to plaintext.
    Returns value unchanged if KMS_KEY_ARN is not configured."""
    if not _KMS_KEY_ARN:
        return value
    ciphertext = base64.b64decode(value.encode())
    response   = _kms().decrypt(CiphertextBlob=ciphertext)
    return response["Plaintext"].decode()


def _get_lwa_creds() -> tuple[str, str]:
    """Load LWA client_id + client_secret from Secrets Manager (cached per container)."""
    global _lwa_creds
    if _lwa_creds is None:
        logger.info("SECRETS_FETCH arn=%s region=%s (cold start)", _LWA_SECRET_ARN, _LWA_SECRET_REGION)
        sm  = boto3.client("secretsmanager", region_name=_LWA_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_LWA_SECRET_ARN)["SecretString"]
        d   = json.loads(raw)
        _lwa_creds = d["client_id"], d["client_secret"]
        logger.info("SECRETS_LOADED client_id_prefix=%s", d["client_id"][:6])
    else:
        logger.debug("SECRETS_CACHED (warm container, skipping Secrets Manager call)")
    return _lwa_creds


def _get_jwks() -> dict:
    """Fetch and cache the Cognito JWKS for JWT signature verification."""
    global _jwks_cache
    if _jwks_cache is None:
        url = f"{_COGNITO_ISSUER}/.well-known/jwks.json"
        logger.debug("JWKS_FETCH url=%s", url)
        with urllib.request.urlopen(url, timeout=5) as r:
            _jwks_cache = json.loads(r.read())
        logger.info("JWKS_LOADED key_count=%d", len(_jwks_cache.get("keys", [])))
    else:
        logger.debug("JWKS_CACHED")
    return _jwks_cache


def _verify_jwt_signature(token: str) -> None:
    """Verify the JWT RS256 signature against Cognito JWKS (defense-in-depth).
    No-op if COGNITO_USER_POOL_ID is not set. Raises ValueError if invalid."""
    if not _COGNITO_USER_POOL_ID:
        logger.warning("JWT_VERIFY_SKIPPED COGNITO_USER_POOL_ID not configured")
        return
    try:
        from jose import jwt as jose_jwt
    except ImportError:
        logger.error("JWT_VERIFY_UNAVAILABLE python-jose not installed — "
                     "add python-jose[cryptography] to requirements.txt")
        return
    try:
        parts   = token.split(".")
        padding = "=" * (4 - len(parts[0]) % 4)
        header  = json.loads(base64.urlsafe_b64decode(parts[0] + padding))
        kid     = header.get("kid", "")
        jwks    = _get_jwks()
        key     = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key:
            raise ValueError(f"JWT kid='{kid}' not found in JWKS")
        jose_jwt.decode(
            token, key,
            algorithms=["RS256"],
            issuer=_COGNITO_ISSUER,
            options={"verify_at_hash": False},
        )
        logger.debug("JWT_SIGNATURE_VERIFIED kid=%s", kid)
    except Exception as exc:
        logger.warning("JWT_SIGNATURE_INVALID reason=%s", exc)
        raise ValueError(f"JWT signature invalid: {exc}") from exc


def _decode_jwt_sub(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        logger.debug("JWT_INVALID part_count=%d expected=3", len(parts))
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    padding = "=" * (4 - len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception as exc:
        logger.debug("JWT_DECODE_FAILED reason=%s", exc)
        raise ValueError(f"Could not decode JWT payload: {exc}") from exc
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        logger.debug("JWT_MISSING_SUB available_claims=%s", list(claims.keys()))
        raise ValueError("Missing sub/username claim in JWT")
    claim_source = "sub" if claims.get("sub") else "username"
    logger.debug("JWT_DECODED claim_source=%s", claim_source)
    return sub


def _disable_alexa_skill(access_token: str, user_id: str, request_id: str) -> None:
    """
    Call the Alexa Skill Disablement API (best-effort).
    Logs failure but does NOT raise — unlink proceeds regardless.
    """
    if not _SKILL_ENABLEMENT_URL or not _ALEXA_SKILL_ID:
        logger.warning("SKILL_DISABLE_SKIPPED missing SKILL_ENABLEMENT_URL or ALEXA_SKILL_ID "
                       "userId=%s request_id=%s", user_id, request_id)
        return

    url = f"{_SKILL_ENABLEMENT_URL}/{_ALEXA_SKILL_ID}/enablement"
    logger.debug("SKILL_DISABLE_START url=%s userId=%s request_id=%s",
                 url, user_id, request_id)

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=_LWA_HTTP_TIMEOUT):
            pass
        logger.info("SKILL_DISABLE_OK skill_id=%s userId=%s request_id=%s",
                    _ALEXA_SKILL_ID, user_id, request_id)
    except urllib.error.HTTPError as e:
        err = (e.read() if e.fp is not None else b"").decode(errors="replace")
        logger.warning("SKILL_DISABLE_HTTP_ERROR skill_id=%s status=%d body=%s "
                       "userId=%s request_id=%s",
                       _ALEXA_SKILL_ID, e.code, err, user_id, request_id)
    except Exception as exc:
        logger.warning("SKILL_DISABLE_ERROR skill_id=%s error=%s userId=%s request_id=%s",
                       _ALEXA_SKILL_ID, exc, user_id, request_id)


def _revoke_refresh_token(refresh_token: str, user_id: str, request_id: str) -> None:
    """
    Revoke the refresh token with Amazon LWA (best-effort).
    Logs failure but does NOT raise — unlink proceeds regardless.
    """
    if not _LWA_REVOKE_URL:
        logger.warning("TOKEN_REVOKE_SKIPPED LWA_REVOKE_URL not configured "
                       "userId=%s request_id=%s", user_id, request_id)
        return

    logger.debug("TOKEN_REVOKE_START endpoint=%s userId=%s request_id=%s",
                 _LWA_REVOKE_URL, user_id, request_id)

    try:
        client_id, client_secret = _get_lwa_creds()
    except Exception as exc:
        logger.warning("TOKEN_REVOKE_CREDS_ERROR error=%s userId=%s request_id=%s",
                       exc, user_id, request_id)
        return

    data = urllib.parse.urlencode({
        "token":           refresh_token,
        "token_type_hint": "refresh_token",
        "client_id":       client_id,
        "client_secret":   client_secret,
    }).encode()

    req = urllib.request.Request(
        _LWA_REVOKE_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_LWA_HTTP_TIMEOUT):
            pass
        logger.info("TOKEN_REVOKE_OK userId=%s request_id=%s", user_id, request_id)
    except urllib.error.HTTPError as e:
        err = (e.read() if e.fp is not None else b"").decode(errors="replace")
        logger.warning("TOKEN_REVOKE_HTTP_ERROR status=%d body=%s userId=%s request_id=%s",
                       e.code, err, user_id, request_id)
    except Exception as exc:
        logger.warning("TOKEN_REVOKE_ERROR error=%s userId=%s request_id=%s",
                       exc, user_id, request_id)


def _notify_alexa_disabled(user_id: str, site_id: str) -> None:
    """Async fire-and-forget: set isAlexaEnabled=false on the site. Non-fatal."""
    if not _CREATE_UPDATE_FN or not site_id:
        return
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=_REGION)
    payload = json.dumps({
        "_source":        "alexa_internal",
        "userId":         user_id,
        "siteId":         site_id,
        "isAlexaEnabled": False,
    }).encode()
    try:
        _lambda_client.invoke(
            FunctionName=_CREATE_UPDATE_FN,
            InvocationType="Event",
            Payload=payload,
        )
        logger.info("ALEXA_DISABLED_NOTIFY_SENT fn=%s userId=%s siteId=%s",
                    _CREATE_UPDATE_FN, user_id, site_id)
    except Exception as exc:
        logger.warning("ALEXA_DISABLED_NOTIFY_FAILED fn=%s userId=%s siteId=%s error=%s",
                       _CREATE_UPDATE_FN, user_id, site_id, exc)


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaUnlink request_id=%s", request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ───────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth    = headers.get("Authorization") or headers.get("authorization") or ""

    if not auth:
        logger.warning("AUTH_MISSING no Authorization header request_id=%s", request_id)
        return _resp(401, {"error": "Unauthorized"})

    scheme = auth.split(" ")[0] if " " in auth else auth
    logger.debug("AUTH_HEADER_PRESENT scheme=%s request_id=%s", scheme, request_id)

    token = auth.split(" ")[-1].strip()

    try:
        _verify_jwt_signature(token)          # defense-in-depth RS256 check
        user_id = _decode_jwt_sub(token)
        logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)
    except Exception as exc:
        logger.warning("AUTH_FAILED reason=%s request_id=%s", exc, request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 1b. Extract optional siteId query param ───────────────────────────────
    site_id = ((event.get("queryStringParameters") or {}).get("siteId") or "").strip()

    # ── 2. Look up user's tokens ──────────────────────────────────────────────
    tokens_table = _ddb().Table(_TOKENS_TABLE)
    logger.debug("TOKEN_LOOKUP table=%s userId=%s request_id=%s",
                 _TOKENS_TABLE, user_id, request_id)

    result = tokens_table.get_item(Key={"userId": user_id})
    item   = result.get("Item")

    if not item:
        logger.warning("TOKEN_NOT_FOUND userId=%s request_id=%s — cleaning up site mapping if siteId present",
                       user_id, request_id)
        # Token already gone — still clear per-site state so UI stays consistent
        if site_id and _USER_DEVICE_MAPPING_TABLE:
            try:
                _ddb().Table(_USER_DEVICE_MAPPING_TABLE).update_item(
                    Key={"userId": user_id, "siteId": site_id},
                    UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                    ExpressionAttributeValues={":f": False},
                )
                logger.info("SITE_MAPPING_CLEARED userId=%s siteId=%s request_id=%s",
                            user_id, site_id, request_id)
            except Exception as exc:
                logger.warning("SITE_MAPPING_CLEAR_FAILED userId=%s siteId=%s error=%s request_id=%s",
                               user_id, site_id, exc, request_id)
            _notify_alexa_disabled(user_id, site_id)
        return _resp(200, {"unlinked": True})

    logger.debug("TOKEN_FOUND userId=%s linked_at=%s request_id=%s",
                 user_id, item.get("linkedAt"), request_id)

    # ── 3. Decrypt tokens ─────────────────────────────────────────────────────
    try:
        access_token  = _decrypt_field(item.get("accessToken",  ""))
        refresh_token = _decrypt_field(item.get("refreshToken", ""))
    except Exception as exc:
        logger.error("TOKEN_DECRYPT_FAILED userId=%s error=%s request_id=%s",
                     user_id, exc, request_id)
        # Continue unlink even if decryption fails — still delete the record
        access_token  = ""
        refresh_token = ""

    # ── 4. Disable Alexa skill (best-effort) ──────────────────────────────────
    if access_token:
        logger.info("SKILL_DISABLE userId=%s skill_id=%s request_id=%s",
                    user_id, _ALEXA_SKILL_ID, request_id)
        try:
            _disable_alexa_skill(access_token, user_id, request_id)
        except Exception as exc:
            logger.warning("SKILL_DISABLE_UNEXPECTED_ERROR userId=%s error=%s request_id=%s",
                           user_id, exc, request_id)
    else:
        logger.warning("SKILL_DISABLE_SKIPPED no access_token userId=%s request_id=%s",
                       user_id, request_id)

    # ── 5. Revoke refresh token with Amazon LWA (best-effort) ─────────────────
    if refresh_token:
        logger.info("TOKEN_REVOKE userId=%s request_id=%s", user_id, request_id)
        try:
            _revoke_refresh_token(refresh_token, user_id, request_id)
        except Exception as exc:
            logger.warning("TOKEN_REVOKE_UNEXPECTED_ERROR userId=%s error=%s request_id=%s",
                           user_id, exc, request_id)
    else:
        logger.warning("TOKEN_REVOKE_SKIPPED no refresh_token userId=%s request_id=%s",
                       user_id, request_id)

    # ── 6. Delete token record from DynamoDB ─────────────────────────────────
    logger.debug("TOKEN_DELETE_START table=%s userId=%s request_id=%s",
                 _TOKENS_TABLE, user_id, request_id)
    tokens_table.delete_item(Key={"userId": user_id})
    logger.debug("TOKEN_DELETE_OK table=%s userId=%s request_id=%s",
                 _TOKENS_TABLE, user_id, request_id)

    # ── 6b. Clear per-site linked flag in user-device mapping table ───────────
    if site_id and _USER_DEVICE_MAPPING_TABLE:
        logger.info("SITE_UNLINK_UPDATE table=%s userId=%s siteId=%s request_id=%s",
                    _USER_DEVICE_MAPPING_TABLE, user_id, site_id, request_id)
        try:
            _ddb().Table(_USER_DEVICE_MAPPING_TABLE).update_item(
                Key={"userId": user_id, "siteId": site_id},
                UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                ExpressionAttributeValues={":f": False},
            )
            logger.info("SITE_UNLINK_UPDATE_OK userId=%s siteId=%s request_id=%s",
                        user_id, site_id, request_id)
        except Exception as exc:
            logger.error("SITE_UNLINK_UPDATE_FAILED userId=%s siteId=%s error=%s request_id=%s",
                         user_id, site_id, exc, request_id)
    elif site_id:
        logger.warning("SITE_UNLINK_UPDATE_SKIPPED USER_DEVICE_MAPPING_TABLE not configured "
                       "userId=%s siteId=%s request_id=%s", user_id, site_id, request_id)

    # AUDIT — account unlinked
    logger.info(
        "[AUDIT] ACCOUNT_UNLINKED userId=%s siteId=%s skill_id=%s "
        "skill_disabled=%s token_revoked=%s request_id=%s",
        user_id, site_id or "none", _ALEXA_SKILL_ID or "unknown",
        bool(access_token and _SKILL_ENABLEMENT_URL and _ALEXA_SKILL_ID),
        bool(refresh_token and _LWA_REVOKE_URL),
        request_id,
    )

    # ── 6c. Notify create_and_update_data_function to set isAlexaEnabled=false ─
    _notify_alexa_disabled(user_id, site_id)

    # ── 7. Return success ─────────────────────────────────────────────────────
    logger.info("REQUEST_OK function=alexaUnlink userId=%s request_id=%s",
                user_id, request_id)
    return _resp(200, {"unlinked": True})


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
