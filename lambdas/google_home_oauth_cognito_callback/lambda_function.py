"""
google_home_oauth_cognito_callback — two routes, one Lambda

  GET  /google-home/oauth/cognito/callback — Steps 9–14
  POST /google-home/oauth/consent          — Steps 15–19 (approve) and 25–27 (cancel)

──────────────────────────────────────────────────────────────────────────────
GET /cognito/callback  (steps 9–14)
──────────────────────────────────────────────────────────────────────────────
  9.  Browser arrives with ?code=<cognito_code>&state=<google_state>
  10. Forward to this Lambda via API GW
  11. Exchange Cognito code at Cognito /oauth2/token
  12. Cognito returns ID token + access token
  13. Validate identity — extract Cognito sub (userId) and email from ID token
  14. Update DDB transaction with userId + email; display DIGILUX consent page

──────────────────────────────────────────────────────────────────────────────
POST /consent  (steps 15–19 approve, steps 25–27 cancel)
──────────────────────────────────────────────────────────────────────────────
  [User approves]
  15. POST body: action=approve, state=<google_state>
  16. Load transaction from DDB (includes userId + Google redirect_uri + state)
  17. Create short-lived single-use auth code in DDB
  18. 302 → Google redirect_uri?code=<auth_code>&state=<google_state>
  19. Google Home receives the authorization code

  [User cancels]
  25. POST body: action=cancel, state=<google_state>
  26. Load + delete transaction from DDB
  27. 302 → Google redirect_uri?error=access_denied&state=<google_state>
      (or safe error HTML page if redirect_uri cannot be determined)
"""
from __future__ import annotations

import base64
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
_REGION               = os.environ.get("DATA_REGION", "")
_COGNITO_CLIENT_ID    = os.environ.get("COGNITO_CLIENT_ID", "")
# Optional — only needed if the Cognito app client has a client secret configured
_COGNITO_CLIENT_SECRET= os.environ.get("COGNITO_CLIENT_SECRET", "")
# Custom Cognito domain, e.g. auth.digilux.co.in
_COGNITO_DOMAIN       = os.environ.get("COGNITO_HOSTED_UI_DOMAIN", "")
# Must exactly match what was used in the /authorize redirect
_COGNITO_CALLBACK_URI = os.environ.get("COGNITO_CALLBACK_URI", "")
_TRANSACTIONS_TABLE   = os.environ.get("GH_OAUTH_TRANSACTIONS_TABLE", "google_home_oauth_transactions")
_AUTH_CODES_TABLE     = os.environ.get("GH_AUTH_CODES_TABLE", "google_home_auth_codes")
_AUTH_CODE_TTL_S      = int(os.environ.get("AUTH_CODE_TTL_SECONDS", "300"))
_APP_NAME             = os.environ.get("APP_NAME", "Digilux")
_BRAND_TITLE          = os.environ.get("BRAND_TITLE", _APP_NAME)
_BRAND_SUBTITLE       = os.environ.get("BRAND_SUBTITLE", "Identity Server")

# ── Brand logo ─────────────────────────────────────────────────────────────────
def _load_logo() -> str:
    """Return base64 data URI for brand-logo.png bundled with this Lambda."""
    _logo_path = os.path.join(os.path.dirname(__file__), "brand-logo.png")
    try:
        with open(_logo_path, "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except Exception as exc:
        logger.warning("BRAND_LOGO_LOAD_FAILED path=%s error=%s", _logo_path, exc)
        return ""

_BRAND_LOGO_DATA_URI = _load_logo()
_PRIVACY_POLICY_URL   = os.environ.get("PRIVACY_POLICY_URL", "https://digilux.co.in/privacy-policy.jsp")
_TERMS_URL            = os.environ.get("TERMS_URL", "https://digilux.co.in/terms.jsp")

_REQUIRED_VARS = {
    "DATA_REGION":              _REGION,
    "COGNITO_CLIENT_ID":        _COGNITO_CLIENT_ID,
    "COGNITO_HOSTED_UI_DOMAIN": _COGNITO_DOMAIN,
    "COGNITO_CALLBACK_URI":     _COGNITO_CALLBACK_URI,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]

# ── Startup diagnostics ────────────────────────────────────────────────────────
if _missing_vars:
    logger.error(
        "CONFIG_STARTUP_ERROR missing_required_env=%s function=google_home_oauth_cognito_callback "
        "— all invocations will return HTTP 500 until these are set",
        _missing_vars,
    )
else:
    logger.info(
        "CONFIG_OK function=google_home_oauth_cognito_callback region=%s "
        "cognito_domain=%s callback_uri=%s transactions_table=%s "
        "auth_codes_table=%s auth_code_ttl_seconds=%d "
        "app_name=%s brand_subtitle=%s privacy_policy_url=%s terms_url=%s",
        _REGION, _COGNITO_DOMAIN, _COGNITO_CALLBACK_URI,
        _TRANSACTIONS_TABLE, _AUTH_CODES_TABLE, _AUTH_CODE_TTL_S,
        _APP_NAME, _BRAND_SUBTITLE, _PRIVACY_POLICY_URL, _TERMS_URL,
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


# ── DynamoDB helpers ───────────────────────────────────────────────────────────

def _get_transaction(state: str) -> dict | None:
    """Load an in-progress OAuth transaction from DDB. Returns None if missing or expired."""
    try:
        resp = _ddb().Table(_TRANSACTIONS_TABLE).get_item(Key={"state": state})
        item = resp.get("Item")
    except ClientError as exc:
        logger.error("DDB_GET_TRANSACTION_ERROR state=%s... error=%s", state[:8], exc)
        return None

    if not item:
        logger.warning("TRANSACTION_NOT_FOUND state=%s...", state[:8])
        return None

    if item.get("ttl", 0) < int(time.time()):
        logger.warning("TRANSACTION_EXPIRED state=%s...", state[:8])
        return None

    return item


def _update_transaction_user(state: str, user_id: str, user_email: str) -> None:
    """Add userId and email to the transaction after successful Cognito login (step 13)."""
    _ddb().Table(_TRANSACTIONS_TABLE).update_item(
        Key={"state": state},
        UpdateExpression="SET userId = :uid, userEmail = :email",
        ExpressionAttributeValues={":uid": user_id, ":email": user_email},
    )
    logger.info("TRANSACTION_USER_SET state=%s... userId=%s", state[:8], user_id)


def _delete_transaction(state: str) -> None:
    """Remove the transaction once consumed (approve or cancel)."""
    try:
        _ddb().Table(_TRANSACTIONS_TABLE).delete_item(Key={"state": state})
        logger.debug("TRANSACTION_DELETED state=%s...", state[:8])
    except ClientError as exc:
        logger.warning("TRANSACTION_DELETE_ERROR state=%s... error=%s", state[:8], exc)


def _store_auth_code(code: str, user_id: str, redirect_uri: str, scope: str) -> None:
    """Persist the short-lived single-use Google auth code (step 17)."""
    now = int(time.time())
    _ddb().Table(_AUTH_CODES_TABLE).put_item(Item={
        "code":        code,
        "userId":      user_id,
        "redirectUri": redirect_uri,
        "scope":       scope,
        "createdAt":   now,
        "ttl":         now + _AUTH_CODE_TTL_S,
    })
    logger.info(
        "AUTH_CODE_STORED code=%s... userId=%s ttl_seconds=%d",
        code[:8], user_id, _AUTH_CODE_TTL_S,
    )


# ── Step 11–13: Cognito code exchange ─────────────────────────────────────────

def _exchange_cognito_code(code: str) -> dict:
    """
    Exchange a Cognito authorization code for tokens at Cognito /oauth2/token.
    Returns the parsed JSON response (contains id_token, access_token, etc.).
    Raises on HTTP or parse errors.
    """
    body = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": _COGNITO_CALLBACK_URI,
        "client_id":    _COGNITO_CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        f"https://{_COGNITO_DOMAIN}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    # Include Basic auth if the Cognito app client has a client secret
    if _COGNITO_CLIENT_SECRET:
        creds = base64.b64encode(
            f"{_COGNITO_CLIENT_ID}:{_COGNITO_CLIENT_SECRET}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {creds}")

    logger.debug("COGNITO_TOKEN_EXCHANGE_START code=%s...", code[:8])
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    logger.info("COGNITO_TOKEN_EXCHANGE_OK code=%s...", code[:8])
    return result


def _decode_id_token(id_token: str) -> dict:
    """Decode the Cognito ID token payload (signature already validated by Cognito)."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError(f"ID token has {len(parts)} parts, expected 3")
    padding = "=" * (4 - len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(parts[1] + padding))


# ── Step 14: Consent page ─────────────────────────────────────────────────────

def _consent_page(state: str, user_email: str, scope: str) -> str:
    """
    Render the DIGILUX consent page shown to the user after successful login.
    The form POST goes to /google-home/oauth/consent with action=approve or action=cancel.
    """
    safe_app      = html.escape(_APP_NAME)
    safe_title    = html.escape(_BRAND_TITLE)
    safe_subtitle = html.escape(_BRAND_SUBTITLE)
    safe_privacy  = html.escape(_PRIVACY_POLICY_URL)
    safe_terms    = html.escape(_TERMS_URL)
    safe_email    = html.escape(user_email or "your account")
    safe_state    = html.escape(state)
    logo_uri      = _BRAND_LOGO_DATA_URI

    def hidden(name: str, value: str) -> str:
        return f'<input type="hidden" name="{name}" value="{html.escape(value)}">'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorization — {safe_app}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #fff;
      max-width: 480px;
      margin: 0 auto;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    .brand-header {{
      padding: 20px 24px 16px;
      background: #fff;
      text-align: center;
    }}
    .brand-header-inner {{
      display: inline-block;
      background: #1565C0;
      border-radius: 8px;
      padding: 12px 20px;
      width: 75%;
    }}
    .brand-logo {{
      max-height: 42px;
      max-width: 100%;
      width: auto;
      height: auto;
      display: block;
      margin: 0 auto;
    }}
    .brand-header .brand-name {{
      font-size: 22px;
      font-weight: 900;
      letter-spacing: 1px;
      color: #fff;
      text-transform: uppercase;
    }}
    .brand-header .brand-sub {{
      font-size: 11px;
      letter-spacing: 2px;
      color: #aaa;
      text-transform: uppercase;
      margin-top: 2px;
    }}
    .content {{
      flex: 1;
      padding: 28px 24px 24px;
    }}
    h1 {{
      font-size: 26px;
      font-weight: 700;
      color: #1a1a1a;
      margin-bottom: 20px;
    }}
    .notice-box {{
      background: #fdf6e3;
      border: 1px solid #e8d8a0;
      border-radius: 4px;
      padding: 14px 16px;
      margin-bottom: 20px;
      font-size: 14px;
      color: #444;
      line-height: 1.5;
      text-align: center;
    }}
    .notice-box strong {{ color: #1a1a1a; }}
    .perm-box {{
      border: 1px solid #ccc;
      border-radius: 4px;
      padding: 16px;
      margin-bottom: 20px;
    }}
    .perm-box .perm-label {{
      font-size: 14px;
      color: #444;
      margin-bottom: 12px;
    }}
    .perm-box ul {{
      list-style: none;
      padding: 0;
    }}
    .perm-box ul li {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 15px;
      color: #1a1a1a;
      margin-bottom: 10px;
    }}
    .perm-box ul li:last-child {{ margin-bottom: 0; }}
    .perm-dot {{
      flex-shrink: 0;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: #2e7d32;
      display: inline-block;
    }}
    .trust-text {{
      font-size: 13px;
      color: #333;
      margin-bottom: 6px;
      font-weight: 500;
    }}
    .privacy-text {{
      font-size: 13px;
      color: #555;
      line-height: 1.6;
      margin-bottom: 28px;
    }}
    .privacy-text a {{ color: #1a73e8; text-decoration: underline; cursor: pointer; }}
    .btn-row {{
      display: flex;
      gap: 12px;
    }}
    .btn-deny, .btn-allow {{
      flex: 1;
      padding: 15px;
      border: none;
      border-radius: 4px;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.5px;
      cursor: pointer;
      text-transform: uppercase;
      background: #222;
      color: #fff;
      transition: background 0.15s;
    }}
    .btn-deny:hover {{ background: #000; }}
    .btn-allow:hover {{ background: #111; }}
  </style>
</head>
<body>
  <div class="brand-header">
    <div class="brand-header-inner">
      {f'<img class="brand-logo" src="{logo_uri}" alt="{safe_title}">' if logo_uri else ''}
    </div>
  </div>

  <div class="content">
    <h1>Authorization</h1>

    <div class="notice-box">
      By clicking <strong>Allow</strong>, you authorize <strong>Google</strong> to
      access the following information.
    </div>

    <div class="perm-box">
      <div class="perm-label">Granting permission to:</div>
      <ul>
        <li>
          <span class="perm-dot"></span>
          discoverdevices
        </li>
        <li>
          <span class="perm-dot"></span>
          controldevices
        </li>
      </ul>
    </div>

    <p class="trust-text">Make sure you trust Google</p>
    <p class="privacy-text">
      Before continuing, please review {safe_app}'s
      <a href="{safe_privacy}" target="_blank" rel="noopener noreferrer">Privacy Policy</a>
      and
      <a href="{safe_terms}" target="_blank" rel="noopener noreferrer">Terms &amp; Conditions</a>.
      These links open separately so you can return to this consent page.
    </p>

    <div class="btn-row">
      <form method="POST" action="/smarthome/google-home/oauth/consent" style="flex:1">
        {hidden("state", safe_state)}
        {hidden("action", "cancel")}
        <button type="submit" class="btn-deny" style="width:100%">DENY</button>
      </form>
      <form method="POST" action="/smarthome/google-home/oauth/consent" style="flex:1">
        {hidden("state", safe_state)}
        {hidden("action", "approve")}
        <button type="submit" class="btn-allow" style="width:100%">ALLOW</button>
      </form>
    </div>
  </div>


</body>
</html>"""


# ── Response helpers ───────────────────────────────────────────────────────────

def _html_resp(status_code: int, body: str) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": body,
    }


def _redirect(location: str) -> dict:
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def _redirect_to_google(redirect_uri: str, state: str, **params) -> dict:
    all_params: dict[str, str] = {}
    if state:
        all_params["state"] = state
    all_params.update(params)
    location = f"{redirect_uri}?{urllib.parse.urlencode(all_params)}"
    return _redirect(location)


def _error_page(message: str) -> str:
    safe_msg = html.escape(message)
    safe_app = html.escape(_APP_NAME)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Error — {safe_app}</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 60px auto; padding: 0 24px; }}
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


def _cancel_page() -> str:
    safe_app = html.escape(_APP_NAME)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Linking cancelled — {safe_app}</title>
  <style>
    body {{ font-family: sans-serif; max-width: 480px; margin: 60px auto; padding: 0 24px;
           text-align: center; }}
    h1 {{ color: #555; font-size: 22px; margin-top: 40px; }}
    p {{ color: #777; margin-top: 12px; line-height: 1.6; }}
  </style>
</head>
<body>
  <h1>Linking cancelled</h1>
  <p>You chose not to link your {safe_app} account with Google Home.</p>
  <p>You can close this window and return to the Google Home app.</p>
</body>
</html>"""


# ── Intent handlers ────────────────────────────────────────────────────────────

def _handle_cognito_callback(event: dict, request_id: str) -> dict:
    """
    Steps 9–14: receive Cognito callback, exchange code, show consent page.
    """
    params       = event.get("queryStringParameters") or {}
    cognito_code = params.get("code",  "").strip()
    state        = params.get("state", "").strip()

    error_param = params.get("error", "")
    error_desc  = params.get("error_description", "")

    logger.debug(
        "COGNITO_CALLBACK_RAW_PARAMS keys=%s error=%s error_description=%s request_id=%s",
        list(params.keys()), error_param or "none", error_desc or "none", request_id,
    )
    logger.info(
        "COGNITO_CALLBACK code_present=%s state=%s... error=%s request_id=%s",
        bool(cognito_code), state[:8] if state else "", error_param or "none", request_id,
    )

    if error_param:
        logger.warning(
            "COGNITO_RETURNED_ERROR error=%s error_description=%s state=%s... request_id=%s",
            error_param, error_desc, state[:8] if state else "", request_id,
        )
        _audit("GH_CALLBACK_COGNITO_ERROR", error=error_param,
               state_prefix=state[:8] if state else "", request_id=request_id)

    # Retrieve the stored OAuth transaction using Google's state
    if not state:
        logger.warning("COGNITO_CALLBACK_MISSING_STATE request_id=%s", request_id)
        return _html_resp(400, _error_page("Missing state parameter. Please try linking again."))

    logger.debug("LOOKING_UP_TRANSACTION state=%s... request_id=%s", state[:8], request_id)
    transaction = _get_transaction(state)
    if not transaction:
        logger.warning(
            "COGNITO_CALLBACK_TRANSACTION_NOT_FOUND state=%s... request_id=%s",
            state[:8], request_id,
        )
        return _html_resp(400, _error_page(
            "This linking session has expired or was not found. Please try again."
        ))

    redirect_uri = transaction.get("redirect_uri", "")
    scope        = transaction.get("scope", "profile")

    logger.debug(
        "TRANSACTION_LOADED state=%s... redirect_uri=%s scope=%s created_at=%s request_id=%s",
        state[:8], redirect_uri, scope, transaction.get("created_at", "?"), request_id,
    )

    if not cognito_code:
        logger.warning(
            "COGNITO_CALLBACK_MISSING_CODE state=%s... request_id=%s", state[:8], request_id
        )
        _audit("GH_CALLBACK_MISSING_CODE", state_prefix=state[:8], request_id=request_id)
        return _redirect_to_google(redirect_uri, state, error="access_denied")

    # ── Step 11: Exchange Cognito code for tokens ──────────────────────────────
    try:
        token_resp = _exchange_cognito_code(cognito_code)
    except Exception as exc:
        logger.error(
            "COGNITO_CODE_EXCHANGE_FAILED error=%s state=%s... request_id=%s",
            exc, state[:8], request_id,
        )
        _audit("GH_CALLBACK_CODE_EXCHANGE_FAILED", state_prefix=state[:8], request_id=request_id)
        return _redirect_to_google(redirect_uri, state, error="server_error")

    # ── Step 12–13: Validate identity — extract sub and email ─────────────────
    id_token = token_resp.get("id_token", "")
    if not id_token:
        logger.error(
            "COGNITO_NO_ID_TOKEN state=%s... request_id=%s", state[:8], request_id
        )
        return _redirect_to_google(redirect_uri, state, error="server_error")

    try:
        claims   = _decode_id_token(id_token)
        user_id  = claims.get("sub", "")
        email    = claims.get("email", "")
        logger.debug(
            "ID_TOKEN_CLAIMS sub=%s email=%s iss=%s... exp=%s token_use=%s request_id=%s",
            user_id, email, str(claims.get("iss", ""))[:40],
            claims.get("exp", "?"), claims.get("token_use", "?"), request_id,
        )
    except Exception as exc:
        logger.error(
            "ID_TOKEN_DECODE_FAILED error=%s state=%s... request_id=%s",
            exc, state[:8], request_id,
        )
        return _redirect_to_google(redirect_uri, state, error="server_error")

    if not user_id:
        logger.error(
            "MISSING_SUB_IN_ID_TOKEN state=%s... request_id=%s", state[:8], request_id
        )
        return _redirect_to_google(redirect_uri, state, error="server_error")

    logger.info(
        "COGNITO_IDENTITY_VERIFIED userId=%s state=%s... request_id=%s",
        user_id, state[:8], request_id,
    )
    _audit(
        "GH_CALLBACK_IDENTITY_VERIFIED",
        userId=user_id, state_prefix=state[:8], request_id=request_id,
    )

    # Store userId + email in the transaction for the consent POST to use
    try:
        _update_transaction_user(state, user_id, email)
    except ClientError as exc:
        logger.error(
            "TRANSACTION_UPDATE_FAILED error=%s userId=%s state=%s... request_id=%s",
            exc, user_id, state[:8], request_id,
        )
        return _redirect_to_google(redirect_uri, state, error="server_error")

    # ── Step 14: Display consent page ─────────────────────────────────────────
    logger.info(
        "DISPLAYING_CONSENT_PAGE userId=%s email=%s scope=%s state=%s... request_id=%s",
        user_id, email, scope, state[:8], request_id,
    )
    _audit("GH_CONSENT_PAGE_SHOWN", userId=user_id, email=email,
           state_prefix=state[:8], request_id=request_id)
    return _html_resp(200, _consent_page(state, email, scope))


def _handle_consent(event: dict, request_id: str) -> dict:
    """
    Steps 15–19 (approve) and 25–27 (cancel): process the user's consent decision.
    """
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded") and body_raw:
        body_raw = base64.b64decode(body_raw).decode()

    try:
        params = dict(urllib.parse.parse_qsl(body_raw, keep_blank_values=True))
    except Exception as exc:
        logger.warning("CONSENT_BODY_PARSE_ERROR error=%s request_id=%s", exc, request_id)
        return _html_resp(400, _error_page("Invalid form submission."))

    action = params.get("action", "").strip()
    state  = params.get("state",  "").strip()

    logger.debug(
        "CONSENT_BODY_PARSED keys=%s action=%s state=%s... request_id=%s",
        list(params.keys()), action, state[:8] if state else "", request_id,
    )
    logger.info(
        "CONSENT_POST action=%s state=%s... request_id=%s",
        action, state[:8] if state else "", request_id,
    )

    if not state:
        logger.warning("CONSENT_MISSING_STATE request_id=%s", request_id)
        return _html_resp(400, _error_page("Missing state. Please start the linking process again."))

    # Load transaction — contains userId, redirect_uri, state
    transaction = _get_transaction(state)
    if not transaction:
        logger.warning(
            "CONSENT_TRANSACTION_NOT_FOUND state=%s... request_id=%s", state[:8], request_id
        )
        return _html_resp(400, _error_page(
            "This linking session has expired. Please return to Google Home and try again."
        ))

    redirect_uri = transaction.get("redirect_uri", "")
    user_id      = transaction.get("userId", "")
    scope        = transaction.get("scope", "profile")

    logger.debug(
        "CONSENT_TRANSACTION_LOADED state=%s... userId=%s redirect_uri=%s scope=%s request_id=%s",
        state[:8], user_id or "MISSING", redirect_uri, scope, request_id,
    )

    # ── User approves (steps 15–19) ───────────────────────────────────────────
    if action == "approve":
        if not user_id:
            logger.error(
                "CONSENT_APPROVE_MISSING_USER_ID state=%s... request_id=%s",
                state[:8], request_id,
            )
            return _redirect_to_google(redirect_uri, state, error="server_error")

        # Step 17: create short-lived single-use auth code
        code = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        try:
            _store_auth_code(code, user_id, redirect_uri, scope)
        except ClientError as exc:
            logger.error(
                "CONSENT_AUTH_CODE_STORE_ERROR error=%s userId=%s request_id=%s",
                exc, user_id, request_id,
            )
            return _redirect_to_google(redirect_uri, state, error="server_error")

        # Consume the transaction (one-use only)
        _delete_transaction(state)

        _audit(
            "GH_CONSENT_APPROVED",
            userId=user_id, code_prefix=code[:8],
            state_prefix=state[:8], redirect_uri=redirect_uri, request_id=request_id,
        )
        logger.info(
            "CONSENT_APPROVED userId=%s code=%s... state=%s... request_id=%s",
            user_id, code[:8], state[:8], request_id,
        )

        # Step 18: redirect to Google's redirect_uri with code + state
        logger.debug(
            "REDIRECTING_TO_GOOGLE redirect_uri=%s code=%s... state=%s... request_id=%s",
            redirect_uri, code[:8], state[:8], request_id,
        )
        return _redirect_to_google(redirect_uri, state, code=code)

    # ── User cancels (steps 25–27) ────────────────────────────────────────────
    elif action == "cancel":
        _delete_transaction(state)
        _audit(
            "GH_CONSENT_CANCELLED",
            state_prefix=state[:8], redirect_uri=redirect_uri, request_id=request_id,
        )
        logger.info(
            "CONSENT_CANCELLED state=%s... redirect_uri=%s request_id=%s",
            state[:8], redirect_uri, request_id,
        )

        # Redirect to Google with error=access_denied, also show cancellation page
        # via the redirect (Google shows its own "linking cancelled" screen)
        if redirect_uri:
            return _redirect_to_google(redirect_uri, state, error="access_denied")
        # Fallback if redirect_uri is somehow missing — show cancel page directly
        return _html_resp(200, _cancel_page())

    else:
        logger.warning(
            "CONSENT_INVALID_ACTION action=%s state=%s... request_id=%s",
            action, state[:8], request_id,
        )
        return _html_resp(400, _error_page("Invalid action."))


# ── Main handler ───────────────────────────────────────────────────────────────

def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    method     = (event.get("httpMethod") or "GET").upper()
    path       = event.get("path", "")
    source_ip  = (event.get("requestContext") or {}).get("identity", {}).get("sourceIp", "")

    logger.info(
        "REQUEST_START function=google_home_oauth_cognito_callback "
        "method=%s path=%s source_ip=%s request_id=%s",
        method, path, source_ip, request_id,
    )

    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        _audit("GH_CALLBACK_CONFIG_ERROR", missing=str(_missing_vars), request_id=request_id)
        return _html_resp(500, _error_page("Server configuration error. Please try again later."))

    if method == "GET":
        return _handle_cognito_callback(event, request_id)
    elif method == "POST":
        return _handle_consent(event, request_id)
    else:
        return _html_resp(405, _error_page("Method not allowed."))
