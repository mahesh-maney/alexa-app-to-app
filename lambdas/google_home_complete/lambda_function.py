"""
google_home_complete — POST /api/v1/voice/google-home/account-linking/deep-link/complete

Called by the Flutter app after the user returns from Google Home / web OAuth.
Verifies the session state matches the authenticated user, then returns the
current linking status from the tokens table.

Body: { "state": "<uuid>" }
Returns: { "linked": bool, "agentId": "...", "linkedAt": "..." }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

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
_REGION         = os.environ.get("DATA_REGION", "")
_SESSIONS_TABLE = os.environ.get("GH_SESSIONS_TABLE", "google_home_link_sessions")
_TOKENS_TABLE   = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_INIT region=%s (cold start)", _REGION)
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
    logger.info("REQUEST_START function=google_home_complete request_id=%s", request_id)

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

    # ── 2. Parse body ──────────────────────────────────────────────────────────
    try:
        body_raw = event.get("body") or "{}"
        body = json.loads(body_raw)
    except Exception:
        return _resp(400, {"error": "Invalid JSON body"})

    state = (body.get("state") or "").strip()
    if not state:
        logger.warning("MISSING_STATE userId=%s request_id=%s", user_id, request_id)
        return _resp(400, {"error": "state is required"})

    logger.debug("COMPLETE_PARAMS userId=%s state=%s request_id=%s",
                 user_id, state, request_id)

    # ── 3. Verify session state (CSRF protection) ──────────────────────────────
    try:
        sess_resp = _ddb().Table(_SESSIONS_TABLE).get_item(Key={"state": state})
        session = sess_resp.get("Item")
    except ClientError as exc:
        logger.error("DDB_GET_SESSION_ERROR error=%s request_id=%s", exc, request_id)
        return _resp(500, {"error": "Internal error"})

    if not session:
        logger.warning("SESSION_NOT_FOUND state=%s userId=%s request_id=%s",
                       state, user_id, request_id)
        _audit("GH_COMPLETE_SESSION_NOT_FOUND", userId=user_id, state=state,
               request_id=request_id)
        return _resp(400, {"error": "Invalid or expired session state"})

    # Verify state belongs to this user
    session_user_id = session.get("userId", "")
    if session_user_id != user_id:
        logger.warning("SESSION_OWNER_MISMATCH expected=%s got=%s state=%s request_id=%s",
                       session_user_id, user_id, state, request_id)
        _audit("GH_SESSION_OWNER_MISMATCH", expected=session_user_id, got=user_id,
               state=state, request_id=request_id)
        return _resp(400, {"error": "Invalid session state"})

    # Check TTL — DDB TTL deletion is eventually consistent; check manually
    now = int(time.time())
    if session.get("ttl", 0) < now:
        logger.warning("SESSION_EXPIRED state=%s userId=%s request_id=%s",
                       state, user_id, request_id)
        return _resp(400, {"error": "Session expired — start a new linking flow"})

    logger.debug("SESSION_VALID state=%s userId=%s request_id=%s",
                 state, user_id, request_id)

    # ── 4. Check if Google Home linking completed (token record exists) ─────────
    try:
        tok_resp = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        token_record = tok_resp.get("Item")
    except ClientError as exc:
        logger.error("DDB_GET_TOKENS_ERROR error=%s request_id=%s", exc, request_id)
        return _resp(500, {"error": "Internal error"})

    linked = bool(token_record)
    agent_id = (token_record or {}).get("agentId", "")
    linked_at = (token_record or {}).get("linkedAt", "")

    _audit("GH_COMPLETE", userId=user_id, state=state, linked=linked,
           request_id=request_id)
    logger.info("REQUEST_OK function=google_home_complete userId=%s linked=%s request_id=%s",
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
