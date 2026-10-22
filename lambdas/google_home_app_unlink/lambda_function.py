"""
google_home_app_unlink — DELETE /api/v1/voice/google-home/account-linking

Unlinks the Google Home account for the authenticated user:
  1. Authenticates the caller via Cognito JWT
  2. Fetches the token record from DDB (idempotent — returns 200 if already unlinked)
  3. Best-effort: calls Google Home Graph API deleteAgentUser so the GH app
     immediately reflects the unlinked state (requires GOOGLE_SA_KEY_SECRET_ARN;
     gracefully skipped when not configured — GH will sync within ~1 h on token expiry)
  4. Deletes the token record from DDB

Response: { "linked": false }
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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
_TOKENS_TABLE        = os.environ.get("GH_TOKENS_TABLE", "google_home_tokens")
# Service account JSON key secret — enables immediate GH app sync via Home Graph API
_GH_SA_KEY_SECRET_ARN = os.environ.get("GOOGLE_SA_KEY_SECRET_ARN", "")
_GH_SECRET_REGION    = os.environ.get("GOOGLE_SECRET_REGION", "")
_HTTP_TIMEOUT        = int(os.environ.get("HTTP_TIMEOUT", "10"))

_REQUIRED_VARS = {"DATA_REGION": _REGION}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_app_unlink",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_app_unlink region=%s tokens_table=%s "
        "home_graph_unlink_configured=%s http_timeout=%d",
        _REGION, _TOKENS_TABLE, bool(_GH_SA_KEY_SECRET_ARN), _HTTP_TIMEOUT,
    )

if not _GH_SA_KEY_SECRET_ARN:
    logger.warning(
        "CONFIG_WARNING GOOGLE_SA_KEY_SECRET_ARN not set — "
        "Google Home app will not immediately reflect unlink; "
        "it will sync within ~1 hour when the issued access token expires. "
        "To fix: create a Google service account with homegraph.agentUsers.delete, "
        "store the JSON key in Secrets Manager, set GOOGLE_SA_KEY_SECRET_ARN."
    )

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None
_sm_cache = None
_sa_creds_cache: dict | None = None


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
        region = _GH_SECRET_REGION or _REGION
        logger.debug("SM_CLIENT_INIT region=%s (cold start)", region)
        _sm_cache = boto3.client("secretsmanager", region_name=region)
        logger.info("SM_CLIENT_READY region=%s", region)
    else:
        logger.debug("SM_CLIENT_REUSE (warm container)")
    return _sm_cache


def _get_sa_credentials() -> dict:
    """
    Fetch and cache the Google service account JSON key from Secrets Manager.
    Expected secret format: Google service account JSON key file
    (must contain 'client_email' and 'private_key').
    Returns {} when not configured or on error.
    """
    global _sa_creds_cache
    if _sa_creds_cache is not None:
        logger.debug("SA_CREDS_CACHED")
        return _sa_creds_cache
    if not _GH_SA_KEY_SECRET_ARN:
        logger.debug("SA_CREDS_SKIP reason=GOOGLE_SA_KEY_SECRET_ARN_not_set")
        return {}
    logger.debug("SA_CREDS_FETCH arn=%s", _GH_SA_KEY_SECRET_ARN)
    try:
        resp  = _sm().get_secret_value(SecretId=_GH_SA_KEY_SECRET_ARN)
        creds = json.loads(resp["SecretString"])
        if "client_email" not in creds or "private_key" not in creds:
            logger.error(
                "SA_CREDS_INVALID arn=%s — secret must contain client_email and private_key",
                _GH_SA_KEY_SECRET_ARN,
            )
            return {}
        _sa_creds_cache = creds
        logger.info(
            "SA_CREDS_LOADED arn=%s email=%s",
            _GH_SA_KEY_SECRET_ARN, creds["client_email"],
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "SA_CREDS_LOAD_ERROR arn=%s error_code=%s error=%s",
            _GH_SA_KEY_SECRET_ARN, error_code, exc,
        )
        return {}
    except Exception as exc:
        logger.error(
            "SA_CREDS_LOAD_UNEXPECTED_ERROR arn=%s error=%s",
            _GH_SA_KEY_SECRET_ARN, exc,
        )
        return {}
    return _sa_creds_cache


def _build_sa_jwt(sa_email: str, private_key_pem: str) -> str:
    """
    Build a signed RS256 JWT assertion for Google service account authentication.
    Scope: https://www.googleapis.com/auth/homegraph
    Requires the 'cryptography' package (included in lambda deployment).
    """
    from cryptography.hazmat.backends import default_backend  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

    now = int(time.time())

    def _b64(data: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    header  = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss":   sa_email,
        "scope": "https://www.googleapis.com/auth/homegraph",
        "aud":   "https://oauth2.googleapis.com/token",
        "exp":   now + 3600,
        "iat":   now,
    }

    signing_input = f"{_b64(header)}.{_b64(payload)}".encode()

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None, backend=default_backend()
    )
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    sig_b64   = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{signing_input.decode()}.{sig_b64}"


def _get_sa_access_token(sa_email: str, private_key_pem: str) -> str:
    """Exchange a service account JWT assertion for a short-lived Google OAuth access token."""
    assertion = _build_sa_jwt(sa_email, private_key_pem)
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion":  assertion,
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        token_data = json.loads(resp.read().decode())
    if "access_token" not in token_data:
        raise ValueError(f"No access_token in SA token response: {list(token_data.keys())}")
    return token_data["access_token"]


def _delete_agent_user(user_id: str) -> None:
    """
    Best-effort: call the Google Home Graph API deleteAgentUser endpoint so that
    the Google Home app immediately shows the account as unlinked.

    Without this call Google's servers retain the link until our issued access token
    expires (~1 h), at which point Google will try to refresh it, get invalid_grant,
    and mark the account as disconnected.

    API reference:
      DELETE https://homegraph.googleapis.com/v1/agentUsers/{agentUserId}
      Auth: Bearer <service-account-access-token>
      Scope: https://www.googleapis.com/auth/homegraph

    Errors are logged but do NOT block the unlink operation.
    """
    sa_creds = _get_sa_credentials()
    if not sa_creds:
        logger.info(
            "GH_DELETE_AGENT_USER_SKIP userId=%s reason=no_service_account_configured",
            user_id,
        )
        _audit("GH_DELETE_AGENT_USER_SKIP", userId=user_id, reason="no_sa_configured")
        return

    try:
        access_token = _get_sa_access_token(sa_creds["client_email"], sa_creds["private_key"])
        logger.debug("SA_ACCESS_TOKEN_OK email=%s", sa_creds["client_email"])
    except Exception as exc:
        logger.warning(
            "GH_DELETE_AGENT_USER_SA_TOKEN_ERROR userId=%s error=%s "
            "— proceeding with DDB delete only",
            user_id, exc,
        )
        _audit("GH_DELETE_AGENT_USER_SA_TOKEN_ERROR", userId=user_id, error=str(exc))
        return

    request_id   = str(uuid.uuid4())
    encoded_user = urllib.parse.quote(user_id, safe="")
    url = (
        f"https://homegraph.googleapis.com/v1/agentUsers/{encoded_user}"
        f"?requestId={request_id}"
    )
    logger.debug(
        "GH_DELETE_AGENT_USER_START userId=%s request_id=%s",
        user_id, request_id,
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            status = resp.status
            logger.info(
                "GH_DELETE_AGENT_USER_OK userId=%s http_status=%d request_id=%s",
                user_id, status, request_id,
            )
            _audit("GH_DELETE_AGENT_USER_OK", userId=user_id,
                   http_status=status, request_id=request_id)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:200] if exc.fp else ""
        logger.warning(
            "GH_DELETE_AGENT_USER_HTTP_ERROR status=%d reason=%s body=%s "
            "userId=%s request_id=%s — proceeding with DDB delete",
            exc.code, exc.reason, body, user_id, request_id,
        )
        _audit("GH_DELETE_AGENT_USER_HTTP_ERROR", userId=user_id,
               http_status=exc.code, reason=str(exc.reason), request_id=request_id)
    except urllib.error.URLError as exc:
        logger.warning(
            "GH_DELETE_AGENT_USER_URL_ERROR reason=%s userId=%s "
            "— proceeding with DDB delete",
            exc.reason, user_id,
        )
        _audit("GH_DELETE_AGENT_USER_URL_ERROR", userId=user_id, reason=str(exc.reason))
    except Exception as exc:
        logger.warning(
            "GH_DELETE_AGENT_USER_UNEXPECTED_ERROR error=%s userId=%s "
            "— proceeding with DDB delete",
            exc, user_id,
        )
        _audit("GH_DELETE_AGENT_USER_UNEXPECTED_ERROR", userId=user_id, error=str(exc))


def _decode_jwt_sub(token: str) -> str:
    logger.debug("JWT_DECODE_START token_len=%d", len(token))
    parts = token.split(".")
    if len(parts) != 3:
        logger.debug("JWT_INVALID_FORMAT part_count=%d", len(parts))
        raise ValueError(f"JWT has {len(parts)} parts, expected 3")
    try:
        padding_chars = "=" * (4 - len(parts[1]) % 4)
        claims        = json.loads(base64.urlsafe_b64decode(parts[1] + padding_chars))
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
    method     = (event.get("httpMethod") or "DELETE").upper()
    path       = event.get("path", "")
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_app_unlink method=%s path=%s "
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
            "AUTH_MISSING function=google_home_app_unlink source_ip=%s request_id=%s",
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
            "AUTH_OK function=google_home_app_unlink userId=%s request_id=%s",
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

    # ── 3. Best-effort: notify Google Home Graph API to immediately unlink ─────
    logger.info(
        "GH_DELETE_AGENT_USER_INITIATED userId=%s request_id=%s",
        user_id, request_id,
    )
    _audit("GH_UNLINK_DELETE_AGENT_USER_START", userId=user_id, request_id=request_id)
    try:
        _delete_agent_user(user_id)
    except Exception as exc:
        # Best-effort — never block the DDB delete
        logger.warning(
            "GH_DELETE_AGENT_USER_OUTER_ERROR error=%s userId=%s "
            "— proceeding with DDB delete",
            exc, user_id,
        )

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
        "REQUEST_OK function=google_home_app_unlink userId=%s request_id=%s",
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
