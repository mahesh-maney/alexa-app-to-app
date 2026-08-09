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

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_status "
        "— all invocations will return HTTP 500",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_status region=%s tokens_table=%s agent_id=%s",
        _REGION, _TOKENS_TABLE, _AGENT_ID,
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
        logger.debug("JWT_INVALID_FORMAT part_count=%d expected=3", len(parts))
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
        "JWT_DECODE_OK token_use=%s iss_suffix=%s exp=%d sub_len=%d",
        claims.get("token_use", "?"),
        claims.get("iss", "")[-20:],
        claims.get("exp", 0),
        len(sub),
    )
    return sub


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method     = (event.get("httpMethod") or "GET").upper()
    path       = event.get("path", "")
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")
    query      = event.get("queryStringParameters") or {}

    logger.info(
        "REQUEST_START function=google_home_status method=%s path=%s "
        "query=%s source_ip=%s request_id=%s",
        method, path, list(query.keys()), source_ip, request_id,
    )

    # ── 0. Config guard ────────────────────────────────────────────────────────
    if _missing_vars:
        logger.error(
            "CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id,
        )
        _audit("GH_STATUS_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ────────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth    = headers.get("Authorization") or headers.get("authorization") or ""
    logger.debug("AUTH_HEADER_CHECK present=%s request_id=%s", bool(auth), request_id)

    if not auth:
        logger.warning(
            "AUTH_MISSING function=google_home_status source_ip=%s request_id=%s",
            source_ip, request_id,
        )
        _audit("GH_STATUS_AUTH_MISSING", source_ip=source_ip, request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    scheme = auth.split(" ")[0] if " " in auth else auth
    token  = auth.split(" ")[-1].strip()
    logger.debug(
        "AUTH_TOKEN_PRESENT scheme=%s token_len=%d request_id=%s",
        scheme, len(token), request_id,
    )

    try:
        user_id = _decode_jwt_sub(token)
        logger.info(
            "AUTH_OK function=google_home_status userId=%s request_id=%s",
            user_id, request_id,
        )
        _audit("GH_STATUS_AUTH_OK", userId=user_id, request_id=request_id)
    except Exception as exc:
        logger.warning(
            "AUTH_FAILED reason=%s scheme=%s source_ip=%s request_id=%s",
            exc, scheme, source_ip, request_id,
        )
        _audit("GH_STATUS_AUTH_FAILED", reason=str(exc), source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Look up token record in DDB ─────────────────────────────────────────
    logger.debug(
        "TOKEN_LOOKUP_START table=%s userId=%s request_id=%s",
        _TOKENS_TABLE, user_id, request_id,
    )
    try:
        tok_resp = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        record   = tok_resp.get("Item")
        consumed = tok_resp.get("ConsumedCapacity")
        logger.debug(
            "TOKEN_LOOKUP_OK table=%s userId=%s found=%s consumed_capacity=%s request_id=%s",
            _TOKENS_TABLE, user_id, bool(record), consumed, request_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_GET_ERROR table=%s error_code=%s error=%s "
            "userId=%s request_id=%s",
            _TOKENS_TABLE, error_code, exc, user_id, request_id,
        )
        _audit("GH_STATUS_DDB_ERROR", userId=user_id, error_code=error_code,
               request_id=request_id)
        return _resp(500, {"error": "Internal error"})
    except Exception as exc:
        logger.error(
            "DDB_GET_UNEXPECTED_ERROR error=%s userId=%s request_id=%s",
            exc, user_id, request_id,
        )
        _audit("GH_STATUS_DDB_ERROR", userId=user_id, error="unexpected",
               request_id=request_id)
        return _resp(500, {"error": "Internal error"})

    # ── 3. Build response ──────────────────────────────────────────────────────
    linked    = bool(record)
    agent_id  = (record or {}).get("agentId", _AGENT_ID)
    linked_at = (record or {}).get("linkedAt", "")
    scope     = (record or {}).get("scope", "")
    expires_at = (record or {}).get("expiresAt", 0)

    if linked:
        logger.info(
            "STATUS_LINKED userId=%s agent_id=%s linked_at=%s "
            "token_expires_at=%d scope=%s request_id=%s",
            user_id, agent_id, linked_at, expires_at, scope, request_id,
        )
    else:
        logger.info(
            "STATUS_NOT_LINKED userId=%s request_id=%s",
            user_id, request_id,
        )

    _audit("GH_STATUS_CHECK", userId=user_id, linked=linked,
           agent_id=agent_id, linked_at=linked_at,
           token_expires_at=expires_at, request_id=request_id)

    response: dict = {"linked": linked}
    if agent_id:
        response["agentId"] = agent_id
    if linked_at:
        response["linkedAt"] = linked_at

    logger.info(
        "REQUEST_OK function=google_home_status userId=%s linked=%s request_id=%s",
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
