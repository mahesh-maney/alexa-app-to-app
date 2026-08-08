"""
google_home_status — GET /api/v1/voice/google-home/account-linking

Returns the current Google Home account linking status for the authenticated user.

Response: { "linked": bool, "agentId": "...", "linkedAt": "..." }
"""
from __future__ import annotations

import base64
import json
import logging
import os

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
_REGION       = os.environ.get("DATA_REGION", "")
_TOKENS_TABLE = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_AGENT_ID     = os.environ.get("GOOGLE_AGENT_ID", "")

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


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


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=google_home_status request_id=%s", request_id)

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

    # ── 2. Look up token record ────────────────────────────────────────────────
    try:
        tok_resp = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        record = tok_resp.get("Item")
        logger.debug("DDB_GET_OK userId=%s found=%s request_id=%s",
                     user_id, bool(record), request_id)
    except ClientError as exc:
        logger.error("DDB_GET_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _resp(500, {"error": "Internal error"})

    linked = bool(record)
    agent_id = (record or {}).get("agentId", _AGENT_ID)
    linked_at = (record or {}).get("linkedAt", "")

    _audit("GH_STATUS_CHECK", userId=user_id, linked=linked, request_id=request_id)
    logger.info("REQUEST_OK function=google_home_status userId=%s linked=%s request_id=%s",
                user_id, linked, request_id)

    response: dict = {"linked": linked}
    if agent_id:
        response["agentId"] = agent_id
    if linked_at:
        response["linkedAt"] = linked_at

    return _resp(200, response)


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }
