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
