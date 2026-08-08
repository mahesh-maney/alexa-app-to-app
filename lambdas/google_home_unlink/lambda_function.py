"""
google_home_unlink — DELETE /api/v1/voice/google-home/account-linking

Unlinks the Google Home account for the authenticated user:
  1. Fetches the token record to confirm it exists
  2. Attempts to revoke the access token with Google (best-effort)
  3. Deletes the token record from DynamoDB

Response: { "linked": false }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request

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
_REGION            = os.environ.get("DATA_REGION", "")
_TOKENS_TABLE      = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_GH_SECRET_ARN     = os.environ.get("GOOGLE_CLIENT_SECRET_ARN", "")
_GH_SECRET_REGION  = os.environ.get("GOOGLE_SECRET_REGION", "ap-south-1")
_HTTP_TIMEOUT      = int(os.environ.get("HTTP_TIMEOUT", "10"))

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None
_sm_cache = None
_gh_secret_cache: dict | None = None


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


def _get_gh_secret() -> dict:
    """Fetch and cache Google OAuth client credentials from Secrets Manager."""
    global _gh_secret_cache
    if _gh_secret_cache is not None:
        return _gh_secret_cache
    if not _GH_SECRET_ARN:
        logger.warning("GH_SECRET_NOT_CONFIGURED — token revocation will be skipped")
        return {}
    try:
        resp = _sm().get_secret_value(SecretId=_GH_SECRET_ARN)
        _gh_secret_cache = json.loads(resp["SecretString"])
        logger.info("GH_SECRET_LOADED")
    except Exception as exc:
        logger.error("GH_SECRET_LOAD_ERROR error=%s", exc)
        return {}
    return _gh_secret_cache


def _decode_jwt_sub(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    padding = "=" * (4 - len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        raise ValueError("Missing sub/username claim in JWT")
    return sub


def _revoke_google_token(access_token: str) -> None:
    """
    Best-effort: revoke the access token with Google.
    Google's revoke endpoint: POST https://oauth2.googleapis.com/revoke
    with body: token=<access_token>
    Errors are logged but do NOT fail the unlink operation.
    """
    try:
        data = urllib.parse.urlencode({"token": access_token}).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/revoke",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            status = resp.status
            logger.info("GH_TOKEN_REVOKE_OK status=%d", status)
    except Exception as exc:
        logger.warning("GH_TOKEN_REVOKE_FAILED error=%s — proceeding with DDB delete", exc)


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=google_home_unlink request_id=%s", request_id)

    if _missing_vars:
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ────────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if not auth:
        return _resp(401, {"error": "Unauthorized"})
    token = auth.split(" ")[-1].strip()
    try:
        user_id = _decode_jwt_sub(token)
        logger.info("AUTH_OK userId=%s request_id=%s", user_id, request_id)
    except Exception as exc:
        logger.warning("AUTH_FAILED reason=%s request_id=%s", exc, request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Fetch token record ──────────────────────────────────────────────────
    try:
        tok_resp = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        record = tok_resp.get("Item")
    except ClientError as exc:
        logger.error("DDB_GET_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _resp(500, {"error": "Internal error"})

    if not record:
        logger.info("UNLINK_NO_RECORD userId=%s — already unlinked request_id=%s",
                    user_id, request_id)
        _audit("GH_UNLINK_NO_RECORD", userId=user_id, request_id=request_id)
        return _resp(200, {"linked": False})

    # ── 3. Best-effort: revoke access token with Google ────────────────────────
    access_token = record.get("accessToken", "")
    if access_token:
        logger.debug("GH_TOKEN_REVOKE_START userId=%s request_id=%s", user_id, request_id)
        _revoke_google_token(access_token)
    else:
        logger.debug("GH_NO_ACCESS_TOKEN_TO_REVOKE userId=%s request_id=%s",
                     user_id, request_id)

    # ── 4. Delete token record from DDB ───────────────────────────────────────
    try:
        _ddb().Table(_TOKENS_TABLE).delete_item(Key={"userId": user_id})
        logger.info("DDB_DELETE_OK userId=%s request_id=%s", user_id, request_id)
    except ClientError as exc:
        logger.error("DDB_DELETE_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _resp(500, {"error": "Failed to unlink account"})

    _audit("GH_UNLINK_COMPLETE", userId=user_id, outcome="success", request_id=request_id)
    logger.info("REQUEST_OK function=google_home_unlink userId=%s request_id=%s",
                user_id, request_id)

    return _resp(200, {"linked": False})


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
