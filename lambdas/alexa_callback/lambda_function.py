"""
alexa_callback — GET /alexa/callback

Handles the OAuth redirect from Amazon after the user approves
(or denies) the Alexa account linking consent screen.

Amazon redirects to:
  https://www.digilux.co.in/alexa/callback?code=AUTH_CODE&state=STATE

Two paths:
  A. Android App Links (preferred) — the OS intercepts this URL before the
     browser loads it and opens the Digilux Flutter app directly with the
     code + state params. This Lambda never fires in that case.

  B. Browser fallback — if the app isn't installed, App Links aren't
     configured, or the user is on iOS, the browser loads this URL.
     This Lambda returns an HTML page that:
       1. Immediately tries to open the app via the custom URI scheme
          digilux://alexa/callback?code=X&state=Y
       2. Falls back to a "Open Digilux App" button after 2 seconds

Error case: if the user denied consent, Amazon returns ?error=access_denied.
This Lambda returns a user-friendly error page.

No authentication required — this endpoint is the public OAuth redirect URI.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
# See .env.example at the repo root for descriptions of every variable.
_APP_SCHEME = os.environ.get("APP_SCHEME", "")

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {"APP_SCHEME": _APP_SCHEME}
_missing_vars  = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaCallback request_id=%s", request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _html_response(_error_page("server_error", "Server configuration error."), status=500)

    params = event.get("queryStringParameters") or {}
    code   = params.get("code",  "")
    state  = params.get("state", "")
    error  = params.get("error", "")
    error_description = params.get("error_description", "")

    logger.debug(
        "PARAMS code_present=%s state_present=%s error=%s error_description=%s request_id=%s",
        bool(code), bool(state), error or "none", error_description or "none", request_id,
    )

    # ── Error / cancelled case ────────────────────────────────────────────────
    if error:
        description = error_description or "The request was denied."
        logger.warning(
            "OAUTH_ERROR error=%s description=%s state=%s request_id=%s",
            error, description, state or "none", request_id,
        )
        # AUDIT — user denied consent or Amazon returned an error
        logger.info(
            "[AUDIT] LINKING_DENIED error=%s description=%s state=%s request_id=%s",
            error, description, state or "none", request_id,
        )
        return _html_response(_error_page(error, description))

    # ── Missing params ────────────────────────────────────────────────────────
    if not code or not state:
        missing = []
        if not code:  missing.append("code")
        if not state: missing.append("state")
        logger.warning("MISSING_PARAMS missing=%s request_id=%s", missing, request_id)
        return _html_response(_error_page("invalid_callback",
                                          "Missing code or state parameter."))

    # ── Success — redirect to app ─────────────────────────────────────────────
    deep_link = (
        f"{_APP_SCHEME}://alexa/callback"
        f"?code={urllib.parse.quote(code, safe='')}"
        f"&state={urllib.parse.quote(state, safe='')}"
    )

    logger.debug("DEEP_LINK_BUILT scheme=%s state=%s code_prefix=%s request_id=%s",
                 _APP_SCHEME, state, code[:6], request_id)

    # AUDIT — authorization code delivered to app
    logger.info(
        "[AUDIT] CALLBACK_SUCCESS state=%s code_prefix=%s deep_link_scheme=%s request_id=%s",
        state, code[:6], _APP_SCHEME, request_id,
    )

    logger.info("REQUEST_OK function=alexaCallback state=%s request_id=%s",
                state, request_id)

    return _html_response(_success_page(deep_link))


def _html_response(html: str, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


def _success_page(deep_link: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alexa Connected — Digilux</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff;
      border-radius: 16px;
      padding: 40px 32px;
      max-width: 380px;
      width: 90%;
      text-align: center;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
    }}
    .icon {{ font-size: 56px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; color: #111; margin-bottom: 8px; }}
    p  {{ font-size: 15px; color: #555; line-height: 1.5; margin-bottom: 28px; }}
    .btn {{
      display: inline-block;
      background: #00a8e0;
      color: #fff;
      font-size: 16px;
      font-weight: 600;
      padding: 14px 32px;
      border-radius: 8px;
      text-decoration: none;
      transition: background 0.2s;
    }}
    .btn:hover {{ background: #0090c0; }}
  </style>
  <script>
    // Attempt deep-link immediately; show the button as fallback after 2s
    window.location.href = "{deep_link}";
    setTimeout(function() {{
      document.getElementById('btn').style.display = 'inline-block';
    }}, 2000);
  </script>
</head>
<body>
  <div class="card">
    <div class="icon">&#10003;</div>
    <h1>Alexa Connected!</h1>
    <p>Your Digilux account has been linked to Alexa. Returning you to the app&hellip;</p>
    <a id="btn" class="btn" href="{deep_link}" style="display:none;">
      Open Digilux App
    </a>
  </div>
</body>
</html>"""


def _error_page(error_code: str, description: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Linking Failed — Digilux</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff;
      border-radius: 16px;
      padding: 40px 32px;
      max-width: 380px;
      width: 90%;
      text-align: center;
      box-shadow: 0 4px 20px rgba(0,0,0,0.08);
    }}
    .icon {{ font-size: 56px; margin-bottom: 16px; }}
    h1 {{ font-size: 22px; color: #c0392b; margin-bottom: 8px; }}
    p  {{ font-size: 15px; color: #555; line-height: 1.5; margin-bottom: 28px; }}
    .code {{ font-size: 12px; color: #999; font-family: monospace; }}
    .btn {{
      display: inline-block;
      background: #00a8e0;
      color: #fff;
      font-size: 16px;
      font-weight: 600;
      padding: 14px 32px;
      border-radius: 8px;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#10007;</div>
    <h1>Linking Failed</h1>
    <p>{description} Please return to the Digilux app and try again.</p>
    <p class="code">{error_code}</p>
    <br>
    <a class="btn" href="{_APP_SCHEME}://alexa/callback?error={urllib.parse.quote(error_code, safe='')}">
      Return to App
    </a>
  </div>
</body>
</html>"""
