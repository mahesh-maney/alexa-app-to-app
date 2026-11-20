"""
tests/e2e/conftest.py — Shared fixtures for all E2E tests.


Fixtures:
    cfg             EnvConfig for the selected TEST_ENV
    api             requests.Session (shared, no auth)
    cognito_tokens  Real Cognito tokens obtained via USER_PASSWORD_AUTH (session scope)
    auth_headers    {"Authorization": "Bearer <access_token>"} (session scope)
    site_id         TEST_SITE_ID value
    device_id       TEST_DEVICE_ID value (skips test if unset)
    ensure_gh_unlinked    Function-scope: unlinks GH before + after each test
    ensure_alexa_unlinked Function-scope: unlinks Alexa before + after each test

NOTE — Cognito USER_PASSWORD_AUTH:
    The Cognito app client must have ALLOW_USER_PASSWORD_AUTH in its auth flows.
    If the test run fails with "USER_PASSWORD_AUTH not supported", enable it in
    AWS Console → Cognito → User Pools → App Clients → Edit → Auth Flows.
    Alternatively, set TEST_COGNITO_JWT to a pre-obtained access token.
"""
from __future__ import annotations

import datetime
import json as _json
import os
import time
from pathlib import Path

import boto3
import pytest
import requests

# Load .env.e2e before anything else so all os.getenv() calls see the values.
# This avoids shell xargs mangling special characters (!, #, etc.) in passwords.
_env_file = Path(__file__).parent / ".env.e2e"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

from tests.e2e.config import get_config, test_user_email, test_user_password, test_site_id


# ── HTTP call capture (feeds the HTML report) ──────────────────────────────────

_current_calls: list[dict] = []   # reset before every test by _reset_capture


class _CapturingSession(requests.Session):
    """requests.Session that records every HTTP call for the HTML report."""

    def request(self, method, url, **kwargs):
        t0   = time.perf_counter()
        resp = super().request(method, url, **kwargs)
        dur  = round(time.perf_counter() - t0, 3)

        # Serialise request body
        raw = kwargs.get("json") or kwargs.get("data") or ""
        if isinstance(raw, dict):
            raw = _json.dumps(raw, indent=2)
        elif isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        # Pretty-print response if JSON
        try:
            resp_text = _json.dumps(_json.loads(resp.text), indent=2)
        except Exception:
            resp_text = resp.text

        # Mask auth token in headers — never log bearer tokens
        hdrs = {
            k: ("Bearer <MASKED>" if k.lower() == "authorization" else v)
            for k, v in (kwargs.get("headers") or {}).items()
        }

        _current_calls.append({
            "method":   method.upper(),
            "url":      url,
            "params":   kwargs.get("params") or {},
            "headers":  hdrs,
            "body":     str(raw)[:3000],
            "status":   resp.status_code,
            "response": resp_text[:3000],
            "elapsed":  dur,
        })
        return resp


# ── Core fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def cfg():
    return get_config()


@pytest.fixture(scope="session")
def api():
    s = _CapturingSession()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(autouse=True)
def _reset_capture():
    """Clear the HTTP call log before every test so each test sees only its own calls."""
    _current_calls.clear()
    yield


@pytest.fixture(scope="session")
def cognito_tokens(cfg):
    """
    Obtain a Cognito access token with smarthome_server/read+write scopes.

    Preferred path (TEST_COGNITO_REFRESH_TOKEN set in .env.e2e):
        POST {cognito_domain}/oauth2/token with grant_type=refresh_token.
        The refresh token is obtained once via the auth-code flow (see
        tests/e2e/get_cognito_token.py) and stored in .env.e2e.

    Fallback (TEST_COGNITO_JWT set):
        Use the token directly.

    Why not USER_PASSWORD_AUTH?
        USER_PASSWORD_AUTH only grants scope 'aws.cognito.signin.user.admin'.
        The API methods require 'smarthome_server/read' or 'smarthome_server/write'.
        Only the hosted-UI authorization-code flow yields those resource-server scopes.
    """
    override = os.getenv("TEST_COGNITO_JWT")
    if override:
        return {"AccessToken": override, "RefreshToken": "", "IdToken": ""}

    refresh_token = os.getenv("TEST_COGNITO_REFRESH_TOKEN", "")
    if refresh_token:
        import requests as _req
        resp = _req.post(
            f"https://{cfg.cognito_domain}/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": cfg.cognito_client_id,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            pytest.fail(
                f"Refresh token exchange failed ({resp.status_code}): {resp.text}\n"
                "Re-run tests/e2e/get_cognito_token.py to get a fresh refresh token."
            )
        data = resp.json()
        return {
            "AccessToken": data["access_token"],
            "IdToken": data.get("id_token", ""),
            "RefreshToken": refresh_token,
        }

    pytest.fail(
        "TEST_COGNITO_REFRESH_TOKEN is not set in .env.e2e.\n"
        "Run: python3 tests/e2e/get_cognito_token.py  to obtain one.\n"
        "Then add TEST_COGNITO_REFRESH_TOKEN=<value> to tests/e2e/.env.e2e."
    )


@pytest.fixture(scope="session")
def auth_headers(cognito_tokens):
    return {"Authorization": f"Bearer {cognito_tokens['AccessToken']}"}


@pytest.fixture(scope="session")
def site_id():
    return test_site_id()


@pytest.fixture(scope="session")
def device_id():
    val = os.getenv("TEST_DEVICE_ID", "")
    if not val:
        pytest.skip("TEST_DEVICE_ID not set — skipping fulfillment test")
    return val


# ── State cleanup fixtures ─────────────────────────────────────────────────────

@pytest.fixture()
def ensure_gh_unlinked(cfg, api, auth_headers):
    """Unlink GH before the test, then again after. Guarantees a clean slate."""
    _gh_unlink(cfg, api, auth_headers)
    yield
    _gh_unlink(cfg, api, auth_headers)


@pytest.fixture()
def ensure_alexa_unlinked(cfg, api, auth_headers, site_id):
    """Unlink Alexa before the test, then again after. Guarantees a clean slate."""
    _alexa_unlink(cfg, api, auth_headers, site_id)
    yield
    _alexa_unlink(cfg, api, auth_headers, site_id)


def _gh_unlink(cfg, api, auth_headers):
    try:
        api.delete(
            f"{cfg.api_base}/api/v1/voice/google-home/account-linking",
            headers=auth_headers,
            timeout=10,
        )
    except Exception:
        pass  # ignore — may already be unlinked


def _alexa_unlink(cfg, api, auth_headers, site_id):
    try:
        api.delete(
            f"{cfg.api_base}/api/v1/alexa/unlink",
            params={"siteId": site_id},
            headers=auth_headers,
            timeout=10,
        )
    except Exception:
        pass  # ignore — may already be unlinked


# ── Browser (Playwright) availability guard ────────────────────────────────────

@pytest.fixture(scope="session")
def playwright_available():
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# ── Helpers shared across test modules ────────────────────────────────────────

def poll_until(fn, expected=True, retries=10, interval=2):
    """
    Call fn() up to `retries` times, waiting `interval` seconds between attempts.
    Returns the last result. Useful for waiting on async state changes.
    """
    result = None
    for _ in range(retries):
        result = fn()
        if result == expected:
            return result
        time.sleep(interval)
    return result


# ── HTML report customisation ──────────────────────────────────────────────────

def pytest_configure(config):
    """Inject project-level metadata into the HTML report header."""
    md = getattr(config, "_metadata", None)
    if md is None:
        return
    md["Project"]     = "Digilux App-to-App Integration"
    md["Environment"] = os.getenv("TEST_ENV", "digilux-qa")
    md["Run At"]      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md["Site ID"]     = os.getenv("TEST_SITE_ID", "—")


def pytest_html_report_title(report):
    env = os.getenv("TEST_ENV", "digilux-qa")
    ts  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    report.title = f"Digilux E2E — {env} — {ts}"


# ── Per-test extras ────────────────────────────────────────────────────────────

def _status_color(code: int) -> str:
    if 200 <= code < 300: return "#4CAF50"
    if 300 <= code < 400: return "#FF9800"
    if code >= 400:       return "#F44336"
    return "#9E9E9E"


_METHOD_COLOR = {
    "GET":    "#61DAFB",
    "POST":   "#98FB98",
    "DELETE": "#FFA07A",
    "PUT":    "#FFD700",
    "PATCH":  "#DA70D6",
}


def _calls_html(calls: list[dict]) -> str:
    if not calls:
        return (
            '<p style="color:#666;font-style:italic;margin:6px 0 2px">'
            "No HTTP calls recorded for this test.</p>"
        )
    rows = []
    for i, c in enumerate(calls, 1):
        sc        = c["status"]
        sc_color  = _status_color(sc)
        mc        = _METHOD_COLOR.get(c["method"], "#e0e0e0")
        params_s  = _json.dumps(c["params"])  if c["params"]  else "—"
        headers_s = _json.dumps(c["headers"]) if c["headers"] else "—"
        body_s    = c["body"]     or "—"
        resp_s    = c["response"] or "—"

        rows.append(f"""
<div style="margin:6px 0;border:1px solid #2d2d44;border-radius:6px;
            overflow:hidden;font-family:'Courier New',monospace;font-size:12px">
  <!-- method + url bar -->
  <div style="background:#1a1a2e;padding:7px 12px;
              display:flex;justify-content:space-between;align-items:center">
    <span>
      <span style="color:{mc};font-weight:700;letter-spacing:.5px">{c["method"]}</span>
      <span style="color:#ccc;margin-left:10px;word-break:break-all">{c["url"]}</span>
    </span>
    <span style="white-space:nowrap;margin-left:16px">
      <span style="color:{sc_color};font-weight:700">{sc}</span>
      <span style="color:#888;margin-left:8px">{c["elapsed"]}s</span>
    </span>
  </div>
  <!-- params + headers row -->
  <div style="background:#111;padding:6px 12px;border-bottom:1px solid #2d2d44;
              display:flex;gap:24px;flex-wrap:wrap">
    <span><span style="color:#888">Params:</span>
          <span style="color:#FFD700">{params_s}</span></span>
    <span><span style="color:#888">Headers:</span>
          <span style="color:#aaa">{headers_s}</span></span>
  </div>
  <!-- request body / response body -->
  <div style="background:#111;padding:8px 12px;display:flex;gap:12px">
    <div style="flex:1;min-width:0">
      <div style="color:#888;font-size:10px;text-transform:uppercase;
                  letter-spacing:.8px;margin-bottom:4px">Request Body</div>
      <pre style="margin:0;padding:8px;background:#0d1117;color:#e0e0e0;
                  border-radius:4px;font-size:11px;overflow:auto;
                  max-height:160px;white-space:pre-wrap;word-break:break-all">{body_s}</pre>
    </div>
    <div style="flex:1;min-width:0">
      <div style="color:#888;font-size:10px;text-transform:uppercase;
                  letter-spacing:.8px;margin-bottom:4px">
        Response &nbsp;<span style="color:{sc_color}">{sc}</span>
      </div>
      <pre style="margin:0;padding:8px;background:#0d1117;color:#e0e0e0;
                  border-radius:4px;font-size:11px;overflow:auto;
                  max-height:160px;white-space:pre-wrap;word-break:break-all">{resp_s}</pre>
    </div>
  </div>
</div>""")
    return "\n".join(rows)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report  = outcome.get_result()

    if report.when != "call":
        return

    try:
        import pytest_html
    except ImportError:
        return

    # ── serial number (stored on the session object) ──────────────────────────
    if not hasattr(item.session, "_e2e_serial"):
        item.session._e2e_serial = 0
    item.session._e2e_serial += 1
    serial = item.session._e2e_serial

    # ── gather metadata ───────────────────────────────────────────────────────
    marks     = [m.name for m in item.iter_markers()
                 if m.name not in ("parametrize", "usefixtures")]
    doc_line  = (getattr(item.function, "__doc__", None) or "").strip().splitlines()[0] \
                if getattr(item.function, "__doc__", None) else ""
    calls     = list(_current_calls)
    total_t   = round(sum(c["elapsed"] for c in calls), 3)
    env_name  = os.getenv("TEST_ENV", "digilux-qa")
    duration  = getattr(report, "duration", 0.0)

    # ── status badge ──────────────────────────────────────────────────────────
    if report.passed:
        badge = ('<span style="background:#4CAF50;color:#fff;padding:2px 10px;'
                 'border-radius:3px;font-size:11px;font-weight:700">PASSED</span>')
    elif report.failed:
        badge = ('<span style="background:#F44336;color:#fff;padding:2px 10px;'
                 'border-radius:3px;font-size:11px;font-weight:700">FAILED</span>')
    else:
        badge = ('<span style="background:#FF9800;color:#fff;padding:2px 10px;'
                 'border-radius:3px;font-size:11px;font-weight:700">SKIPPED</span>')

    marks_html = "".join(
        f'<span style="background:#2a2a3e;color:#a0a0c0;padding:1px 7px;'
        f'border-radius:3px;font-size:10px;margin-left:4px">{m}</span>'
        for m in marks
    )

    header = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;
            font-size:12px;padding:6px 2px 10px;border-bottom:1px solid #2d2d44;
            margin-bottom:10px;display:flex;justify-content:space-between;
            align-items:flex-start;flex-wrap:wrap;gap:6px">
  <div>
    <span style="color:#666;font-size:13px;font-weight:700;margin-right:10px">#{serial}</span>
    {badge}
    <span style="color:#ccc;margin-left:10px">{doc_line}</span>
    {marks_html}
  </div>
  <div style="color:#888;font-size:11px;white-space:nowrap">
    <span title="Total test duration">&#9201; {duration:.3f}s</span>
    &nbsp;|&nbsp;
    <span title="HTTP calls made">{len(calls)} call(s) &middot; {total_t}s net</span>
    &nbsp;|&nbsp;
    <span style="color:#61DAFB">{env_name}</span>
  </div>
</div>
"""

    extras = getattr(report, "extras", [])
    extras.append(pytest_html.extras.html(header + _calls_html(calls)))
    report.extras = extras
