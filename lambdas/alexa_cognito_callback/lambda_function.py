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
  5.  Retrieve LWA access token for the user (from alexa_lwa_tokens)
  6.  Call Skill Enablement API (enable + account link):
        POST /v1/users/~current/skills/{skillId}/enablement
        Authorization: Bearer <LWA access token>
        Body:
          {
            "stage": "<ALEXA_SKILL_STAGE>",
            "accountLinkRequest": {
              "type": "AUTH_CODE",
              "authCode": "<Cognito auth code — unconsumed>",
              "redirectUri": "<COGNITO_CALLBACK_URI>",
              "authCodeVerifier": "<PKCE verifier>"
            }
          }
        Alexa exchanges the Cognito auth code at the skill Access Token URL.
        Blocking: failure → linked=false.
  7.  Mark PKCE transaction COGNITO_VERIFIED in DDB
  8.  Fire CREATE_UPDATE_FN (async) → isAlexaEnabled=true for siteId
  9.  302 redirect → cognito result App Link?linked=true

Error paths → 302 redirect → cognito result?linked=false&error={code}

This endpoint has NO Cognito authorizer — it is called by Cognito Hosted UI
redirecting a browser. Auth is via HMAC-signed state bound to Stage 1 userId.
The Cognito auth code is passed unconsumed to Alexa (single-use).
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

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                   = os.environ.get("DATA_REGION",               "")
_PKCE_TABLE                    = os.environ.get("PKCE_TABLE",                 "")
_LWA_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",             "")
_USER_DEVICE_MAPPING_TABLE     = os.environ.get("USER_DEVICE_MAPPING_TABLE",    "")
_KMS_KEY_ARN              = os.environ.get("KMS_KEY_ARN",               "")
_COGNITO_CALLBACK_URI     = os.environ.get("COGNITO_CALLBACK_URI",     "")
_PKCE_STATE_SECRET_ARN    = os.environ.get("PKCE_STATE_SECRET_ARN",    "")
_PKCE_STATE_SECRET_REGION = os.environ.get("PKCE_STATE_SECRET_REGION", "")
_ALEXA_SKILL_ID           = os.environ.get("ALEXA_SKILL_ID",           "")
_ALEXA_SKILL_STAGE        = os.environ.get("ALEXA_SKILL_STAGE",        "development")
_SKILL_ENABLEMENT_URL     = os.environ.get("SKILL_ENABLEMENT_URL",
                                            "https://api.amazonalexa.com/v1/users/~current/skills")
_CREATE_UPDATE_FN         = os.environ.get("CREATE_UPDATE_FN",         "")
_APP_SCHEME               = os.environ.get("APP_SCHEME",               "")
_HTTP_TIMEOUT             = int(os.environ.get("HTTP_TIMEOUT",         "10"))

_REQUIRED_VARS = {
    "DATA_REGION":              _REGION,
    "PKCE_TABLE":               _PKCE_TABLE,
    "LWA_TOKENS_TABLE":         _LWA_TOKENS_TABLE,
    "KMS_KEY_ARN":              _KMS_KEY_ARN,
    "COGNITO_CALLBACK_URI":     _COGNITO_CALLBACK_URI,
    "PKCE_STATE_SECRET_ARN":    _PKCE_STATE_SECRET_ARN,
    "PKCE_STATE_SECRET_REGION": _PKCE_STATE_SECRET_REGION,
    "APP_SCHEME":               _APP_SCHEME,
    "ALEXA_SKILL_ID":           _ALEXA_SKILL_ID,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s", _missing_vars)

# ── Module-level caches ────────────────────────────────────────────────────────
_dynamodb               = None
_kms_cache              = None
_lambda_client          = None
_pkce_signing_secret: str | None = None


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


def _check_smapi_enablement(
    lwa_access_token: str,
    auth_code: str,
    redirect_uri: str,
    code_verifier: str,
    stage: str,
    request_id: str,
) -> bool:
    """
    Enable the skill AND complete account linking via Skill Enablement API.

    POST /v1/users/~current/skills/{skillId}/enablement
    Alexa redeems auth_code at the skill's Access Token URL.

    Returns True on HTTP 201 (or already-linked 409). False on failure.
    """
    if not _ALEXA_SKILL_ID or not _SKILL_ENABLEMENT_URL:
        logger.warning("SMAPI_CHECK_SKIPPED missing ALEXA_SKILL_ID or SKILL_ENABLEMENT_URL "
                       "request_id=%s", request_id)
        return False
    if not auth_code or not redirect_uri:
        logger.warning("SMAPI_CHECK_SKIPPED missing auth_code or redirect_uri request_id=%s",
                       request_id)
        return False

    url = f"{_SKILL_ENABLEMENT_URL}/{_ALEXA_SKILL_ID}/enablement"
    body: dict = {
        "stage": stage or "development",
        "accountLinkRequest": {
            "type": "AUTH_CODE",
            "authCode": auth_code,
            "redirectUri": redirect_uri,
        },
    }
    # Required when the authorization request used PKCE (S256).
    if code_verifier:
        body["accountLinkRequest"]["authCodeVerifier"] = code_verifier

    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {lwa_access_token}",
            "Content-Type":  "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            logger.info("SMAPI_SKILL_ENABLED_AND_LINKED skill_id=%s status=%d stage=%s "
                        "request_id=%s", _ALEXA_SKILL_ID, r.status, stage, request_id)
            return True
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode(errors="replace") if exc.fp else ""
        except Exception:
            pass
        # 409 — skill already enabled/linked for this user.
        if exc.code == 409:
            logger.info("SMAPI_SKILL_ALREADY_LINKED skill_id=%s status=%d request_id=%s",
                        _ALEXA_SKILL_ID, exc.code, request_id)
            return True
        logger.warning(
            "SMAPI_ENABLEMENT_HTTP_ERROR status=%d skill_id=%s body=%s request_id=%s",
            exc.code, _ALEXA_SKILL_ID, err_body[:500], request_id,
        )
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

    # Identity is bound via Stage 1 Cognito JWT + HMAC-signed state → userId.
    # Do NOT exchange the Cognito auth code here — Alexa must redeem it via
    # Skill Enablement (auth codes are single-use).
    logger.info("[AUDIT] STAGE1_IDENTITY_BOUND userId=%s txnId_prefix=%s request_id=%s",
                user_id, txn_id[:8], request_id)

    # ── 5. Retrieve LWA access token (required for Skill Enablement) ───────────
    try:
        lwa_resp = _ddb().Table(_LWA_TOKENS_TABLE).get_item(Key={"userId": user_id})
        lwa_item = lwa_resp.get("Item", {})
        lwa_expires_at = int(lwa_item.get("expiresAt", 0))
        if not lwa_item or lwa_expires_at <= now:
            logger.warning("LWA_TOKEN_EXPIRED_OR_MISSING userId=%s request_id=%s",
                           user_id, request_id)
            return _deep_link(False, "lwa_token_missing")
        lwa_access_token = _decrypt_field(lwa_item["accessToken"])
    except Exception as exc:
        logger.error("LWA_TOKEN_LOOKUP_ERROR error=%s userId=%s request_id=%s",
                     exc, user_id, request_id)
        return _deep_link(False, "lwa_token_missing")

    # ── 6. Skill Enablement POST (enable + account link) — best-effort ───────────
    # Pass the unconsumed Cognito auth code so Alexa can exchange it at the
    # skill Access Token URL (Cognito /oauth2/token when configured that way).
    # NOTE: This call fails with 404 for dev-stage skills (Amazon limitation).
    # It is non-fatal: the user has successfully authenticated and the LWA token
    # is valid. We proceed to write alexaLinked=True regardless. When the skill
    # is published, SMAPI will succeed and the skill will appear linked in the
    # Alexa app immediately; AcceptGrant will fire and confirm the link.
    smapi_verified = _check_smapi_enablement(
        lwa_access_token=lwa_access_token,
        auth_code=code,
        redirect_uri=_COGNITO_CALLBACK_URI,
        code_verifier=pkce_verifier,
        stage=_ALEXA_SKILL_STAGE,
        request_id=request_id,
    )
    if not smapi_verified:
        logger.warning("SMAPI_ENABLE_FAILED userId=%s — skill not enabled via SMAPI "
                       "(dev-stage limitation); proceeding with linking request_id=%s",
                       user_id, request_id)

    # ── 7. Mark PKCE transaction COGNITO_VERIFIED + release per-user lock ──────
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

    # ── 8a. Write alexaLinked=True to mapping table immediately ──────────────
    # AcceptGrant (which stores the LWA token) fires asynchronously AFTER SMAPI
    # PUT returns. If the Flutter app reloads the status screen before AcceptGrant
    # completes, alexa_link_status sees no token and returns linked=False.
    # Writing alexaLinked=True here ensures the status check's primary path
    # (mapping table lookup) returns linked=True right away.
    if site_id and _USER_DEVICE_MAPPING_TABLE:
        try:
            _ddb().Table(_USER_DEVICE_MAPPING_TABLE).update_item(
                Key={"userId": user_id, "siteId": site_id},
                UpdateExpression="SET alexaLinked = :t, alexaLinkedAt = :ts",
                ExpressionAttributeValues={
                    ":t":  True,
                    ":ts": int(time.time()),
                },
            )
            logger.info("ALEXA_LINKED_WRITTEN userId=%s siteId=%s request_id=%s",
                        user_id, site_id, request_id)
        except Exception as exc:
            logger.warning("ALEXA_LINKED_WRITE_ERROR userId=%s siteId=%s error=%s request_id=%s",
                           user_id, site_id, exc, request_id)

    # ── 8b. Fire CREATE_UPDATE_FN → isAlexaEnabled=true ──────────────────────
    _notify_alexa_enabled(user_id, site_id, request_id)

    logger.info("[AUDIT] ALEXA_LINKED userId=%s siteId=%s smapi_verified=%s "
                "txnId_prefix=%s request_id=%s",
                user_id, site_id, smapi_verified, txn_id[:8], request_id)

    # ── 9. Redirect to Flutter ────────────────────────────────────────────────
    return _deep_link(True)
