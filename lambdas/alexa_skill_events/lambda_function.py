"""
alexa_skill_events — POST /alexa/skill-event

Handles Alexa skill lifecycle events sent by Amazon.

Supported events:
  AlexaSkillEvent.SkillDisabled — user disabled the Digilux skill from the Alexa app.
    Action: delete LWA tokens from digilux_honeywell_alexa_lwa_tokens,
            set alexaLinked=false on all user sites in digilux_honeywell_user_device_mapping.

  AlexaSkillEvent.SkillEnabled / SkillAccountLinked — logged only; linking is handled
    by our own completeAppToApp flow.

Security:
  Verifies the SignatureCertChainUrl header comes from Amazon's certificate endpoint.
  Full RSA signature verification can be added by installing the `cryptography` package.

User ID mapping:
  The event contains context.System.user.userId (Amazon LWA user ID, format
  amzn1.account.xxx). This is stored in the tokens table as amazonUserId during
  completeAppToApp (after calling the LWA Profile API). A GSI (amazonUserId-index)
  enables O(1) lookup.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION",                "")
_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",           "")
_USER_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",  "")
_ALEXA_SKILL_ID            = os.environ.get("ALEXA_SKILL_ID",             "")

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "LWA_TOKENS_TABLE": _TOKENS_TABLE,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)

# Amazon cert URL pattern — all valid Alexa signing cert URLs match this
_AMAZON_CERT_URL_RE = re.compile(
    r"^https://s3\.amazonaws\.com/echo\.api/.*\.pem"
    r"|^https://s3[.-][\w-]+\.amazonaws\.com/echo\.api/.*\.pem"
    r"|^https://s3\.amazonaws\.com\.cn/echo\.api/.*\.pem",
    re.IGNORECASE,
)

# Module-level caches
_dynamodb = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    return _dynamodb


def _resp(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


def _verify_request_source(event: dict) -> None:
    """
    Verify the request genuinely comes from Amazon Alexa.

    Checks:
      1. SignatureCertChainUrl header is present and points to Amazon's S3 bucket.
      2. Signature header is present (non-empty).

    Full RSA body-signature verification requires the `cryptography` package:
      pip install cryptography
    Add it to requirements.txt and uncomment the crypto block below to enable it.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    cert_url = headers.get("signaturecertchainurl", "")
    signature = headers.get("signature", "")

    if not cert_url:
        raise ValueError("Missing SignatureCertChainUrl header")
    if not signature:
        raise ValueError("Missing Signature header")
    if not _AMAZON_CERT_URL_RE.match(cert_url):
        raise ValueError(f"SignatureCertChainUrl not from Amazon: {cert_url}")

    # ── Optional: full cryptographic verification ──────────────────────────────
    # Uncomment and add `cryptography` to requirements.txt for production hardening.
    #
    # import base64
    # from cryptography import x509
    # from cryptography.hazmat.primitives import hashes, serialization
    # from cryptography.hazmat.primitives.asymmetric import padding
    # from cryptography.exceptions import InvalidSignature
    #
    # with urllib.request.urlopen(cert_url, timeout=5) as r:
    #     cert_pem = r.read()
    # cert = x509.load_pem_x509_certificate(cert_pem)
    # public_key = cert.public_key()
    # raw_body = (event.get("body") or "").encode()
    # sig_bytes = base64.b64decode(signature)
    # try:
    #     public_key.verify(sig_bytes, raw_body, padding.PKCS1v15(), hashes.SHA1())
    # except InvalidSignature as exc:
    #     raise ValueError(f"RSA body signature invalid: {exc}") from exc
    # ──────────────────────────────────────────────────────────────────────────


def _handle_skill_disabled(alexa_user_id: str, skill_id: str, request_id: str) -> None:
    """
    Clean up after a user disables the Digilux Alexa skill.

    Steps:
      1. Look up the Cognito userId via the amazonUserId GSI on the tokens table.
      2. Delete the LWA token record.
      3. Set alexaLinked=false on all of the user's sites in user_device_mapping.
    """
    tokens_table = _ddb().Table(_TOKENS_TABLE)

    # ── 1. Find Cognito userId ────────────────────────────────────────────────
    # SKILL_DISABLED events from Alexa use amzn1.ask.account.XXX format (Alexa
    # Customer ID). Try alexaCustomerId-index first, then fall back to the LWA
    # format (amzn1.account.XXX) via amazonUserId-index.
    items = []

    if alexa_user_id.startswith("amzn1.ask.account."):
        logger.debug("GSI_LOOKUP indexName=alexaCustomerId-index alexa_user_id=%s request_id=%s",
                     alexa_user_id, request_id)
        result = tokens_table.query(
            IndexName="alexaCustomerId-index",
            KeyConditionExpression=Key("alexaCustomerId").eq(alexa_user_id),
            Limit=1,
        )
        items = result.get("Items", [])

    if not items:
        logger.debug("GSI_LOOKUP indexName=amazonUserId-index alexa_user_id=%s request_id=%s",
                     alexa_user_id, request_id)
        result = tokens_table.query(
            IndexName="amazonUserId-index",
            KeyConditionExpression=Key("amazonUserId").eq(alexa_user_id),
            Limit=1,
        )
        items = result.get("Items", [])

    if not items:
        logger.warning(
            "SKILL_DISABLED_USER_NOT_FOUND alexaUserId=%s request_id=%s — "
            "user may not have linked via this system",
            alexa_user_id, request_id,
        )
        return  # Not an error — Alexa may fire events for non-linked users

    user_id = items[0]["userId"]
    logger.info("SKILL_DISABLED_USER_FOUND userId=%s alexaUserId=%s request_id=%s",
                user_id, alexa_user_id, request_id)

    # ── 2. Delete LWA token record ────────────────────────────────────────────
    tokens_table.delete_item(Key={"userId": user_id})
    logger.info("TOKEN_DELETED userId=%s request_id=%s", user_id, request_id)

    # ── 3. Set alexaLinked=false on all user sites ────────────────────────────
    if _USER_DEVICE_MAPPING_TABLE:
        mapping_table = _ddb().Table(_USER_DEVICE_MAPPING_TABLE)

        # Query all sites for this user (PK=userId, SK=siteId)
        site_result = mapping_table.query(
            KeyConditionExpression=Key("userId").eq(user_id),
        )
        sites = site_result.get("Items", [])

        for site in sites:
            site_id = site.get("siteId")
            if not site_id:
                continue
            mapping_table.update_item(
                Key={"userId": user_id, "siteId": site_id},
                UpdateExpression="SET alexaLinked = :f REMOVE alexaLinkedAt",
                ExpressionAttributeValues={":f": False},
            )
            logger.info("SITE_UNLINKED userId=%s siteId=%s request_id=%s",
                        user_id, site_id, request_id)

        logger.info("ALL_SITES_UNLINKED userId=%s site_count=%d request_id=%s",
                    user_id, len(sites), request_id)
    else:
        logger.warning("USER_DEVICE_MAPPING_TABLE not set — site flags not updated "
                       "userId=%s request_id=%s", user_id, request_id)

    # AUDIT
    logger.info(
        "[AUDIT] ALEXA_INITIATED_UNLINK userId=%s alexaUserId=%s skill_id=%s request_id=%s",
        user_id, alexa_user_id, skill_id, request_id,
    )


def _handle_skill_account_linked(alexa_user_id: str, skill_id: str, request_id: str) -> None:
    """
    Handle AlexaSkillEvent.SkillAccountLinked — fired when a user links their
    Amazon account to the Digilux skill (either via Alexa app or App-to-App flow).

    Steps:
      1. Look up the Cognito userId via the amazonUserId GSI on the tokens table.
      2. Set alexaLinked=true on all of the user's sites in user_device_mapping.

    Note: this only works if the user has previously linked via the App-to-App
    flow (which stores amazonUserId). Users who link exclusively via the Alexa
    app will not have a GSI record and cannot be looked up.
    """
    tokens_table = _ddb().Table(_TOKENS_TABLE)

    # ── 1. Find Cognito userId via amazonUserId GSI ───────────────────────────
    result = tokens_table.query(
        IndexName="amazonUserId-index",
        KeyConditionExpression=Key("amazonUserId").eq(alexa_user_id),
        Limit=1,
    )
    items = result.get("Items", [])

    if not items:
        logger.warning(
            "SKILL_ACCOUNT_LINKED_USER_NOT_FOUND alexaUserId=%s request_id=%s — "
            "no App-to-App token record; cannot map to Cognito user",
            alexa_user_id, request_id,
        )
        return

    user_id = items[0]["userId"]
    logger.info(
        "SKILL_ACCOUNT_LINKED_USER_FOUND userId=%s alexaUserId=%s request_id=%s",
        user_id, alexa_user_id, request_id,
    )

    # ── 2. Set alexaLinked=true on all user sites ─────────────────────────────
    if _USER_DEVICE_MAPPING_TABLE:
        mapping_table = _ddb().Table(_USER_DEVICE_MAPPING_TABLE)
        linked_at = int(time.time())

        site_result = mapping_table.query(
            KeyConditionExpression=Key("userId").eq(user_id),
        )
        sites = site_result.get("Items", [])

        for site in sites:
            site_id = site.get("siteId")
            if not site_id:
                continue
            mapping_table.update_item(
                Key={"userId": user_id, "siteId": site_id},
                UpdateExpression="SET alexaLinked = :t, alexaLinkedAt = :ts",
                ExpressionAttributeValues={":t": True, ":ts": linked_at},
            )
            logger.info("SITE_LINKED userId=%s siteId=%s request_id=%s",
                        user_id, site_id, request_id)

        logger.info("ALL_SITES_LINKED userId=%s site_count=%d request_id=%s",
                    user_id, len(sites), request_id)
    else:
        logger.warning("USER_DEVICE_MAPPING_TABLE not set — site flags not updated "
                       "userId=%s request_id=%s", user_id, request_id)

    # AUDIT
    logger.info(
        "[AUDIT] ALEXA_APP_LINKED userId=%s alexaUserId=%s skill_id=%s request_id=%s",
        user_id, alexa_user_id, skill_id, request_id,
    )


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaSkillEvents request_id=%s", request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Verify the request comes from Amazon ───────────────────────────────
    try:
        _verify_request_source(event)
    except ValueError as exc:
        logger.warning("SIGNATURE_INVALID reason=%s request_id=%s", exc, request_id)
        return _resp(400, {"error": "Invalid request"})

    # ── 2. Parse body ─────────────────────────────────────────────────────────
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.warning("BODY_PARSE_FAILED request_id=%s", request_id)
        return _resp(400, {"error": "Invalid JSON body"})

    # ── 3a. Alexa.Authorization.Error / SKILL_DISABLED (SMAPI format) ─────────
    # Amazon delivers this format to the skill events endpoint when a user
    # disables the skill from the Alexa app. Structure:
    # { "header": { "namespace": "Alexa.Authorization", "name": "Error", ... },
    #   "event":  { "type": "SKILL_DISABLED", "userId": "amzn1.ask.account.XXX" } }
    # This has no context.System, so it must be handled before the skill-ID check.
    top_header = body.get("header") or {}
    if (top_header.get("namespace") == "Alexa.Authorization"
            and top_header.get("name") == "Error"):
        ev = body.get("event") or {}
        event_type    = ev.get("type", "")
        alexa_user_id = ev.get("userId", "")
        logger.info("ALEXA_AUTHORIZATION_ERROR type=%s alexaUserId=%s request_id=%s",
                    event_type, alexa_user_id, request_id)
        if event_type == "SKILL_DISABLED":
            if alexa_user_id:
                try:
                    _handle_skill_disabled(alexa_user_id, "", request_id)
                except Exception as exc:
                    logger.error("SKILL_DISABLED_HANDLER_ERROR error=%s request_id=%s",
                                 exc, request_id)
            else:
                logger.warning("SKILL_DISABLED_NO_USER_ID request_id=%s", request_id)
        else:
            logger.info("UNHANDLED_AUTHORIZATION_ERROR_TYPE type=%s request_id=%s",
                        event_type, request_id)
        logger.info("REQUEST_OK function=alexaSkillEvents request_id=%s", request_id)
        return _resp(200, {"received": True})

    # ── 3b. Validate skill application ID ─────────────────────────────────────
    skill_id = (body.get("context") or {}).get("System", {}).get(
        "application", {}).get("applicationId", "")
    if _ALEXA_SKILL_ID and skill_id != _ALEXA_SKILL_ID:
        logger.warning("SKILL_ID_MISMATCH expected=%s got=%s request_id=%s",
                       _ALEXA_SKILL_ID, skill_id, request_id)
        return _resp(400, {"error": "Invalid skill ID"})

    # ── 4. Extract event type and user ────────────────────────────────────────
    alexa_user_id  = (body.get("context") or {}).get("System", {}).get(
        "user", {}).get("userId", "")
    request_type   = (body.get("request") or {}).get("type", "")

    logger.info("SKILL_EVENT type=%s alexaUserId=%s skill_id=%s request_id=%s",
                request_type, alexa_user_id, skill_id, request_id)

    # ── 5. Route by event type ────────────────────────────────────────────────
    if request_type == "AlexaSkillEvent.SkillDisabled":
        if not alexa_user_id:
            logger.warning("SKILL_DISABLED_NO_USER_ID request_id=%s", request_id)
        else:
            try:
                _handle_skill_disabled(alexa_user_id, skill_id, request_id)
            except Exception as exc:
                logger.error("SKILL_DISABLED_HANDLER_ERROR error=%s request_id=%s",
                             exc, request_id)
                # Still return 200 — Amazon does not retry on 5xx for skill events
                # Log the error and investigate manually

    elif request_type == "AlexaSkillEvent.SkillEnabled":
        logger.info("SKILL_ENABLED alexaUserId=%s — linking handled by completeAppToApp "
                    "request_id=%s", alexa_user_id, request_id)

    elif request_type == "AlexaSkillEvent.SkillAccountLinked":
        if not alexa_user_id:
            logger.warning("SKILL_ACCOUNT_LINKED_NO_USER_ID request_id=%s", request_id)
        else:
            try:
                _handle_skill_account_linked(alexa_user_id, skill_id, request_id)
            except Exception as exc:
                logger.error("SKILL_ACCOUNT_LINKED_HANDLER_ERROR error=%s request_id=%s",
                             exc, request_id)

    else:
        logger.info("UNHANDLED_EVENT_TYPE type=%s request_id=%s", request_type, request_id)

    # Amazon expects HTTP 200 — always return 200 to prevent retries
    logger.info("REQUEST_OK function=alexaSkillEvents request_id=%s", request_id)
    return _resp(200, {"received": True})
