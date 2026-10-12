"""
google_home_oauth_authorize — GET /google-home/oauth/authorize

Steps 2–6 of the Google Home account linking diagram:

  2. Google Home sends GET /oauth/authorize (client_id, redirect_uri, scope, state)
  3. API Gateway forwards to this Lambda
  4. Validate client_id, redirect_uri, response_type=code, state
  5. Store OAuth transaction securely in DynamoDB (TTL: 10 min)
  6. Redirect user to Cognito Hosted UI (/oauth2/authorize)

After step 6 the user signs in with DIGILUX credentials on the Cognito Hosted UI
(steps 7–8). Cognito then redirects to google_home_oauth_cognito_callback for
steps 9–19.
"""
from __future__ import annotations

import html
import json
import logging
import os
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
_REGION               = os.environ.get("DATA_REGION", "")
_GH_CLIENT_ID         = os.environ.get("GOOGLE_CLIENT_ID", "")
_ALLOWED_REDIRECTS    = set(
    u.strip() for u in os.environ.get("ALLOWED_REDIRECT_URIS", "").split(",") if u.strip()
)
_COGNITO_CLIENT_ID    = os.environ.get("COGNITO_CLIENT_ID", "")
# Custom domain for Cognito Hosted UI, e.g. auth.digilux.co.in
_COGNITO_DOMAIN       = os.environ.get("COGNITO_HOSTED_UI_DOMAIN", "")
# The URL Cognito will redirect back to after login (must match Cognito allowed callback URLs)
_COGNITO_CALLBACK_URI = os.environ.get("COGNITO_CALLBACK_URI", "")
_TRANSACTIONS_TABLE   = os.environ.get("GH_OAUTH_TRANSACTIONS_TABLE", "google_home_oauth_transactions")
_TRANSACTION_TTL_S    = int(os.environ.get("TRANSACTION_TTL_SECONDS", "600"))
_APP_NAME             = os.environ.get("APP_NAME", "Digilux Smart Home")

_REQUIRED_VARS = {
    "DATA_REGION":              _REGION,
    "GOOGLE_CLIENT_ID":         _GH_CLIENT_ID,
    "COGNITO_CLIENT_ID":        _COGNITO_CLIENT_ID,
    "COGNITO_HOSTED_UI_DOMAIN": _COGNITO_DOMAIN,
    "COGNITO_CALLBACK_URI":     _COGNITO_CALLBACK_URI,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_oauth_authorize "
        "— all invocations will return HTTP 500 until these are set",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_oauth_authorize region=%s cognito_domain=%s "
        "cognito_callback_uri=%s transactions_table=%s ttl_seconds=%d "
        "allowed_redirect_uris_count=%d",
        _REGION, _COGNITO_DOMAIN, _COGNITO_CALLBACK_URI,
        _TRANSACTIONS_TABLE, _TRANSACTION_TTL_S, len(_ALLOWED_REDIRECTS),
    )

if not _ALLOWED_REDIRECTS:
    logger.warning(
        "CONFIG_WARNING ALLOWED_REDIRECT_URIS not set — all redirect_uri values accepted"
    )

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        logger.debug("DDB_CLIENT_INIT region=%s (cold start)", _REGION)
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        logger.info("DDB_CLIENT_READY region=%s", _REGION)
    return _dynamodb


# ── Validation helpers ─────────────────────────────────────────────────────────

def _validate_client_id(client_id: str) -> bool:
    if not _GH_CLIENT_ID:
        logger.warning("GOOGLE_CLIENT_ID not configured — skipping client_id check")
        return True
    return client_id == _GH_CLIENT_ID


def _validate_redirect_uri(redirect_uri: str) -> bool:
    if not redirect_uri:
        return False
    if _ALLOWED_REDIRECTS and redirect_uri not in _ALLOWED_REDIRECTS:
        logger.warning(
            "INVALID_REDIRECT_URI uri=%s allowed=%s", redirect_uri, _ALLOWED_REDIRECTS
        )
        return False
    return True


# ── Step 5: Store OAuth transaction ───────────────────────────────────────────

def _store_transaction(state: str, client_id: str, redirect_uri: str, scope: str) -> None:
    """
    Persist the in-progress OAuth transaction keyed by Google's state param.
    Retrieved later in the Cognito callback to issue the final auth code.
    """
    now = int(time.time())
    _ddb().Table(_TRANSACTIONS_TABLE).put_item(Item={
        "state":        state,
        "client_id":    client_id,
        "redirect_uri": redirect_uri,
        "scope":        scope,
        "created_at":   now,
        "ttl":          now + _TRANSACTION_TTL_S,
    })
    logger.info(
        "OAUTH_TRANSACTION_STORED state=%s... client_id=%s ttl=%d",
        state[:8], client_id, _TRANSACTION_TTL_S,
    )


# ── Step 6: Build Cognito Hosted UI URL ───────────────────────────────────────

def _build_cognito_authorize_url(state: str) -> str:
    """
    Build the Cognito Hosted UI authorization URL.
    We pass Google's state through as Cognito's state so the callback Lambda
    can retrieve the stored transaction.
    """
    params = {
        "response_type": "code",
        "client_id":     _COGNITO_CLIENT_ID,
        "redirect_uri":  _COGNITO_CALLBACK_URI,
        "state":         state,
        "scope":         "openid email profile",
    }
    url = f"https://{_COGNITO_DOMAIN}/oauth2/authorize?{urllib.parse.urlencode(params)}"
    logger.debug("COGNITO_AUTHORIZE_URL %s", url[:120])
    return url


# ── Response helpers ───────────────────────────────────────────────────────────

def _redirect_error(redirect_uri: str, state: str, error: str) -> dict:
    """Redirect back to Google with an error — used when redirect_uri is valid."""
    params: dict[str, str] = {"error": error}
    if state:
        params["state"] = state
    location = f"{redirect_uri}?{urllib.parse.urlencode(params)}"
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _html_resp(status_code: int, body: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body,
    }


def _error_page(message: str) -> str:
    """Render a plain error page for cases where we cannot redirect to Google."""
    safe_msg = html.escape(message)
    safe_app = html.escape(_APP_NAME)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Error — {safe_app}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
           max-width: 480px; margin: 60px auto; padding: 0 24px; }}
    h1 {{ color: #d32f2f; font-size: 22px; }}
    p {{ color: #555; margin-top: 12px; line-height: 1.6; }}
  </style>
</head>
<body>
  <h1>Account linking failed</h1>
  <p>{safe_msg}</p>
  <p>Please return to the Google Home app and try again.</p>
</body>
</html>"""


# ── Main handler ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method     = (event.get("httpMethod") or "GET").upper()
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_oauth_authorize method=%s "
        "source_ip=%s request_id=%s",
        method, source_ip, request_id,
    )

    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        _audit("GH_AUTHORIZE_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _html_resp(500, _error_page("Server configuration error. Please try again later."))

    if method != "GET":
        return _html_resp(405, _error_page("Method not allowed."))

    # ── Parse query params ─────────────────────────────────────────────────────
    params        = event.get("queryStringParameters") or {}
    client_id     = params.get("client_id",     "").strip()
    redirect_uri  = params.get("redirect_uri",  "").strip()
    response_type = params.get("response_type", "").strip()
    state         = params.get("state",         "").strip()
    scope         = params.get("scope",         "profile").strip()

    logger.info(
        "AUTHORIZE_GET client_id=%s redirect_uri=%s response_type=%s "
        "state_present=%s request_id=%s",
        client_id, redirect_uri, response_type, bool(state), request_id,
    )

    # ── Step 4: Validate params ────────────────────────────────────────────────

    # client_id must match — error page (not redirect) since redirect_uri may also be invalid
    if not _validate_client_id(client_id):
        logger.warning("INVALID_CLIENT_ID client_id=%s request_id=%s", client_id, request_id)
        _audit("GH_AUTHORIZE_INVALID_CLIENT", client_id=client_id, request_id=request_id)
        return _html_resp(400, _error_page(
            "Invalid client ID. This account linking request cannot be completed."
        ))

    # redirect_uri must be valid before we can use it for error redirects
    if not _validate_redirect_uri(redirect_uri):
        logger.warning(
            "INVALID_REDIRECT_URI redirect_uri=%s request_id=%s", redirect_uri, request_id
        )
        _audit("GH_AUTHORIZE_INVALID_REDIRECT", redirect_uri=redirect_uri, request_id=request_id)
        return _html_resp(400, _error_page(
            "Invalid redirect URI. This account linking request cannot be completed."
        ))

    # From here redirect_uri is trusted — use it for error redirects
    if response_type != "code":
        logger.warning(
            "UNSUPPORTED_RESPONSE_TYPE response_type=%s request_id=%s", response_type, request_id
        )
        return _redirect_error(redirect_uri, state, "unsupported_response_type")

    if not state:
        logger.warning("MISSING_STATE request_id=%s", request_id)
        return _redirect_error(redirect_uri, state, "invalid_request")

    # ── Step 5: Store OAuth transaction in DynamoDB ────────────────────────────
    try:
        _store_transaction(state, client_id, redirect_uri, scope)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.error(
            "TRANSACTION_STORE_ERROR error_code=%s error=%s request_id=%s",
            error_code, exc, request_id,
        )
        _audit("GH_AUTHORIZE_DDB_ERROR", error_code=error_code, request_id=request_id)
        return _redirect_error(redirect_uri, state, "server_error")

    _audit(
        "GH_AUTHORIZE_TRANSACTION_STORED",
        state_prefix=state[:8], client_id=client_id,
        redirect_uri=redirect_uri, request_id=request_id,
    )

    # ── Step 6: Redirect to Cognito Hosted UI ─────────────────────────────────
    cognito_url = _build_cognito_authorize_url(state)
    logger.info(
        "REDIRECTING_TO_COGNITO state=%s... cognito_url=%s request_id=%s",
        state[:8], cognito_url[:100], request_id,
    )
    _audit("GH_AUTHORIZE_COGNITO_REDIRECT", state_prefix=state[:8], request_id=request_id)

    return {"statusCode": 302, "headers": {"Location": cognito_url}, "body": ""}
