"""
alexa_cognito_callback — GET /api/v1/alexa/cognito/callback

Stage 2 of the two-stage Alexa SMAPI account-linking flow.

Flow:
  1.  Parse Cognito auth code + HMAC-signed state from query params
  2.  Verify HMAC state → pkceTransactionId
  3.  Look up PKCE transaction in alexa_pkce_sessions
        - must exist and be PENDING_COGNITO
        - must not be expired
  4.  Decrypt KMS-encrypted PKCE verifier
  5.  Exchange Cognito auth code for Cognito tokens (PKCE token exchange)
  6.  Decode Cognito access token → extract sub (= userId)
  7.  Identity binding check: sub MUST equal PKCE transaction userId
        Prevents wrong-account attack: an attacker cannot link a different
        user's Digilux site by logging in with a different Cognito account.
  8.  Retrieve LWA access token for the user (from alexa_lwa_tokens)
  9.  Call SMAPI PUT /v1/users/~current/skills/{skillId}/enablement
        Enables the skill for the user so it shows as linked in the Alexa app.
        Non-blocking: warning only if unavailable or token expired.
  10. Mark PKCE transaction COGNITO_VERIFIED in DDB
  11. Fire CREATE_UPDATE_FN (async) → isAlexaEnabled=true for siteId
  12. 302 redirect → {APP_SCHEME}://alexa/cognito/result?linked=true

Error paths → 302 redirect → {APP_SCHEME}://alexa/cognito/result?linked=false&error={code}

This endpoint has NO Cognito authorizer — it is called by Cognito Hosted UI
redirecting a browser. Auth is via HMAC-signed state + identity binding (step 7).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_module
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import ClientError

try:
    from jose import jwt as jose_jwt
    from jose import jwk as jose_jwk
    from jose.exceptions import JWTError
except ImportError as _imp_exc:
    logger = logging.getLogger()
    logger.error("JWT_VERIFY_UNAVAILABLE python-jose not installed")
    raise RuntimeError("python-jose[cryptography] required for JWT verification") from _imp_exc

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                 = os.environ.get("DATA_REGION",               "")
_PKCE_TABLE             = os.environ.get("PKCE_TABLE",                "")
_LWA_TOKENS_TABLE       = os.environ.get("LWA_TOKENS_TABLE",          "")
_KMS_KEY_ARN            = os.environ.get("KMS_KEY_ARN",               "")
_COGNITO_HOSTED_UI_DOMAIN = os.environ.get("COGNITO_HOSTED_UI_DOMAIN", "")
_COGNITO_ALEXA_CLIENT_ID  = os.environ.get("COGNITO_ALEXA_CLIENT_ID",  "")
_COGNITO_CLIENT_SECRET    = os.environ.get("COGNITO_CLIENT_SECRET",    "")
_COGNITO_CALLBACK_URI     = os.environ.get("COGNITO_CALLBACK_URI",     "")
_COGNITO_USER_POOL_ID     = os.environ.get("COGNITO_USER_POOL_ID",     "")
_COGNITO_REGION           = os.environ.get("COGNITO_REGION",           _REGION or "")
_PKCE_STATE_SECRET_ARN    = os.environ.get("PKCE_STATE_SECRET_ARN",    "")
_PKCE_STATE_SECRET_REGION = os.environ.get("PKCE_STATE_SECRET_REGION", "")
_ALEXA_SKILL_ID           = os.environ.get("ALEXA_SKILL_ID",           "")
_ALEXA_SKILL_STAGE        = os.environ.get("ALEXA_SKILL_STAGE",        "development")
_SKILL_ENABLEMENT_URL     = os.environ.get("SKILL_ENABLEMENT_URL",
                                            "https://api.amazonalexa.com/v1/users/~current/skills")
_CREATE_UPDATE_FN         = os.environ.get("CREATE_UPDATE_FN",         "")
_APP_SCHEME               = os.environ.get("APP_SCHEME",               "")
_HTTP_TIMEOUT             = int(os.environ.get("HTTP_TIMEOUT",         "10"))

_COGNITO_ISSUER = (
    f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    if _COGNITO_USER_POOL_ID else ""
)

_REQUIRED_VARS = {
    "DATA_REGION":            _REGION,
    "PKCE_TABLE":             _PKCE_TABLE,
    "LWA_TOKENS_TABLE":       _LWA_TOKENS_TABLE,
    "KMS_KEY_ARN":            _KMS_KEY_ARN,
    "COGNITO_HOSTED_UI_DOMAIN": _COGNITO_HOSTED_UI_DOMAIN,
    "COGNITO_ALEXA_CLIENT_ID":  _COGNITO_ALEXA_CLIENT_ID,
    "COGNITO_CALLBACK_URI":     _COGNITO_CALLBACK_URI,
    "PKCE_STATE_SECRET_ARN":    _PKCE_STATE_SECRET_ARN,
    "PKCE_STATE_SECRET_REGION": _PKCE_STATE_SECRET_REGION,
    "APP_SCHEME":               _APP_SCHEME,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb               = None
_kms_cache              = None
_lambda_client          = None
_pkce_signing_secret: str | None = None
_jwks_cache: dict | None = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


def _kms():
    global _kms_cache
    if _kms_cache is None:
        _kms_cache = boto3.client("kms", region_name=_REGION)
    return _kms_cache


def _decrypt_field(value: str) -> str:
    if not _KMS_KEY_ARN:
        return value
    ciphertext = base64.b64decode(value.encode())
    return _kms().decrypt(CiphertextBlob=ciphertext)["Plaintext"].decode()


def _get_pkce_signing_secret() -> str:
    global _pkce_signing_secret
    if _pkce_signing_secret is None:
        logger.info("PKCE_SECRET_FETCH arn=%s region=%s (cold start)",
                    _PKCE_STATE_SECRET_ARN, _PKCE_STATE_SECRET_REGION)
        sm  = boto3.client("secretsmanager", region_name=_PKCE_STATE_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_PKCE_STATE_SECRET_ARN)["SecretString"]
        _pkce_signing_secret = json.loads(raw)["signing_secret"]
        logger.info("PKCE_SECRET_LOADED")
    return _pkce_signing_secret


def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        url = f"{_COGNITO_ISSUER}/.well-known/jwks.json"
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as r:
            _jwks_cache = json.loads(r.read())
        logger.info("JWKS_LOADED key_count=%d", len(_jwks_cache.get("keys", [])))
    return _jwks_cache


def _verify_state_and_get_txn_id(signed_state: str) -> str | None:
    """
    Verify HMAC-signed state and return the pkceTransactionId.
    Format: base64url(txn_id).base64url(HMAC-SHA256(signing_secret, txn_id))
    Returns None if the state is malformed or the signature is invalid.
    """
    try:
        parts = signed_state.split(".")
        if len(parts) != 2:
            return None
        # Restore base64url padding
        def _b64d(s: str) -> bytes:
            pad = (4 - len(s) % 4) % 4
            return base64.urlsafe_b64decode(s + "=" * pad)

        txn_id   = _b64d(parts[0]).decode()
        sig_recv = _b64d(parts[1])

        sig_expected = _hmac_module.new(
            _get_pkce_signing_secret().encode(),
            txn_id.encode(),
            hashlib.sha256,
        ).digest()

        if not _hmac_module.compare_digest(sig_expected, sig_recv):
            return None
        return txn_id
    except Exception:
        return None


def _exchange_cognito_code(code: str, verifier: str) -> dict:
    """Exchange Cognito auth code for tokens using PKCE verifier."""
    params = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  _COGNITO_CALLBACK_URI,
        "client_id":     _COGNITO_ALEXA_CLIENT_ID,
        "code_verifier": verifier,
    }
    body = urllib.parse.urlencode(params).encode()
    url  = f"https://{_COGNITO_HOSTED_UI_DOMAIN}/oauth2/token"
    req  = urllib.request.Request(url, data=body,
                                  method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    if _COGNITO_CLIENT_SECRET:
        creds = base64.b64encode(
            f"{_COGNITO_ALEXA_CLIENT_ID}:{_COGNITO_CLIENT_SECRET}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {creds}")

    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read())


def _decode_sub_from_access_token(access_token: str) -> str | None:
    """Decode Cognito access token and return sub. Verifies RS256 signature via JWKS."""
    try:
        jwks = _get_jwks()
        header = jose_jwt.get_unverified_header(access_token)
        kid    = header.get("kid")
        key    = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            logger.warning("JWT_KID_NOT_FOUND kid=%s", kid)
            return None
        public_key = jose_jwk.construct(key)
        claims = jose_jwt.decode(
            access_token,
            public_key,
            algorithms=["RS256"],
            issuer=_COGNITO_ISSUER,
            options={"verify_at_hash": False},
        )
        return claims.get("sub")
    except JWTError as exc:
        logger.warning("JWT_DECODE_FAILED reason=%s", exc)
        return None


def _check_smapi_enablement(lwa_access_token: str, request_id: str) -> bool:
    """
    Enable the skill for the user via SMAPI PUT, making it show as linked in
    the Alexa app. Non-blocking: returns True if enabled, False on failure.
    """
    if not _ALEXA_SKILL_ID or not _SKILL_ENABLEMENT_URL:
        logger.warning("SMAPI_CHECK_SKIPPED missing ALEXA_SKILL_ID or SKILL_ENABLEMENT_URL "
                       "request_id=%s", request_id)
        return False
    url = f"{_SKILL_ENABLEMENT_URL}/{_ALEXA_SKILL_ID}/enablement"
    req = urllib.request.Request(
        url,
        data=b"",
        method="PUT",
        headers={
            "Authorization":  f"Bearer {lwa_access_token}",
            "Content-Type":   "application/json",
            "Content-Length": "0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            logger.info("SMAPI_SKILL_ENABLED skill_id=%s status=%d request_id=%s",
                        _ALEXA_SKILL_ID, r.status, request_id)
            return True
    except urllib.error.HTTPError as exc:
        # 204 is returned as a success by urlopen, but handle edge cases.
        # 400/409 can mean already enabled — treat as success.
        if exc.code in (400, 409):
            logger.info("SMAPI_SKILL_ALREADY_ENABLED skill_id=%s status=%d request_id=%s",
                        _ALEXA_SKILL_ID, exc.code, request_id)
            return True
        logger.warning("SMAPI_ENABLEMENT_HTTP_ERROR status=%d skill_id=%s request_id=%s",
                       exc.code, _ALEXA_SKILL_ID, request_id)
        return False
    except Exception as exc:
        logger.warning("SMAPI_ENABLEMENT_ERROR error=%s request_id=%s", exc, request_id)
        return False


def _notify_alexa_enabled(user_id: str, site_id: str, request_id: str) -> None:
    """Async fire-and-forget: set isAlexaEnabled=true on the site. Non-fatal."""
    global _lambda_client
    if not _CREATE_UPDATE_FN or not site_id:
        logger.warning("ALEXA_ENABLED_NOTIFY_SKIPPED missing CREATE_UPDATE_FN or site_id "
                       "request_id=%s", request_id)
        return
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=_REGION)
    payload = json.dumps({
        "_source":        "alexa_internal",
        "userId":         user_id,
        "siteId":         site_id,
        "isAlexaEnabled": True,
    }).encode()
    try:
        _lambda_client.invoke(
            FunctionName=_CREATE_UPDATE_FN,
            InvocationType="Event",
            Payload=payload,
        )
        logger.info("ALEXA_ENABLED_NOTIFY_SENT fn=%s userId=%s siteId=%s request_id=%s",
                    _CREATE_UPDATE_FN, user_id, site_id, request_id)
    except Exception as exc:
        logger.warning("ALEXA_ENABLED_NOTIFY_FAILED fn=%s userId=%s siteId=%s error=%s "
                       "request_id=%s", _CREATE_UPDATE_FN, user_id, site_id, exc, request_id)


def _get_cognito_result_base() -> str:
    """Derive the HTTPS App Link base URL from COGNITO_CALLBACK_URI host.

    COGNITO_CALLBACK_URI = https://iot.digilux.co.in/smarthome/api/v1/alexa/cognito/callback
    →  returns               https://iot.digilux.co.in/alexa/cognito/result

    Android intercepts this HTTPS App Link (same domain as Stage 1 redirect,
    same assetlinks.json coverage) via onNewIntent — no Play Store involved.
    """
    if not _COGNITO_CALLBACK_URI:
        return ""
    parsed = urllib.parse.urlparse(_COGNITO_CALLBACK_URI)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/alexa/cognito/result"


def _deep_link(linked: bool, error: str | None = None) -> dict:
    """Return an HTTP 302 to the Flutter HTTPS App Link for the Cognito result.

    Chrome Custom Tabs follows the 302; Android intercepts the HTTPS URL as
    an App Link (same domain as the Stage 1 Alexa redirect, covered by the
    same assetlinks.json) and fires onNewIntent on MainActivity — the same
    reliable mechanism used for Stage 1.

    Fallback: if COGNITO_CALLBACK_URI is unset, falls back to a JS redirect
    to the custom scheme (for unit-test / non-Android environments).
    """
    params = {"linked": "true" if linked else "false"}
    if error:
        params["error"] = error
    qs = urllib.parse.urlencode(params)

    base = _get_cognito_result_base()
    if base:
        return {
            "statusCode": 302,
            "headers": {
                "Location":      f"{base}?{qs}",
                "Cache-Control": "no-store, no-cache",
            },
            "body": "",
        }

    # Fallback: custom-scheme JS redirect (non-Android / legacy).
    if not _APP_SCHEME:
        return {"statusCode": 500, "headers": {"Content-Type": "text/plain"}, "body": "Config error"}
    nav_url = f"{_APP_SCHEME}://alexa/cognito/result?{qs}"
    escaped = nav_url.replace("\\", "\\\\").replace('"', '\\"')
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<script>window.onload=function(){{window.location.href=\"{escaped}\";}}</script>"
        "</head><body>Completing Alexa account linking&hellip;</body></html>"
    )
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store, no-cache"},
        "body": html,
    }


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaCognitoCallback request_id=%s", request_id)

    # ── 0. Guard: fail fast on missing config ─────────────────────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _deep_link(False, "config_error")

    # ── 1. Parse query params ─────────────────────────────────────────────────
    qs     = event.get("queryStringParameters") or {}
    code   = qs.get("code", "").strip()
    state  = qs.get("state", "").strip()
    error  = qs.get("error", "").strip()

    if error:
        logger.info("COGNITO_USER_CANCELLED error=%s request_id=%s", error, request_id)
        err_code = "access_denied" if error == "access_denied" else "cognito_error"
        return _deep_link(False, err_code)

    if not code or not state:
        logger.warning("MISSING_PARAMS code_present=%s state_present=%s request_id=%s",
                       bool(code), bool(state), request_id)
        return _deep_link(False, "invalid_request")

    # ── 2. Verify HMAC state → pkceTransactionId ──────────────────────────────
    txn_id = _verify_state_and_get_txn_id(state)
    if not txn_id:
        logger.warning("STATE_INVALID request_id=%s", request_id)
        return _deep_link(False, "invalid_state")

    logger.info("STATE_VERIFIED txnId_prefix=%s request_id=%s", txn_id[:8], request_id)

    # ── 3. Load PKCE transaction ───────────────────────────────────────────────
    now = int(time.time())
    table = _ddb().Table(_PKCE_TABLE)
    try:
        resp = table.get_item(Key={"pkceTransactionId": txn_id})
    except ClientError as exc:
        logger.error("DDB_GET_FAILED table=%s error=%s request_id=%s",
                     _PKCE_TABLE, exc, request_id)
        return _deep_link(False, "server_error")

    txn = resp.get("Item")
    if not txn:
        logger.warning("TXN_NOT_FOUND txnId_prefix=%s request_id=%s", txn_id[:8], request_id)
        return _deep_link(False, "session_expired")

    if txn.get("status") != "PENDING_COGNITO":
        logger.warning("TXN_WRONG_STATUS status=%s txnId_prefix=%s request_id=%s",
                       txn.get("status"), txn_id[:8], request_id)
        return _deep_link(False, "session_already_used")

    if txn.get("expiresAt", 0) < now:
        logger.warning("TXN_EXPIRED txnId_prefix=%s request_id=%s", txn_id[:8], request_id)
        return _deep_link(False, "session_expired")

    user_id  = txn.get("userId", "")
    site_id  = txn.get("siteId", "")
    enc_verifier = txn.get("encryptedVerifier", "")

    if not user_id or not enc_verifier:
        logger.error("TXN_MISSING_FIELDS txnId_prefix=%s request_id=%s", txn_id[:8], request_id)
        return _deep_link(False, "server_error")

    logger.info("TXN_LOADED userId=%s siteId=%s txnId_prefix=%s request_id=%s",
                user_id, site_id, txn_id[:8], request_id)

    # ── 4. Decrypt PKCE verifier ───────────────────────────────────────────────
    try:
        pkce_verifier = _decrypt_field(enc_verifier)
    except Exception as exc:
        logger.error("KMS_DECRYPT_FAILED txnId_prefix=%s error=%s request_id=%s",
                     txn_id[:8], exc, request_id)
        return _deep_link(False, "server_error")

    # ── 5. Exchange Cognito auth code for tokens ───────────────────────────────
    try:
        tokens = _exchange_cognito_code(code, pkce_verifier)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode() if exc.fp else ""
        logger.error("COGNITO_EXCHANGE_HTTP_ERROR status=%d body=%s request_id=%s",
                     exc.code, body, request_id)
        return _deep_link(False, "cognito_exchange_failed")
    except Exception as exc:
        logger.error("COGNITO_EXCHANGE_FAILED error=%s request_id=%s", exc, request_id)
        return _deep_link(False, "cognito_exchange_failed")

    access_token = tokens.get("access_token", "")
    if not access_token:
        logger.error("COGNITO_NO_ACCESS_TOKEN request_id=%s", request_id)
        return _deep_link(False, "cognito_exchange_failed")

    logger.info("COGNITO_EXCHANGE_OK request_id=%s", request_id)

    # ── 6. Decode Cognito access token → sub ──────────────────────────────────
    cognito_sub = _decode_sub_from_access_token(access_token)
    if not cognito_sub:
        logger.error("COGNITO_SUB_MISSING request_id=%s", request_id)
        return _deep_link(False, "identity_verification_failed")

    # ── 7. Identity binding check ─────────────────────────────────────────────
    # The Cognito sub MUST match the userId from the PKCE transaction.
    # This proves the user who authorized the LWA app-to-app linking is the
    # SAME user who just logged in with Cognito — prevents cross-account linking.
    if cognito_sub != user_id:
        logger.error(
            "[SECURITY] IDENTITY_BINDING_FAILED cognito_sub_prefix=%s "
            "txn_userId_prefix=%s txnId_prefix=%s request_id=%s",
            cognito_sub[:8], user_id[:8], txn_id[:8], request_id,
        )
        return _deep_link(False, "identity_mismatch")

    logger.info("[AUDIT] IDENTITY_BINDING_OK userId=%s txnId_prefix=%s request_id=%s",
                user_id, txn_id[:8], request_id)

    # ── 8. Retrieve LWA token for SMAPI check ────────────────────────────────
    smapi_verified = False
    try:
        lwa_resp = _ddb().Table(_LWA_TOKENS_TABLE).get_item(Key={"userId": user_id})
        lwa_item = lwa_resp.get("Item", {})
        lwa_expires_at = int(lwa_item.get("expiresAt", 0))
        if lwa_item and lwa_expires_at > now:
            lwa_access_token = _decrypt_field(lwa_item["accessToken"])
            # ── 9. SMAPI skill enablement (PUT) ──────────────────────────────
            smapi_verified = _check_smapi_enablement(lwa_access_token, request_id)
            if not smapi_verified:
                logger.warning("SMAPI_ENABLE_FAILED — proceeding (identity binding already confirmed) "
                               "userId=%s request_id=%s", user_id, request_id)
        else:
            logger.warning("LWA_TOKEN_EXPIRED_OR_MISSING userId=%s — skipping SMAPI check "
                           "request_id=%s", user_id, request_id)
    except Exception as exc:
        logger.warning("SMAPI_CHECK_ERROR error=%s userId=%s request_id=%s",
                       exc, user_id, request_id)

    # ── 10. Mark PKCE transaction COGNITO_VERIFIED + release per-user lock ──────
    lock_key = f"LOCK#{user_id}"
    try:
        table.update_item(
            Key={"pkceTransactionId": txn_id},
            UpdateExpression="SET #st = :v, verifiedAt = :t",
            ConditionExpression="#st = :pending",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":v":       "COGNITO_VERIFIED",
                ":t":       now,
                ":pending": "PENDING_COGNITO",
            },
        )
        logger.info("TXN_VERIFIED txnId_prefix=%s userId=%s request_id=%s",
                    txn_id[:8], user_id, request_id)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("TXN_ALREADY_USED txnId_prefix=%s request_id=%s",
                           txn_id[:8], request_id)
            # Race: another request already verified this transaction.
            # Still deep-link success — the user IS linked.
        else:
            logger.error("TXN_UPDATE_FAILED error=%s request_id=%s", exc, request_id)
            return _deep_link(False, "server_error")

    # Release the per-user lock so the user can re-link immediately if needed.
    try:
        table.delete_item(Key={"pkceTransactionId": lock_key})
        logger.info("PKCE_LOCK_RELEASED userId=%s request_id=%s", user_id, request_id)
    except Exception as exc:
        logger.warning("PKCE_LOCK_RELEASE_FAILED userId=%s error=%s request_id=%s",
                       user_id, exc, request_id)

    # ── 11. Fire CREATE_UPDATE_FN → isAlexaEnabled=true ──────────────────────
    _notify_alexa_enabled(user_id, site_id, request_id)

    logger.info("[AUDIT] ALEXA_LINKED userId=%s siteId=%s smapi_verified=%s "
                "txnId_prefix=%s request_id=%s",
                user_id, site_id, smapi_verified, txn_id[:8], request_id)

    # ── 12. Redirect to Flutter ───────────────────────────────────────────────
    return _deep_link(True)
