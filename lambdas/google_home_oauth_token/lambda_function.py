"""
google_home_oauth_token — POST /google-home/oauth/token

OAuth 2.0 token endpoint. Google calls this to:
  1. Exchange an authorization code for access + refresh tokens
     (grant_type=authorization_code)
  2. Refresh an expired access token
     (grant_type=refresh_token)

Google credentials (client_id + client_secret) are validated from Secrets Manager.
Our issued tokens are opaque random strings stored in DynamoDB.

Successful response:
  {
    "access_token":  "<opaque>",
    "token_type":    "Bearer",
    "expires_in":    3600,
    "refresh_token": "<opaque>"
  }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

_audit_logger = logging.getLogger("audit")
_audit_logger.setLevel(logging.INFO)


def _audit(event: str, **fields) -> None:
    parts = " ".join(f"{k}={v}" for k, v in fields.items())
    _audit_logger.info("[AUDIT] %s %s", event, parts)


# ── Environment variables ──────────────────────────────────────────────────────
_REGION              = os.environ.get("DATA_REGION", "")
_AUTH_CODES_TABLE    = os.environ.get("GH_AUTH_CODES_TABLE", "google_home_auth_codes")
_TOKENS_TABLE        = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_GH_SECRET_ARN       = os.environ.get("GOOGLE_CLIENT_SECRET_ARN", "")
_GH_SECRET_REGION    = os.environ.get("GOOGLE_SECRET_REGION", "ap-south-1")
_AGENT_ID            = os.environ.get("GOOGLE_AGENT_ID", "")
_ACCESS_TOKEN_TTL_S  = int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "3600"))
_REFRESH_TOKEN_TTL_S = int(os.environ.get("REFRESH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 180)))

_REQUIRED_VARS = {
    "DATA_REGION":               _REGION,
    "GOOGLE_CLIENT_SECRET_ARN":  _GH_SECRET_ARN,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None
_sm_cache = None
_gh_creds_cache: dict | None = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


def _sm():
    global _sm_cache
    if _sm_cache is None:
        _sm_cache = boto3.client("secretsmanager", region_name=_GH_SECRET_REGION)
    return _sm_cache


def _get_gh_credentials() -> dict:
    """
    Fetch Google OAuth client credentials from Secrets Manager.
    Expected secret format: {"client_id": "...", "client_secret": "..."}
    """
    global _gh_creds_cache
    if _gh_creds_cache is not None:
        return _gh_creds_cache
    if not _GH_SECRET_ARN:
        logger.error("GOOGLE_CLIENT_SECRET_ARN not set")
        return {}
    try:
        resp = _sm().get_secret_value(SecretId=_GH_SECRET_ARN)
        _gh_creds_cache = json.loads(resp["SecretString"])
        logger.info("GH_CREDENTIALS_LOADED client_id_prefix=%s",
                    _gh_creds_cache.get("client_id", "")[:8])
    except Exception as exc:
        logger.error("GH_CREDENTIALS_LOAD_ERROR error=%s", exc)
        return {}
    return _gh_creds_cache


def _generate_token() -> str:
    """Generate a cryptographically random opaque token."""
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()


def _validate_client_credentials(client_id: str, client_secret: str) -> bool:
    """Validate Google's client_id and client_secret against stored credentials."""
    creds = _get_gh_credentials()
    if not creds:
        logger.error("GH_CREDENTIALS_UNAVAILABLE — rejecting token request")
        return False
    expected_id = creds.get("client_id", "")
    expected_secret = creds.get("client_secret", "")
    if not expected_id or not expected_secret:
        logger.error("GH_CREDENTIALS_INCOMPLETE")
        return False
    valid = (client_id == expected_id) and (client_secret == expected_secret)
    if not valid:
        logger.warning("GH_CREDENTIALS_MISMATCH provided_id=%s", client_id)
    return valid


def _parse_client_credentials(event: dict, params: dict) -> tuple[str, str]:
    """
    Extract client_id and client_secret from either:
      - HTTP Basic Authorization header, or
      - POST body params
    """
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization") or ""

    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            client_id, client_secret = decoded.split(":", 1)
            logger.debug("CLIENT_CREDS_FROM_BASIC_AUTH client_id=%s", client_id)
            return client_id, client_secret
        except Exception:
            logger.warning("BASIC_AUTH_PARSE_FAILED")

    # Fall back to body params
    return params.get("client_id", ""), params.get("client_secret", "")


# ── Grant handlers ─────────────────────────────────────────────────────────────

def _handle_authorization_code(params: dict, event: dict, request_id: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    code         = params.get("code", "").strip()
    redirect_uri = params.get("redirect_uri", "").strip()

    if not code:
        logger.warning("MISSING_CODE request_id=%s", request_id)
        return _oauth_error("invalid_request", "code is required")

    logger.info("TOKEN_EXCHANGE_START code=%s... request_id=%s", code[:8], request_id)

    # Fetch auth code record
    try:
        resp = _ddb().Table(_AUTH_CODES_TABLE).get_item(Key={"code": code})
        record = resp.get("Item")
    except ClientError as exc:
        logger.error("DDB_GET_CODE_ERROR error=%s request_id=%s", exc, request_id)
        return _oauth_error("server_error", "Internal error")

    if not record:
        logger.warning("AUTH_CODE_NOT_FOUND code=%s... request_id=%s", code[:8], request_id)
        _audit("GH_TOKEN_EXCHANGE_INVALID_CODE", code_prefix=code[:8], request_id=request_id)
        return _oauth_error("invalid_grant", "Authorization code not found or expired")

    # Check TTL
    now = int(time.time())
    if record.get("ttl", 0) < now:
        logger.warning("AUTH_CODE_EXPIRED code=%s... request_id=%s", code[:8], request_id)
        _audit("GH_TOKEN_EXCHANGE_EXPIRED_CODE", request_id=request_id)
        return _oauth_error("invalid_grant", "Authorization code expired")

    # Validate redirect_uri matches what was used during authorization
    stored_redirect = record.get("redirectUri", "")
    if redirect_uri and stored_redirect and redirect_uri != stored_redirect:
        logger.warning("REDIRECT_URI_MISMATCH provided=%s stored=%s request_id=%s",
                       redirect_uri, stored_redirect, request_id)
        _audit("GH_TOKEN_EXCHANGE_REDIRECT_MISMATCH", request_id=request_id)
        return _oauth_error("invalid_grant", "redirect_uri mismatch")

    user_id = record.get("userId", "")
    scope   = record.get("scope", "profile")

    # Delete the code immediately (single-use)
    try:
        _ddb().Table(_AUTH_CODES_TABLE).delete_item(Key={"code": code})
        logger.debug("AUTH_CODE_DELETED code=%s...", code[:8])
    except ClientError as exc:
        logger.error("AUTH_CODE_DELETE_ERROR error=%s code=%s...", exc, code[:8])
        # Non-fatal — continue

    # Issue tokens
    access_token  = _generate_token()
    refresh_token = _generate_token()
    expires_at    = now + _ACCESS_TOKEN_TTL_S
    linked_at     = str(now)

    # Store in tokens table (PK: userId, with GSIs on accessToken + refreshToken)
    try:
        _ddb().Table(_TOKENS_TABLE).put_item(Item={
            "userId":       user_id,
            "accessToken":  access_token,
            "refreshToken": refresh_token,
            "expiresAt":    expires_at,
            "linkedAt":     linked_at,
            "agentId":      _AGENT_ID,
            "scope":        scope,
        })
        logger.info("TOKENS_STORED userId=%s expires_at=%d", user_id, expires_at)
    except ClientError as exc:
        logger.error("TOKENS_STORE_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _oauth_error("server_error", "Failed to store tokens")

    _audit("GH_TOKEN_ISSUED", userId=user_id, grant=user_id,
           access_token_prefix=access_token[:8], request_id=request_id)

    return _token_response(access_token, refresh_token, _ACCESS_TOKEN_TTL_S)


def _handle_refresh_token(params: dict, event: dict, request_id: str) -> dict:
    """Issue a new access token in exchange for a valid refresh token."""
    refresh_token = params.get("refresh_token", "").strip()
    if not refresh_token:
        return _oauth_error("invalid_request", "refresh_token is required")

    logger.info("REFRESH_TOKEN_START token=%s... request_id=%s",
                refresh_token[:8], request_id)

    # Look up by refreshToken GSI
    try:
        resp = _ddb().Table(_TOKENS_TABLE).query(
            IndexName="refreshToken-index",
            KeyConditionExpression="refreshToken = :rt",
            ExpressionAttributeValues={":rt": refresh_token},
            Limit=1,
        )
        items = resp.get("Items", [])
    except ClientError as exc:
        logger.error("DDB_REFRESH_QUERY_ERROR error=%s request_id=%s", exc, request_id)
        return _oauth_error("server_error", "Internal error")

    if not items:
        logger.warning("REFRESH_TOKEN_NOT_FOUND token=%s... request_id=%s",
                       refresh_token[:8], request_id)
        _audit("GH_REFRESH_INVALID_TOKEN", request_id=request_id)
        return _oauth_error("invalid_grant", "Refresh token not found or revoked")

    record  = items[0]
    user_id = record.get("userId", "")
    scope   = record.get("scope", "profile")

    # Issue a new access token (refresh token stays the same)
    now          = int(time.time())
    new_access   = _generate_token()
    expires_at   = now + _ACCESS_TOKEN_TTL_S

    try:
        _ddb().Table(_TOKENS_TABLE).update_item(
            Key={"userId": user_id},
            UpdateExpression="SET accessToken = :at, expiresAt = :ea",
            ExpressionAttributeValues={":at": new_access, ":ea": expires_at},
        )
        logger.info("ACCESS_TOKEN_REFRESHED userId=%s expires_at=%d", user_id, expires_at)
    except ClientError as exc:
        logger.error("TOKEN_REFRESH_UPDATE_ERROR error=%s userId=%s", exc, user_id)
        return _oauth_error("server_error", "Failed to refresh token")

    _audit("GH_TOKEN_REFRESHED", userId=user_id, request_id=request_id)

    return _token_response(new_access, refresh_token, _ACCESS_TOKEN_TTL_S)


# ── Main handler ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=google_home_oauth_token request_id=%s", request_id)

    if _missing_vars:
        return _oauth_error("server_error", "Server configuration error")

    # ── Parse form body ────────────────────────────────────────────────────────
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode()

    try:
        params = dict(urllib.parse.parse_qsl(body_raw, keep_blank_values=True))
    except Exception:
        params = {}

    grant_type = params.get("grant_type", "").strip()
    logger.info("TOKEN_REQUEST grant_type=%s request_id=%s", grant_type, request_id)

    # ── Validate client credentials ────────────────────────────────────────────
    client_id, client_secret = _parse_client_credentials(event, params)
    if not _validate_client_credentials(client_id, client_secret):
        _audit("GH_TOKEN_INVALID_CLIENT", client_id=client_id, request_id=request_id)
        return _oauth_error("invalid_client", "Invalid client credentials")

    logger.debug("CLIENT_CREDENTIALS_OK client_id=%s", client_id)

    # ── Dispatch grant type ────────────────────────────────────────────────────
    if grant_type == "authorization_code":
        return _handle_authorization_code(params, event, request_id)
    elif grant_type == "refresh_token":
        return _handle_refresh_token(params, event, request_id)
    else:
        logger.warning("UNSUPPORTED_GRANT_TYPE grant_type=%s request_id=%s",
                       grant_type, request_id)
        return _oauth_error("unsupported_grant_type",
                            f"grant_type '{grant_type}' is not supported")


def _token_response(access_token: str, refresh_token: str, expires_in: int) -> dict:
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma":        "no-cache",
        },
        "body": json.dumps({
            "access_token":  access_token,
            "token_type":    "Bearer",
            "expires_in":    expires_in,
            "refresh_token": refresh_token,
        }),
    }


def _oauth_error(error: str, description: str = "") -> dict:
    body: dict = {"error": error}
    if description:
        body["error_description"] = description
    status = 401 if error == "invalid_client" else 400
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }
