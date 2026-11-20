"""
tests/e2e/get_cognito_token.py — Obtain a Cognito refresh token with the
smarthome_server/read+write scopes needed for E2E tests.

Run this once (or whenever the stored refresh token expires) to populate
TEST_COGNITO_REFRESH_TOKEN in tests/e2e/.env.e2e.

Usage:
    python3 tests/e2e/get_cognito_token.py

Requirements:
    pip install requests

How it works:
    1. Starts a temporary HTTP server on localhost:9999 to capture the auth code.
    2. Opens the Cognito Hosted UI login page (auth.digilux.co.in) and submits
       the test user credentials via the HTML form.
    3. Exchanges the auth code for tokens at /oauth2/token.
    4. Writes TEST_COGNITO_REFRESH_TOKEN= to .env.e2e (or prints it if the file
       doesn't exist).

Cognito refresh tokens are valid for 30 days (the user pool default).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

CLIENT_ID      = "q7189jitfkk4ttesepkgls491"
COGNITO_DOMAIN = "https://auth.digilux.co.in"
REDIRECT_URI   = "http://localhost:9999/callback"
SCOPES         = "openid email smarthome_server/read smarthome_server/write"

ENV_FILE = Path(__file__).parent / ".env.e2e"


# ── Localhost callback server ─────────────────────────────────────────────────

_captured: dict = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _captured["code"]  = params.get("code",  [""])[0]
        _captured["error"] = params.get("error", [""])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Auth code captured — you can close this tab.")
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *_):
        pass  # silence server logs


# ── Main flow ─────────────────────────────────────────────────────────────────

def _get_tokens(email: str, password: str) -> dict:
    server = HTTPServer(("localhost", 9999), _CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    session = requests.Session()

    # Step 1: load the hosted-UI login page
    auth_url = (
        f"{COGNITO_DOMAIN}/oauth2/authorize"
        f"?response_type=code&client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(SCOPES, safe='')}"
    )
    r = session.get(auth_url, allow_redirects=True, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to load login page: {r.status_code}")

    # Parse CSRF token and form action
    csrf_match        = re.search(r'name="csrf"\s+value="([^"]+)"', r.text)
    asfdata_match     = re.search(r'name="cognitoAsfData"\s+value="([^"]*)"', r.text)
    form_action_match = re.search(r'<form[^>]+action="([^"]+)"', r.text)

    if not csrf_match:
        raise RuntimeError("Could not find CSRF token in login page")

    csrf_val   = csrf_match.group(1)
    asf_val    = asfdata_match.group(1) if asfdata_match else ""
    raw_action = form_action_match.group(1).replace("&amp;", "&") if form_action_match else "/login"
    action     = raw_action if raw_action.startswith("http") else f"{COGNITO_DOMAIN}{raw_action}"

    # Step 2: submit the login form
    r2 = session.post(
        action,
        data={"csrf": csrf_val, "cognitoAsfData": asf_val,
              "username": email, "password": password},
        allow_redirects=False,
        timeout=10,
    )

    # Step 3: follow redirects until we hit localhost:9999
    redirect_url = r2.headers.get("Location", "")
    for _ in range(10):
        if not redirect_url:
            break
        if redirect_url.startswith("http://localhost:9999"):
            session.get(redirect_url, timeout=5)
            break
        r3 = session.get(redirect_url, allow_redirects=False, timeout=10)
        redirect_url = r3.headers.get("Location", "")

    server_thread.join(timeout=5)

    if _captured.get("error"):
        raise RuntimeError(f"Cognito returned error: {_captured['error']}")
    if not _captured.get("code"):
        raise RuntimeError(f"No auth code captured (last redirect: {redirect_url})")

    code = _captured["code"]

    # Step 4: exchange code for tokens
    token_resp = requests.post(
        f"{COGNITO_DOMAIN}/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":   "authorization_code",
            "code":         code,
            "client_id":    CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
        },
        timeout=10,
    )
    if token_resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({token_resp.status_code}): {token_resp.text}")

    return token_resp.json()


def _decode_scope(access_token: str) -> str:
    payload = access_token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    claims = json.loads(base64.b64decode(payload))
    return claims.get("scope", "(unknown)")


def _update_env_file(key: str, value: str) -> bool:
    """Update or append KEY=VALUE in .env.e2e. Returns True if updated."""
    if not ENV_FILE.exists():
        return False
    text = ENV_FILE.read_text()
    pattern = re.compile(rf"^{key}=.*$", re.MULTILINE)
    if pattern.search(text):
        ENV_FILE.write_text(pattern.sub(f"{key}={value}", text))
    else:
        ENV_FILE.write_text(text.rstrip() + f"\n{key}={value}\n")
    return True


def main() -> None:
    # Load credentials from .env.e2e if present
    env_vars: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env_vars[k.strip()] = v.strip()

    email    = env_vars.get("TEST_USER_EMAIL",    os.getenv("TEST_USER_EMAIL",    ""))
    password = env_vars.get("TEST_USER_PASSWORD", os.getenv("TEST_USER_PASSWORD", ""))

    if not email or not password:
        print("ERROR: TEST_USER_EMAIL / TEST_USER_PASSWORD not set in .env.e2e")
        sys.exit(1)

    print(f"Logging in as {email} ...")
    tokens = _get_tokens(email, password)

    scope = _decode_scope(tokens["access_token"])
    print(f"Access token scope : {scope}")

    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        print("ERROR: No refresh token returned (check Cognito app client settings)")
        sys.exit(1)

    if _update_env_file("TEST_COGNITO_REFRESH_TOKEN", refresh_token):
        print(f"Updated TEST_COGNITO_REFRESH_TOKEN in {ENV_FILE}")
    else:
        print(f".env.e2e not found — add this line manually:")
        print(f"TEST_COGNITO_REFRESH_TOKEN={refresh_token}")

    print("\nDone. Run pytest to verify:")
    print("  python3 -m pytest tests/e2e/ -m smoke -q")


if __name__ == "__main__":
    main()
