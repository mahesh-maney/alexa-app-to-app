"""
conftest.py — pytest configuration for alexa app-to-app tests.

Sets all required environment variables BEFORE any Lambda module is imported.
pytest loads conftest.py before collecting/importing test files, so these values
are in place when the module-level os.environ.get() calls execute.

For local development outside of tests, copy .env.example to .env and export
the variables (or use python-dotenv in a dev harness).
"""
import os

# ── Common ─────────────────────────────────────────────────────────────────────
os.environ.setdefault("LOG_LEVEL",   "WARNING")   # suppress noise during tests
os.environ.setdefault("DATA_REGION", "ap-south-1")

# ── alexa_start_app_to_app ─────────────────────────────────────────────────────
os.environ.setdefault("SESSION_TABLE",       "alexa_app_linking_sessions")
os.environ.setdefault("REDIRECT_URI",        "https://www.digilux.co.in/alexa/callback")
os.environ.setdefault("SESSION_TTL_SECONDS", "600")

# ── alexa_complete_app_to_app ──────────────────────────────────────────────────
os.environ.setdefault("LWA_TOKENS_TABLE",            "digilux_honeywell_alexa_lwa_tokens")
os.environ.setdefault("LWA_SECRET_ARN",              "arn:aws:secretsmanager:eu-west-1:000000000000:secret:test/lwa")
os.environ.setdefault("LWA_SECRET_REGION",           "eu-west-1")
os.environ.setdefault("LWA_TOKEN_URL",               "https://api.amazon.com/auth/o2/token")
os.environ.setdefault("ALEXA_SKILL_ID",              "amzn1.ask.skill.test-skill-id")
os.environ.setdefault("ALEXA_SKILL_STAGE",           "live")
os.environ.setdefault("SKILL_ENABLEMENT_URL",        "https://api.amazonalexa.com/v1/users/~current/skills")
os.environ.setdefault("LWA_HTTP_TIMEOUT",            "10")
os.environ.setdefault("TOKEN_EXPIRY_BUFFER_SECONDS", "60")

# ── alexa_callback ─────────────────────────────────────────────────────────────
os.environ.setdefault("APP_SCHEME", "digilux")

# ── Security / optional features (empty = disabled in tests) ───────────────────
os.environ.setdefault("COGNITO_USER_POOL_ID",           "")   # JWT sig verify disabled
os.environ.setdefault("COGNITO_REGION",                  "ap-south-1")
os.environ.setdefault("KMS_KEY_ARN",                     "")   # KMS encryption disabled
os.environ.setdefault("MAX_PENDING_SESSIONS_PER_USER",   "5")
os.environ.setdefault("MAX_REQUEST_BODY_BYTES",          "4096")
os.environ.setdefault("MAX_AUTH_CODE_LEN",               "2048")
os.environ.setdefault("ALLOWED_REDIRECT_HOSTS",          "")
os.environ.setdefault("LWA_REVOKE_URL",                  "https://api.amazon.com/auth/o2/revoke")

# ── Google Home Lambdas ────────────────────────────────────────────────────────
os.environ.setdefault("GH_SESSIONS_TABLE",            "google_home_link_sessions")
os.environ.setdefault("GH_AUTH_CODES_TABLE",          "google_home_auth_codes")
os.environ.setdefault("GH_TOKENS_TABLE",              "google_home_tokens")
os.environ.setdefault("GOOGLE_AGENT_ID",              "test-agent-id")
os.environ.setdefault("GOOGLE_CLIENT_ID",             "test-google-client-id")
os.environ.setdefault("GOOGLE_REDIRECT_URI",          "https://oauth-redirect.googleusercontent.com/r/test")
os.environ.setdefault("GOOGLE_SCOPE",                 "profile")
os.environ.setdefault("OAUTH_BASE_URL",               "https://iot.digilux.co.in/smarthome")
os.environ.setdefault("ALLOWED_REDIRECT_URIS",        "")          # empty = allow all in tests
os.environ.setdefault("COGNITO_CLIENT_ID",            "test-cognito-client-id")
os.environ.setdefault("AUTH_CODE_TTL_SECONDS",        "300")
os.environ.setdefault("APP_NAME",                     "Digilux Smart Home")
os.environ.setdefault("GOOGLE_CLIENT_SECRET_ARN",     "arn:aws:secretsmanager:ap-south-1:000000000000:secret:test/gh")
os.environ.setdefault("GOOGLE_SECRET_REGION",         "ap-south-1")
os.environ.setdefault("ACCESS_TOKEN_TTL_SECONDS",     "3600")
os.environ.setdefault("REFRESH_TOKEN_TTL_SECONDS",    "15552000")
os.environ.setdefault("USER_DEVICE_MAPPING_TABLE",    "digilux_honeywell_user_device_mapping")
os.environ.setdefault("HTTP_TIMEOUT",                 "10")
