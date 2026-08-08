"""
google_home_oauth_authorize — GET/POST /google-home/oauth/authorize

OAuth 2.0 authorization endpoint that Google Home redirects users to during
account linking. This Lambda acts as our OAuth authorization server.

GET  /oauth/authorize  — serves the HTML login page
                         If pre_auth_state is present and valid, auto-authorizes
                         (user already authenticated in Flutter app)
POST /oauth/authorize  — processes login form submission

Query params (Google sends on GET):
  client_id     : Google's client ID (must match GOOGLE_CLIENT_ID env var)
  redirect_uri  : where to send the auth code (must be in allowed list)
  response_type : must be "code"
  state         : Google's CSRF token (echoed back in redirect)
  scope         : requested scopes
  pre_auth_state: (optional) our session state from /start — skips login form

POST body (form-encoded):
  username, password, client_id, redirect_uri, state, scope, pre_auth_state

On success: 302 redirect to redirect_uri?code=AUTH_CODE&state=GOOGLE_STATE
On error:   302 redirect to redirect_uri?error=access_denied&state=GOOGLE_STATE
            OR HTML error page if redirect_uri is invalid
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import time
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
_REGION              = os.environ.get("DATA_REGION", "")
_SESSIONS_TABLE      = os.environ.get("GH_SESSIONS_TABLE", "google_home_link_sessions")
_AUTH_CODES_TABLE    = os.environ.get("GH_AUTH_CODES_TABLE", "google_home_auth_codes")
_GH_CLIENT_ID        = os.environ.get("GOOGLE_CLIENT_ID", "")
_ALLOWED_REDIRECTS   = set(
    u.strip() for u in os.environ.get("ALLOWED_REDIRECT_URIS", "").split(",") if u.strip()
)
_COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_COGNITO_REGION       = os.environ.get("COGNITO_REGION", _REGION or "ap-south-1")
_COGNITO_CLIENT_ID    = os.environ.get("COGNITO_CLIENT_ID", "")
_AUTH_CODE_TTL_S      = int(os.environ.get("AUTH_CODE_TTL_SECONDS", "300"))
_APP_NAME             = os.environ.get("APP_NAME", "Digilux Smart Home")

_REQUIRED_VARS = {
    "DATA_REGION":        _REGION,
    "GOOGLE_CLIENT_ID":   _GH_CLIENT_ID,
    "COGNITO_CLIENT_ID":  _COGNITO_CLIENT_ID,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb = None
_cognito_cache = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


def _cognito():
    global _cognito_cache
    if _cognito_cache is None:
        _cognito_cache = boto3.client("cognito-idp", region_name=_COGNITO_REGION)
    return _cognito_cache


# ── OAuth validation helpers ───────────────────────────────────────────────────

def _validate_redirect_uri(redirect_uri: str) -> bool:
    """Check that the redirect_uri is in the allowed list."""
    if not redirect_uri:
        return False
    if _ALLOWED_REDIRECTS and redirect_uri not in _ALLOWED_REDIRECTS:
        logger.warning("INVALID_REDIRECT_URI uri=%s allowed=%s", redirect_uri, _ALLOWED_REDIRECTS)
        return False
    return True


def _validate_client_id(client_id: str) -> bool:
    if not _GH_CLIENT_ID:
        logger.warning("GOOGLE_CLIENT_ID not configured — skipping client_id check")
        return True
    return client_id == _GH_CLIENT_ID


# ── Auth code generation ───────────────────────────────────────────────────────

def _generate_auth_code() -> str:
    """Generate a cryptographically random authorization code."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _store_auth_code(code: str, user_id: str, redirect_uri: str, scope: str) -> None:
    """Persist the auth code in DDB with a short TTL."""
    now = int(time.time())
    _ddb().Table(_AUTH_CODES_TABLE).put_item(Item={
        "code":        code,
        "userId":      user_id,
        "redirectUri": redirect_uri,
        "scope":       scope,
        "createdAt":   now,
        "ttl":         now + _AUTH_CODE_TTL_S,
    })
    logger.info("AUTH_CODE_STORED userId=%s code=%s... ttl=%d",
                user_id, code[:8], _AUTH_CODE_TTL_S)


# ── Cognito authentication ─────────────────────────────────────────────────────

def _authenticate_cognito(username: str, password: str) -> str | None:
    """
    Authenticate username/password against Cognito.
    Returns the Cognito sub (userId) on success, None on failure.
    """
    if not _COGNITO_CLIENT_ID:
        logger.error("COGNITO_CLIENT_ID not configured")
        return None
    try:
        resp = _cognito().initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
            ClientId=_COGNITO_CLIENT_ID,
        )
        id_token = resp.get("AuthenticationResult", {}).get("IdToken", "")
        if not id_token:
            logger.warning("COGNITO_AUTH_NO_ID_TOKEN username=%s", username)
            return None
        # Decode sub from the ID token
        parts = id_token.split(".")
        padding = "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        sub = claims.get("sub", "")
        if sub:
            logger.info("COGNITO_AUTH_OK sub=%s", sub)
        return sub or None
    except _cognito().exceptions.NotAuthorizedException:
        logger.warning("COGNITO_AUTH_INVALID_CREDENTIALS username=%s", username)
        return None
    except _cognito().exceptions.UserNotFoundException:
        logger.warning("COGNITO_AUTH_USER_NOT_FOUND username=%s", username)
        return None
    except Exception as exc:
        logger.error("COGNITO_AUTH_ERROR error=%s username=%s", exc, username)
        return None


# ── Session lookup for pre-auth flow ──────────────────────────────────────────

def _get_session_user(pre_auth_state: str) -> str | None:
    """
    Look up the userId from a pre_auth_state created by /start.
    Returns userId if valid and not expired, else None.
    """
    if not pre_auth_state:
        return None
    try:
        resp = _ddb().Table(_SESSIONS_TABLE).get_item(Key={"state": pre_auth_state})
        session = resp.get("Item")
        if not session:
            logger.warning("PRE_AUTH_SESSION_NOT_FOUND state=%s", pre_auth_state)
            return None
        if session.get("ttl", 0) < int(time.time()):
            logger.warning("PRE_AUTH_SESSION_EXPIRED state=%s", pre_auth_state)
            return None
        return session.get("userId")
    except ClientError as exc:
        logger.error("PRE_AUTH_SESSION_DDB_ERROR error=%s", exc)
        return None


# ── HTML templates ─────────────────────────────────────────────────────────────

def _login_page(client_id: str, redirect_uri: str, state: str, scope: str,
                pre_auth_state: str, error_msg: str = "") -> str:
    """Render the OAuth login form as an HTML page."""
    safe_error = f'<p class="error">{html.escape(error_msg)}</p>' if error_msg else ""
    safe_app   = html.escape(_APP_NAME)
    safe_scope = html.escape(scope or "smart home control")

    # Hidden fields to carry OAuth params through form POST
    def hidden(name: str, value: str) -> str:
        return f'<input type="hidden" name="{name}" value="{html.escape(value)}">'

    hidden_fields = "\n".join([
        hidden("client_id",     client_id),
        hidden("redirect_uri",  redirect_uri),
        hidden("state",         state),
        hidden("scope",         scope),
        hidden("pre_auth_state", pre_auth_state),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link {safe_app} with Google Home</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 20px;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      padding: 40px 32px;
      max-width: 400px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
    }}
    .logo {{
      text-align: center;
      margin-bottom: 24px;
    }}
    .logo svg {{ width: 48px; height: 48px; }}
    h1 {{ font-size: 22px; font-weight: 600; color: #1a1a1a; margin-bottom: 8px; text-align: center; }}
    .subtitle {{ font-size: 14px; color: #666; text-align: center; margin-bottom: 28px; }}
    .permission {{ background: #f0f7ff; border-radius: 8px; padding: 14px; margin-bottom: 24px; }}
    .permission p {{ font-size: 13px; color: #444; }}
    .permission strong {{ color: #1a73e8; }}
    label {{ display: block; font-size: 14px; font-weight: 500; color: #333; margin-bottom: 6px; }}
    input[type="email"], input[type="password"] {{
      width: 100%; padding: 12px 14px; border: 1.5px solid #ddd;
      border-radius: 8px; font-size: 15px; outline: none; transition: border-color 0.2s;
    }}
    input:focus {{ border-color: #1a73e8; }}
    .field {{ margin-bottom: 18px; }}
    button {{
      width: 100%; padding: 14px; background: #1a73e8; color: white;
      border: none; border-radius: 8px; font-size: 16px; font-weight: 600;
      cursor: pointer; transition: background 0.2s;
    }}
    button:hover {{ background: #1558b0; }}
    .error {{ color: #d32f2f; font-size: 14px; margin-bottom: 16px; text-align: center; }}
    .footer {{ text-align: center; margin-top: 24px; font-size: 12px; color: #999; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect width="48" height="48" rx="12" fill="#1a73e8"/>
        <path d="M24 12C17.373 12 12 17.373 12 24s5.373 12 12 12 12-5.373 12-12-5.373-12-12-12zm0 4a8 8 0 110 16 8 8 0 010-16z" fill="white" opacity="0.9"/>
        <circle cx="24" cy="24" r="4" fill="white"/>
      </svg>
    </div>
    <h1>Link {safe_app}</h1>
    <p class="subtitle">Sign in to connect your account with Google Home</p>

    <div class="permission">
      <p>Google Home is requesting access to: <strong>{safe_scope}</strong></p>
    </div>

    {safe_error}

    <form method="POST" autocomplete="on">
      {hidden_fields}
      <div class="field">
        <label for="username">Email address</label>
        <input type="email" id="username" name="username" placeholder="you@example.com"
               autocomplete="email" required>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" placeholder="Your password"
               autocomplete="current-password" required>
      </div>
      <button type="submit">Sign in &amp; Allow</button>
    </form>

    <div class="footer">
      Your credentials are sent securely and never shared with Google.
    </div>
  </div>
</body>
</html>"""


def _error_page(message: str) -> str:
    """Render a plain error page when we cannot redirect."""
    safe_msg = html.escape(message)
    safe_app = html.escape(_APP_NAME)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Error — {safe_app}</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 60px auto; padding: 0 24px; }}
    h1 {{ color: #d32f2f; }}
    p {{ color: #555; margin-top: 12px; line-height: 1.5; }}
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
    method = (event.get("httpMethod") or "GET").upper()
    logger.info("REQUEST_START function=google_home_oauth_authorize method=%s request_id=%s",
                method, request_id)

    if _missing_vars:
        return _html_resp(500, _error_page("Server configuration error. Please try again later."))

    if method == "GET":
        return _handle_get(event, request_id)
    elif method == "POST":
        return _handle_post(event, request_id)
    else:
        return _html_resp(405, _error_page("Method not allowed."))


def _handle_get(event: dict, request_id: str) -> dict:
    """Serve the login form or auto-authorize via pre_auth_state."""
    params = event.get("queryStringParameters") or {}
    client_id      = params.get("client_id", "")
    redirect_uri   = params.get("redirect_uri", "")
    response_type  = params.get("response_type", "")
    state          = params.get("state", "")
    scope          = params.get("scope", "profile")
    pre_auth_state = params.get("pre_auth_state", "")

    logger.info("AUTHORIZE_GET client_id=%s redirect_uri=%s response_type=%s "
                "pre_auth_state_present=%s request_id=%s",
                client_id, redirect_uri, response_type, bool(pre_auth_state), request_id)

    # Validate client_id
    if not _validate_client_id(client_id):
        logger.warning("INVALID_CLIENT_ID client_id=%s request_id=%s", client_id, request_id)
        return _html_resp(400, _error_page(
            "Invalid client_id. This account linking request cannot be completed."
        ))

    # Validate redirect_uri
    if not _validate_redirect_uri(redirect_uri):
        logger.warning("INVALID_REDIRECT_URI redirect_uri=%s request_id=%s",
                       redirect_uri, request_id)
        return _html_resp(400, _error_page(
            "Invalid redirect_uri. This account linking request cannot be completed."
        ))

    # Must be code flow
    if response_type != "code":
        return _redirect_error(redirect_uri, state, "unsupported_response_type")

    # ── Pre-auth path: user already authenticated in Flutter app ──────────────
    if pre_auth_state:
        logger.info("PRE_AUTH_FLOW pre_auth_state=%s... request_id=%s",
                    pre_auth_state[:8], request_id)
        user_id = _get_session_user(pre_auth_state)
        if user_id:
            return _issue_code_and_redirect(user_id, redirect_uri, state, scope, request_id)
        else:
            logger.warning("PRE_AUTH_INVALID pre_auth_state=%s — falling through to login form",
                           pre_auth_state)
            # Fall through to login form (don't reveal the failure)

    # ── Standard path: show login form ────────────────────────────────────────
    logger.info("SHOWING_LOGIN_FORM client_id=%s redirect_uri=%s request_id=%s",
                client_id, redirect_uri, request_id)
    return _html_resp(200, _login_page(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
        pre_auth_state=pre_auth_state,
    ))


def _handle_post(event: dict, request_id: str) -> dict:
    """Process the login form submission."""
    body_raw = event.get("body") or ""
    # Handle base64-encoded body (API GW may encode binary)
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode()

    try:
        params = dict(urllib.parse.parse_qsl(body_raw, keep_blank_values=True))
    except Exception as exc:
        logger.warning("FORM_PARSE_ERROR error=%s request_id=%s", exc, request_id)
        return _html_resp(400, _error_page("Invalid form submission."))

    username       = params.get("username", "").strip()
    password       = params.get("password", "")
    client_id      = params.get("client_id", "")
    redirect_uri   = params.get("redirect_uri", "")
    state          = params.get("state", "")
    scope          = params.get("scope", "profile")
    pre_auth_state = params.get("pre_auth_state", "")

    logger.info("AUTHORIZE_POST username=%s client_id=%s redirect_uri=%s request_id=%s",
                username, client_id, redirect_uri, request_id)

    # Re-validate client_id and redirect_uri (never trust POST params alone)
    if not _validate_client_id(client_id):
        return _html_resp(400, _error_page("Invalid client_id."))
    if not _validate_redirect_uri(redirect_uri):
        return _html_resp(400, _error_page("Invalid redirect_uri."))

    # ── Authenticate user ──────────────────────────────────────────────────────
    if not username or not password:
        return _html_resp(200, _login_page(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            scope=scope, pre_auth_state=pre_auth_state,
            error_msg="Email and password are required.",
        ))

    user_id = _authenticate_cognito(username, password)
    if not user_id:
        _audit("GH_OAUTH_LOGIN_FAILED", username=username, redirect_uri=redirect_uri,
               request_id=request_id)
        return _html_resp(200, _login_page(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            scope=scope, pre_auth_state=pre_auth_state,
            error_msg="Incorrect email or password. Please try again.",
        ))

    logger.info("LOGIN_OK userId=%s request_id=%s", user_id, request_id)
    _audit("GH_OAUTH_LOGIN_SUCCESS", userId=user_id, redirect_uri=redirect_uri,
           request_id=request_id)

    return _issue_code_and_redirect(user_id, redirect_uri, state, scope, request_id)


def _issue_code_and_redirect(user_id: str, redirect_uri: str, state: str,
                              scope: str, request_id: str) -> dict:
    """Generate an auth code, store it, and redirect to Google's redirect_uri."""
    code = _generate_auth_code()
    try:
        _store_auth_code(code, user_id, redirect_uri, scope)
    except Exception as exc:
        logger.error("AUTH_CODE_STORE_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _redirect_error(redirect_uri, state, "server_error")

    _audit("GH_AUTH_CODE_ISSUED", userId=user_id, code_prefix=code[:8],
           redirect_uri=redirect_uri, request_id=request_id)

    # Build redirect URL
    callback_params = {"code": code, "state": state}
    location = f"{redirect_uri}?{urllib.parse.urlencode(callback_params)}"
    logger.info("REDIRECT_TO_GOOGLE userId=%s location=%s... request_id=%s",
                user_id, location[:60], request_id)

    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _redirect_error(redirect_uri: str, state: str, error: str) -> dict:
    """Redirect to Google's redirect_uri with an error parameter."""
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
