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

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_complete "
        "— all invocations will return HTTP 500",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_complete region=%s "
        "sessions_table=%s tokens_table=%s",
        _REGION, _SESSIONS_TABLE, _TOKENS_TABLE,
    )

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_CLIENT_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        logger.info("DDB_CLIENT_READY region=%s", _REGION)
    else:
        logger.debug("DDB_CLIENT_REUSE (warm container)")
    return _dynamodb


def _decode_jwt_sub(token: str) -> str:
    logger.debug("JWT_DECODE_START token_len=%d", len(token))
    parts = token.split(".")
    if len(parts) != 3:
        logger.debug("JWT_INVALID_FORMAT part_count=%d", len(parts))
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    try:
        padding = "=" * (4 - len(parts[1]) % 4)
        claims  = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception as exc:
        logger.debug("JWT_PAYLOAD_DECODE_FAILED reason=%s", exc)
        raise ValueError(f"Could not decode JWT payload: {exc}") from exc
    sub = claims.get("sub") or claims.get("username")
    if not sub:
        logger.debug("JWT_MISSING_SUB claims_keys=%s", list(claims.keys()))
        raise ValueError("Missing sub/username claim in JWT")
    logger.debug(
        "JWT_DECODE_OK token_use=%s exp=%d sub_len=%d",
        claims.get("token_use", "?"), claims.get("exp", 0), len(sub),
    )
    return sub


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method    = (event.get("httpMethod") or "POST").upper()
    path      = event.get("path", "")
    source_ip = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_complete method=%s path=%s "
        "source_ip=%s request_id=%s",
        method, path, source_ip, request_id,
    )

    # ── 0. Config guard ────────────────────────────────────────────────────────
    if _missing_vars:
        logger.error(
            "CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id,
        )
        _audit("GH_COMPLETE_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ────────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth    = headers.get("Authorization") or headers.get("authorization") or ""
    logger.debug("AUTH_HEADER_CHECK present=%s request_id=%s", bool(auth), request_id)

    if not auth:
        logger.warning(
            "AUTH_MISSING function=google_home_complete source_ip=%s request_id=%s",
            source_ip, request_id,
        )
        _audit("GH_COMPLETE_AUTH_MISSING", source_ip=source_ip, request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    token  = auth.split(" ")[-1].strip()
    scheme = auth.split(" ")[0] if " " in auth else auth
    logger.debug("AUTH_TOKEN_PRESENT scheme=%s token_len=%d request_id=%s",
                 scheme, len(token), request_id)

    try:
        user_id = _decode_jwt_sub(token)
        logger.info(
            "AUTH_OK function=google_home_complete userId=%s request_id=%s",
            user_id, request_id,
        )
        _audit("GH_COMPLETE_AUTH_OK", userId=user_id, request_id=request_id)
    except Exception as exc:
        logger.warning(
            "AUTH_FAILED reason=%s scheme=%s source_ip=%s request_id=%s",
            exc, scheme, source_ip, request_id,
        )
        _audit("GH_COMPLETE_AUTH_FAILED", reason=str(exc), source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Parse and validate body ─────────────────────────────────────────────
    body_raw = event.get("body") or "{}"
    logger.debug("BODY_PARSE_START body_len=%d request_id=%s", len(body_raw), request_id)
    try:
        body = json.loads(body_raw)
        logger.debug("BODY_PARSE_OK keys=%s request_id=%s", list(body.keys()), request_id)
    except Exception as exc:
        logger.warning(
            "BODY_PARSE_ERROR reason=%s body_prefix=%s request_id=%s",
            exc, body_raw[:50], request_id,
        )
        _audit("GH_COMPLETE_BAD_REQUEST", userId=user_id, reason="invalid_json",
               request_id=request_id)
        return _resp(400, {"error": "Invalid JSON body"})

    state = (body.get("state") or "").strip()
    logger.debug(
        "PARAMS_EXTRACTED state=%s state_len=%d request_id=%s",
        state[:8] + "..." if state else "", len(state), request_id,
    )

    if not state:
        logger.warning(
            "MISSING_STATE userId=%s request_id=%s", user_id, request_id,
        )
        _audit("GH_COMPLETE_MISSING_STATE", userId=user_id, request_id=request_id)
        return _resp(400, {"error": "state is required"})

    # ── 3. Verify session state (CSRF protection) ──────────────────────────────
    logger.debug(
        "SESSION_LOOKUP_START table=%s state=%s... request_id=%s",
        _SESSIONS_TABLE, state[:8], request_id,
    )
    try:
        sess_resp = _ddb().Table(_SESSIONS_TABLE).get_item(Key={"state": state})
        session = sess_resp.get("Item")
        logger.debug(
            "SESSION_LOOKUP_OK table=%s state=%s... found=%s request_id=%s",
            _SESSIONS_TABLE, state[:8], bool(session), request_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_GET_SESSION_ERROR table=%s error_code=%s error=%s "
            "userId=%s state=%s... request_id=%s",
            _SESSIONS_TABLE, error_code, exc, user_id, state[:8], request_id,
        )
        _audit("GH_COMPLETE_SESSION_LOOKUP_ERROR", userId=user_id, state_prefix=state[:8],
               error_code=error_code, request_id=request_id)
        return _resp(500, {"error": "Internal error"})
    except Exception as exc:
        logger.error(
            "DDB_GET_SESSION_UNEXPECTED_ERROR error=%s userId=%s request_id=%s",
            exc, user_id, request_id,
        )
        return _resp(500, {"error": "Internal error"})

    if not session:
        logger.warning(
            "SESSION_NOT_FOUND state=%s... userId=%s request_id=%s",
            state[:8], user_id, request_id,
        )
        _audit("GH_COMPLETE_SESSION_NOT_FOUND", userId=user_id, state_prefix=state[:8],
               request_id=request_id)
        return _resp(400, {"error": "Invalid or expired session state"})

    # Verify state ownership (CSRF)
    session_user_id = session.get("userId", "")
    session_status  = session.get("status", "")
    session_created = session.get("createdAt", 0)
    session_ttl     = int(session.get("ttl", 0))

    logger.debug(
        "SESSION_FOUND state=%s... session_userId=%s session_status=%s "
        "session_created=%d session_ttl=%d request_id=%s",
        state[:8], session_user_id, session_status, session_created, session_ttl, request_id,
    )

    if session_user_id != user_id:
        logger.warning(
            "SESSION_OWNER_MISMATCH state=%s... "
            "expected_userId=%s actual_userId=%s request_id=%s",
            state[:8], session_user_id, user_id, request_id,
        )
        _audit("GH_COMPLETE_SESSION_OWNER_MISMATCH",
               expected=session_user_id, got=user_id,
               state_prefix=state[:8], request_id=request_id)
        return _resp(400, {"error": "Invalid session state"})

    # Manual TTL check (DDB TTL deletion is eventually consistent)
    now = int(time.time())
    if session_ttl and session_ttl < now:
        logger.warning(
            "SESSION_EXPIRED state=%s... userId=%s expired_at=%d now=%d "
            "expired_seconds_ago=%d request_id=%s",
            state[:8], user_id, session_ttl, now, now - session_ttl, request_id,
        )
        _audit("GH_COMPLETE_SESSION_EXPIRED", userId=user_id, state_prefix=state[:8],
               expired_at=session_ttl, now=now, request_id=request_id)
        return _resp(400, {"error": "Session expired — start a new linking flow"})

    logger.info(
        "SESSION_VALID state=%s... userId=%s age_seconds=%d request_id=%s",
        state[:8], user_id, now - session_created, request_id,
    )
    _audit("GH_COMPLETE_SESSION_VALID", userId=user_id, state_prefix=state[:8],
           session_status=session_status, request_id=request_id)

    # ── 4. Check if Google Home linking completed (token record exists) ─────────
    logger.debug(
        "TOKEN_LOOKUP_START table=%s userId=%s request_id=%s",
        _TOKENS_TABLE, user_id, request_id,
    )
    try:
        tok_resp     = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        token_record = tok_resp.get("Item")
        logger.debug(
            "TOKEN_LOOKUP_OK table=%s userId=%s found=%s request_id=%s",
            _TOKENS_TABLE, user_id, bool(token_record), request_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_GET_TOKENS_ERROR table=%s error_code=%s error=%s "
            "userId=%s request_id=%s",
            _TOKENS_TABLE, error_code, exc, user_id, request_id,
        )
        _audit("GH_COMPLETE_TOKEN_LOOKUP_ERROR", userId=user_id,
               error_code=error_code, request_id=request_id)
        return _resp(500, {"error": "Internal error"})
    except Exception as exc:
        logger.error(
            "DDB_GET_TOKENS_UNEXPECTED_ERROR error=%s userId=%s request_id=%s",
            exc, user_id, request_id,
        )
        return _resp(500, {"error": "Internal error"})

    linked    = bool(token_record)
    agent_id  = (token_record or {}).get("agentId", "")
    linked_at = (token_record or {}).get("linkedAt", "")
    scope     = (token_record or {}).get("scope", "")

    logger.info(
        "GH_LINK_STATUS userId=%s linked=%s agent_id=%s linked_at=%s request_id=%s",
        user_id, linked, agent_id, linked_at, request_id,
    )
    _audit("GH_COMPLETE", userId=user_id, state_prefix=state[:8],
           linked=linked, agent_id=agent_id, linked_at=linked_at, scope=scope,
           request_id=request_id)

    response: dict = {"linked": linked}
    if agent_id:
        response["agentId"] = agent_id
    if linked_at:
        response["linkedAt"] = linked_at

    logger.info(
        "REQUEST_OK function=google_home_complete userId=%s linked=%s request_id=%s",
        user_id, linked, request_id,
    )
    return _resp(200, response)


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
