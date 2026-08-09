"""
google_home_unlink — DELETE /api/v1/voice/google-home/account-linking

Unlinks the Google Home account for the authenticated user:
  1. Authenticates the caller via Cognito JWT
  2. Fetches the token record from DDB (idempotent — returns 200 if already unlinked)
  3. Best-effort: revokes the access token with Google
  4. Deletes the token record from DDB

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
_REGION           = os.environ.get("DATA_REGION", "")
_TOKENS_TABLE     = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
_GH_SECRET_ARN    = os.environ.get("GOOGLE_CLIENT_SECRET_ARN", "")
_GH_SECRET_REGION = os.environ.get("GOOGLE_SECRET_REGION", "ap-south-1")
_HTTP_TIMEOUT     = int(os.environ.get("HTTP_TIMEOUT", "10"))

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_unlink",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_unlink region=%s tokens_table=%s "
        "token_revocation_configured=%s http_timeout=%d",
        _REGION, _TOKENS_TABLE, bool(_GH_SECRET_ARN), _HTTP_TIMEOUT,
    )

if not _GH_SECRET_ARN:
    logger.warning(
        "CONFIG_WARNING GOOGLE_CLIENT_SECRET_ARN not set — "
        "token revocation with Google will be skipped on unlink"
    )

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None
_sm_cache = None
_gh_secret_cache: dict | None = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_CLIENT_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        logger.info("DDB_CLIENT_READY region=%s", _REGION)
    else:
        logger.debug("DDB_CLIENT_REUSE (warm container)")
    return _dynamodb


def _sm():
    global _sm_cache
    if _sm_cache is None:
        logger.debug("SM_CLIENT_INIT region=%s (cold start)", _GH_SECRET_REGION)
        _sm_cache = boto3.client("secretsmanager", region_name=_GH_SECRET_REGION)
        logger.info("SM_CLIENT_READY region=%s", _GH_SECRET_REGION)
    else:
        logger.debug("SM_CLIENT_REUSE (warm container)")
    return _sm_cache


def _get_gh_secret() -> dict:
    """Fetch and cache Google OAuth credentials from Secrets Manager."""
    global _gh_secret_cache
    if _gh_secret_cache is not None:
        logger.debug("GH_SECRET_CACHED")
        return _gh_secret_cache
    if not _GH_SECRET_ARN:
        logger.warning("GH_SECRET_SKIP reason=GOOGLE_CLIENT_SECRET_ARN_not_set")
        return {}
    logger.debug("GH_SECRET_FETCH arn=%s", _GH_SECRET_ARN)
    try:
        resp = _sm().get_secret_value(SecretId=_GH_SECRET_ARN)
        _gh_secret_cache = json.loads(resp["SecretString"])
        logger.info(
            "GH_SECRET_LOADED arn=%s fields=%s",
            _GH_SECRET_ARN, list(_gh_secret_cache.keys()),
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "GH_SECRET_LOAD_ERROR arn=%s error_code=%s error=%s",
            _GH_SECRET_ARN, error_code, exc,
        )
        return {}
    except Exception as exc:
        logger.error("GH_SECRET_LOAD_UNEXPECTED_ERROR arn=%s error=%s", _GH_SECRET_ARN, exc)
        return {}
    return _gh_secret_cache


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


def _revoke_google_token(access_token: str, user_id: str) -> None:
    """
    Best-effort: revoke the access token with Google.
    Endpoint: POST https://oauth2.googleapis.com/revoke
    Errors are logged but do NOT block the unlink operation.
    """
    url = "https://oauth2.googleapis.com/revoke"
    logger.debug(
        "GH_TOKEN_REVOKE_START url=%s token_prefix=%s... userId=%s",
        url, access_token[:8], user_id,
    )
    try:
        data = urllib.parse.urlencode({"token": access_token}).encode()
        req  = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            status = resp.status
            body   = resp.read().decode()[:200]
            logger.info(
                "GH_TOKEN_REVOKE_OK status=%d body=%s userId=%s",
                status, body, user_id,
            )
            _audit("GH_TOKEN_REVOKE_OK", userId=user_id, http_status=status)
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode()[:200] if exc.fp else ""
        logger.warning(
            "GH_TOKEN_REVOKE_HTTP_ERROR status=%d reason=%s body=%s "
            "userId=%s — proceeding with DDB delete",
            exc.code, exc.reason, response_body, user_id,
        )
        _audit("GH_TOKEN_REVOKE_HTTP_ERROR", userId=user_id,
               http_status=exc.code, reason=str(exc.reason))
    except urllib.error.URLError as exc:
        logger.warning(
            "GH_TOKEN_REVOKE_URL_ERROR reason=%s userId=%s — proceeding with DDB delete",
            exc.reason, user_id,
        )
        _audit("GH_TOKEN_REVOKE_URL_ERROR", userId=user_id, reason=str(exc.reason))
    except Exception as exc:
        logger.warning(
            "GH_TOKEN_REVOKE_UNEXPECTED_ERROR error=%s userId=%s — proceeding with DDB delete",
            exc, user_id,
        )
        _audit("GH_TOKEN_REVOKE_UNEXPECTED_ERROR", userId=user_id, error=str(exc))


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method     = (event.get("httpMethod") or "DELETE").upper()
    path       = event.get("path", "")
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_unlink method=%s path=%s "
        "source_ip=%s request_id=%s",
        method, path, source_ip, request_id,
    )

    # ── 0. Config guard ────────────────────────────────────────────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        _audit("GH_UNLINK_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Authenticate ────────────────────────────────────────────────────────
    headers = event.get("headers") or {}
    auth    = headers.get("Authorization") or headers.get("authorization") or ""
    logger.debug("AUTH_HEADER_CHECK present=%s request_id=%s", bool(auth), request_id)

    if not auth:
        logger.warning(
            "AUTH_MISSING function=google_home_unlink source_ip=%s request_id=%s",
            source_ip, request_id,
        )
        _audit("GH_UNLINK_AUTH_MISSING", source_ip=source_ip, request_id=request_id)
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
            "AUTH_OK function=google_home_unlink userId=%s request_id=%s",
            user_id, request_id,
        )
        _audit("GH_UNLINK_AUTH_OK", userId=user_id, request_id=request_id)
    except Exception as exc:
        logger.warning(
            "AUTH_FAILED reason=%s scheme=%s source_ip=%s request_id=%s",
            exc, scheme, source_ip, request_id,
        )
        _audit("GH_UNLINK_AUTH_FAILED", reason=str(exc), source_ip=source_ip,
               request_id=request_id)
        return _resp(401, {"error": "Unauthorized"})

    # ── 2. Fetch token record ──────────────────────────────────────────────────
    logger.debug(
        "TOKEN_LOOKUP_START table=%s userId=%s request_id=%s",
        _TOKENS_TABLE, user_id, request_id,
    )
    try:
        tok_resp = _ddb().Table(_TOKENS_TABLE).get_item(Key={"userId": user_id})
        record   = tok_resp.get("Item")
        logger.debug(
            "TOKEN_LOOKUP_OK table=%s userId=%s found=%s request_id=%s",
            _TOKENS_TABLE, user_id, bool(record), request_id,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_GET_ERROR table=%s error_code=%s error=%s userId=%s request_id=%s",
            _TOKENS_TABLE, error_code, exc, user_id, request_id,
        )
        _audit("GH_UNLINK_DDB_GET_ERROR", userId=user_id, error_code=error_code,
               request_id=request_id)
        return _resp(500, {"error": "Internal error"})
    except Exception as exc:
        logger.error(
            "DDB_GET_UNEXPECTED_ERROR error=%s userId=%s request_id=%s",
            exc, user_id, request_id,
        )
        _audit("GH_UNLINK_DDB_GET_ERROR", userId=user_id, error="unexpected",
               request_id=request_id)
        return _resp(500, {"error": "Internal error"})

    # Idempotent: already unlinked
    if not record:
        logger.info(
            "UNLINK_ALREADY_UNLINKED userId=%s — no token record found request_id=%s",
            user_id, request_id,
        )
        _audit("GH_UNLINK_ALREADY_UNLINKED", userId=user_id, request_id=request_id)
        return _resp(200, {"linked": False})

    agent_id  = record.get("agentId", "")
    linked_at = record.get("linkedAt", "")
    scope     = record.get("scope", "")
    logger.info(
        "TOKEN_RECORD_FOUND userId=%s agent_id=%s linked_at=%s scope=%s request_id=%s",
        user_id, agent_id, linked_at, scope, request_id,
    )

    # ── 3. Best-effort: revoke access token with Google ────────────────────────
    access_token = record.get("accessToken", "")
    if access_token:
        logger.info(
            "GH_TOKEN_REVOKE_INITIATED userId=%s token_prefix=%s... request_id=%s",
            user_id, access_token[:8], request_id,
        )
        _audit("GH_UNLINK_REVOKE_START", userId=user_id, token_prefix=access_token[:8],
               request_id=request_id)
        _revoke_google_token(access_token, user_id)
    else:
        logger.info(
            "GH_TOKEN_REVOKE_SKIP userId=%s reason=no_access_token_in_record request_id=%s",
            user_id, request_id,
        )
        _audit("GH_UNLINK_REVOKE_SKIP", userId=user_id, reason="no_access_token",
               request_id=request_id)

    # ── 4. Delete token record from DDB ───────────────────────────────────────
    logger.debug(
        "DDB_DELETE_START table=%s userId=%s request_id=%s",
        _TOKENS_TABLE, user_id, request_id,
    )
    try:
        _ddb().Table(_TOKENS_TABLE).delete_item(Key={"userId": user_id})
        logger.info(
            "DDB_DELETE_OK table=%s userId=%s request_id=%s",
            _TOKENS_TABLE, user_id, request_id,
        )
        _audit("GH_UNLINK_COMPLETE", userId=user_id, agent_id=agent_id,
               outcome="success", request_id=request_id)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "DDB_DELETE_ERROR table=%s error_code=%s error=%s userId=%s request_id=%s",
            _TOKENS_TABLE, error_code, exc, user_id, request_id,
        )
        _audit("GH_UNLINK_DDB_DELETE_ERROR", userId=user_id, error_code=error_code,
               outcome="error", request_id=request_id)
        return _resp(500, {"error": "Failed to unlink account"})
    except Exception as exc:
        logger.error(
            "DDB_DELETE_UNEXPECTED_ERROR error=%s userId=%s request_id=%s",
            exc, user_id, request_id,
        )
        _audit("GH_UNLINK_DDB_DELETE_ERROR", userId=user_id, error="unexpected",
               outcome="error", request_id=request_id)
        return _resp(500, {"error": "Failed to unlink account"})

    logger.info(
        "REQUEST_OK function=google_home_unlink userId=%s request_id=%s",
        user_id, request_id,
    )
    return _resp(200, {"linked": False})


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
