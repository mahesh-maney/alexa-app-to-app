"""
alexa_start_app_to_app — POST /api/v1/alexa/startAppToApp

Initiates an Alexa App-to-App account linking session.

Flow:
  1. Authenticate the caller via Cognito JWT (Authorization: Bearer <token>)
  2. Generate a cryptographically random state (CSRF token)
  3. Generate a PKCE code_verifier + code_challenge (SHA-256)
  4. Persist the session in DynamoDB with a 10-minute TTL
  5. Return { state, codeChallenge, redirectUri } to the Flutter app

The Flutter app uses the returned values to build the Alexa companion URL
and open either the Alexa app or a Login-with-Amazon browser fallback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
# All values are required unless marked optional.
# Set them as Lambda environment variables (or export them locally).
# See .env.example at the repo root for descriptions of every variable.
_REGION        = os.environ.get("DATA_REGION",         "")
_SESSION_TABLE = os.environ.get("SESSION_TABLE",        "")
_REDIRECT_URI  = os.environ.get("REDIRECT_URI",         "")
_SESSION_TTL_S = int(os.environ.get("SESSION_TTL_SECONDS", "600"))  # optional
_KMS_KEY_ARN   = os.environ.get("KMS_KEY_ARN", "")
_COGNITO_USER_POOL_ID   = os.environ.get("COGNITO_USER_POOL_ID",    "")
_COGNITO_REGION         = os.environ.get("COGNITO_REGION",          _REGION or "")
_ALLOWED_REDIRECT_HOSTS = set(
    h.strip() for h in os.environ.get("ALLOWED_REDIRECT_HOSTS", "").split(",") if h.strip()
)
_COGNITO_ISSUER = (
    f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    if _COGNITO_USER_POOL_ID else ""
)

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {"DATA_REGION": _REGION, "SESSION_TABLE": _SESSION_TABLE,
                  "REDIRECT_URI": _REDIRECT_URI}
_missing_vars  = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)

# ── Startup security warnings ──────────────────────────────────────────────────
if not _COGNITO_USER_POOL_ID:
    logger.warning("SECURITY_WARNING COGNITO_USER_POOL_ID not set — "
                   "JWT signature verification DISABLED (set for production)")
if not _KMS_KEY_ARN:
    logger.warning("SECURITY_WARNING KMS_KEY_ARN not set — "
                   "codeVerifier stored in plaintext (set for production)")
if _REDIRECT_URI:
    _p = urllib.parse.urlparse(_REDIRECT_URI)
    if _p.scheme != "https":
        logger.error("SECURITY_CONFIG_ERROR REDIRECT_URI must use HTTPS, got=%s", _p.scheme)
    elif _ALLOWED_REDIRECT_HOSTS and _p.netloc not in _ALLOWED_REDIRECT_HOSTS:
        logger.error("SECURITY_CONFIG_ERROR REDIRECT_URI host='%s' not in ALLOWED_REDIRECT_HOSTS=%s",
                     _p.netloc, _ALLOWED_REDIRECT_HOSTS)
    else:
        logger.info("REDIRECT_URI_VALID scheme=https host=%s", _p.netloc)

# Module-level caches
_dynamodb     = None
_kms_cache    = None
_jwks_cache: dict | None = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_INIT region=%s table=%s (cold start)", _REGION, _SESSION_TABLE)
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


def _encrypt_field(plaintext: str) -> str:
    """KMS-encrypt a string and return base64-encoded ciphertext.
    Returns plaintext unchanged if KMS_KEY_ARN is not configured (logs warning)."""
    if not _KMS_KEY_ARN:
        return plaintext
    response = _kms().encrypt(KeyId=_KMS_KEY_ARN, Plaintext=plaintext.encode())
    return base64.b64encode(response["CiphertextBlob"]).decode()


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
    No-op if COGNITO_USER_POOL_ID is not set — logs a security warning.
    Raises ValueError if signature is invalid."""
    if not _COGNITO_USER_POOL_ID:
        logger.warning("JWT_VERIFY_SKIPPED COGNITO_USER_POOL_ID not configured")
        return
    try:
        from jose import jwt as jose_jwt  # python-jose[cryptography] in requirements.txt
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
    """
    Extract the Cognito `sub` claim from a JWT without re-verifying the
    signature — the API Gateway Cognito authorizer already validated it.
    """
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


def _generate_pkce() -> tuple[str, str]:
    """
    Generate a PKCE (RFC 7636) code_verifier and code_challenge pair.

    code_verifier : 32 random bytes → base64url (43 chars, no padding)
    code_challenge: BASE64URL(SHA256(code_verifier))
    """
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    logger.debug("PKCE_GENERATED verifier_len=%d challenge_len=%d method=S256",
                 len(verifier), len(challenge))
    return verifier, challenge


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=startAppToApp request_id=%s", request_id)

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

    # ── 2. Generate state + PKCE ──────────────────────────────────────────────
    logger.debug("GENERATING_PKCE userId=%s request_id=%s", user_id, request_id)
    state                         = str(uuid.uuid4())
    code_verifier, code_challenge = _generate_pkce()
    now                           = int(time.time())
    expires_at                    = now + _SESSION_TTL_S

    logger.debug(
        "SESSION_PARAMS userId=%s state=%s created_at=%d expires_at=%d ttl_seconds=%d request_id=%s",
        user_id, state, now, expires_at, _SESSION_TTL_S, request_id,
    )

    # ── 3. Persist session (codeVerifier KMS-encrypted if key configured) ────
    session_table = _ddb().Table(_SESSION_TABLE)
    encrypted_verifier = _encrypt_field(code_verifier)
    logger.debug("DDB_PUT_START table=%s userId=%s state=%s kms_enabled=%s request_id=%s",
                 _SESSION_TABLE, user_id, state, bool(_KMS_KEY_ARN), request_id)

    session_table.put_item(Item={
        "state":        state,
        "userId":       user_id,
        "codeVerifier": encrypted_verifier,  # KMS-encrypted; never returned to client
        "status":       "PENDING",
        "createdAt":    now,
        "expiresAt":    expires_at,
        "ttl":          expires_at,
    })

    logger.debug("DDB_PUT_OK table=%s userId=%s state=%s request_id=%s",
                 _SESSION_TABLE, user_id, state, request_id)

    # AUDIT — critical security event: new linking session created
    logger.info(
        "[AUDIT] SESSION_CREATED userId=%s state=%s status=PENDING "
        "created_at=%d expires_at=%d ttl_seconds=%d redirect_uri=%s request_id=%s",
        user_id, state, now, expires_at, _SESSION_TTL_S, _REDIRECT_URI, request_id,
    )

    # ── 4. Return state + PKCE values to Flutter ─────────────────────────────
    logger.info(
        "REQUEST_OK function=startAppToApp userId=%s state=%s request_id=%s",
        user_id, state, request_id,
    )
    return _resp(200, {
        "state":         state,
        "codeChallenge": code_challenge,
        "redirectUri":   _REDIRECT_URI,
    })


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body":       json.dumps(body),
    }
