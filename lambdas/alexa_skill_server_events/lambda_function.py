"""
alexa_skill_server_events — POST /alexa/skill-event

Handles Alexa skill lifecycle events sent by Amazon.

Supported events:
  AlexaSkillEvent.SkillAccountLinked — user linked their Alexa account to Digilux.
    Action: validate the linked-account Cognito JWT via JWKS, store alexaCustomerId
            (amzn1.ask.account.xxx) on the token record, set alexaLinked=true on all
            user sites. No state change if the token is absent or fails validation.

  AlexaSkillEvent.SkillDisabled — user disabled the Digilux skill from the Alexa app.
    Action: look up Cognito userId via alexaCustomerId-index GSI, apply stale-event
            guard (event older than current linkedAt is silently dropped), then delete
            LWA tokens and set alexaLinked=false on all user sites.

  AlexaSkillEvent.SkillEnabled — logged only; linking is handled by completeAppToApp.

Security — Amazon request verification (fail closed on any check):
  1. SignatureCertChainUrl header present and from Amazon's S3 bucket.
  2. Certificate chain: each cert within validity window, leaf SAN contains
     echo-api.amazon.com, each cert's signature verified against the next issuer.
  3. RSA-SHA1 body signature verified against the leaf certificate's public key.
     (SHA1 is mandated by Amazon's Alexa signing spec; used for verification only.)
  4. Request timestamp within ±150 seconds of server time (replay attack prevention).

User ID mapping:
  SkillAccountLinked provides context.System.user.accessToken (Cognito JWT).
  Validated via JWKS → sub = Cognito userId → alexaCustomerId stored on token record.
  SkillDisabled looks up by alexaCustomerId-index (amzn1.ask.account.xxx),
  falls back to amazonUserId-index (amzn1.account.xxx) for pre-existing records.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from jose import jwt as _jose_jwt
from jose.exceptions import JWTError

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))

# ── Environment variables ──────────────────────────────────────────────────────
_REGION                    = os.environ.get("DATA_REGION",                "")
_TOKENS_TABLE              = os.environ.get("LWA_TOKENS_TABLE",           "")
_USER_DEVICE_MAPPING_TABLE = os.environ.get("USER_DEVICE_MAPPING_TABLE",  "")
_ALEXA_SKILL_ID            = os.environ.get("ALEXA_SKILL_ID",             "")
_COGNITO_USER_POOL_ID      = os.environ.get("COGNITO_USER_POOL_ID",       "")
_COGNITO_REGION            = os.environ.get("COGNITO_REGION",             "")

# ── Startup config validation ──────────────────────────────────────────────────
_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "LWA_TOKENS_TABLE": _TOKENS_TABLE,
}
_missing_vars = [k for k, v in _REQUIRED_VARS.items() if not v]
if _missing_vars:
    logger.error("CONFIG_STARTUP_ERROR missing_required_env=%s — "
                 "invocations will return HTTP 500", _missing_vars)

if not _COGNITO_USER_POOL_ID or not _COGNITO_REGION:
    logger.warning("COGNITO_VARS_MISSING COGNITO_USER_POOL_ID or COGNITO_REGION not set — "
                   "SkillAccountLinked token validation unavailable; "
                   "alexaCustomerId cannot be established")

# Amazon cert URL pattern — all valid Alexa signing cert URLs match this
_AMAZON_CERT_URL_RE = re.compile(
    r"^https://s3\.amazonaws\.com/echo\.api/.*\.pem"
    r"|^https://s3[.-][\w-]+\.amazonaws\.com/echo\.api/.*\.pem"
    r"|^https://s3\.amazonaws\.com\.cn/echo\.api/.*\.pem",
    re.IGNORECASE,
)

# PEM certificate block matcher for chain parsing
_PEM_CERT_RE = re.compile(
    b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)

# Module-level caches
_dynamodb = None
_cert_cache:        dict[str, dict] = {}  # url -> {"chain": [x509, ...], "expires": float}
_jwks_cache:        dict | None     = None
_jwks_cache_expires: float          = 0.0


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


def _parse_iso_timestamp(ts_str: str) -> int:
    """Parse ISO 8601 timestamp to epoch seconds. Returns 0 on failure."""
    if not ts_str:
        return 0
    try:
        normalized = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp())
    except (ValueError, AttributeError, TypeError):
        return 0


# ── Amazon request verification ────────────────────────────────────────────────

def _parse_cert_chain(pem_data: bytes) -> list:
    """Extract all X.509 certificates from a PEM-encoded chain. Raises if none found."""
    certs = [
        x509.load_pem_x509_certificate(match.group(0))
        for match in _PEM_CERT_RE.finditer(pem_data)
    ]
    if not certs:
        raise ValueError("No certificates found in PEM data from Amazon")
    return certs


def _get_amazon_cert_chain(cert_url: str) -> list:
    """
    Download and cache Amazon's signing certificate chain.
    Returns list of x509 objects: [leaf, intermediate?, ...].
    Cached at module level; expires at the soonest cert's not_valid_after (max 1 hour).
    """
    cached = _cert_cache.get(cert_url)
    if cached and cached["expires"] > time.time() + 60:
        return cached["chain"]

    try:
        with urllib.request.urlopen(cert_url, timeout=5) as r:
            pem_data = r.read()
    except Exception as exc:
        raise ValueError(f"Failed to download Amazon cert chain from {cert_url!r}: {exc}") from exc

    certs = _parse_cert_chain(pem_data)
    min_expiry  = min(c.not_valid_after_utc.timestamp() for c in certs)
    cache_until = min(min_expiry, time.time() + 3600)
    _cert_cache[cert_url] = {"chain": certs, "expires": cache_until}
    return certs


def _verify_cert_chain(certs: list) -> None:
    """
    Validate the certificate chain:
      1. Every cert is within its validity window.
      2. The leaf cert's SAN contains echo-api.amazon.com (Amazon's requirement).
      3. Each cert's signature validates against the next cert's public key.
         (Verifies chain integrity back to the trust anchor Amazon provided.)
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Validity window — every cert in the chain
    for i, cert in enumerate(certs):
        if now_utc < cert.not_valid_before_utc or now_utc > cert.not_valid_after_utc:
            raise ValueError(f"Certificate {i} in chain is outside its validity window")

    # 2. Leaf cert SAN must contain echo-api.amazon.com
    leaf = certs[0]
    try:
        san_ext   = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        dns_names = san_ext.value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound as exc:
        raise ValueError("Leaf certificate has no SubjectAlternativeName extension") from exc

    if "echo-api.amazon.com" not in dns_names:
        raise ValueError(
            f"Leaf certificate SAN does not contain echo-api.amazon.com: {dns_names}"
        )

    # 3. Chain integrity: cert[i] must be signed by cert[i+1]
    for i in range(len(certs) - 1):
        subject = certs[i]
        issuer  = certs[i + 1]
        try:
            issuer.public_key().verify(
                subject.signature,
                subject.tbs_certificate_bytes,
                padding.PKCS1v15(),
                subject.signature_hash_algorithm,
            )
        except InvalidSignature as exc:
            raise ValueError(
                f"Certificate chain broken at position {i}: issuer signature invalid"
            ) from exc
        except Exception as exc:
            raise ValueError(f"Certificate chain validation error at position {i}: {exc}") from exc


def _verify_request_source(event: dict) -> None:
    """
    Verify the request genuinely comes from Amazon Alexa. Fail closed on any failure.

    Steps:
      1. SignatureCertChainUrl present and from Amazon's S3 bucket.
      2. Signature header present.
      3. Request timestamp within ±150 seconds (replay attack prevention).
         Checked before cert download to fail fast on obvious replays.
      4. Certificate chain: downloaded, all certs valid, SAN checked, chain sigs verified.
      5. RSA-SHA1 body signature verified against the leaf cert's public key.
         SHA1 is mandated by Amazon's Alexa signing specification for the body hash.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    cert_url      = headers.get("signaturecertchainurl", "")
    signature_b64 = headers.get("signature", "")

    if not cert_url:
        raise ValueError("Missing SignatureCertChainUrl header")
    if not signature_b64:
        raise ValueError("Missing Signature header")
    if not _AMAZON_CERT_URL_RE.match(cert_url):
        raise ValueError(f"SignatureCertChainUrl not from Amazon S3: {cert_url!r}")

    raw_body_str = event.get("body") or ""
    raw_body = raw_body_str.encode("utf-8") if isinstance(raw_body_str, str) else raw_body_str

    # 3. Timestamp freshness — fail fast before cert download
    try:
        body_json = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_json = {}
    ts_str   = (body_json.get("request") or {}).get("timestamp", "")
    event_ts = _parse_iso_timestamp(ts_str)
    if event_ts:
        age = abs(time.time() - event_ts)
        if age > 150:
            raise ValueError(f"Request timestamp stale or future-dated: age={age:.0f}s")

    # 4. Download cert chain and validate
    certs = _get_amazon_cert_chain(cert_url)
    _verify_cert_chain(certs)

    # 5. RSA-SHA1 body signature using the leaf certificate
    try:
        sig_bytes = base64.b64decode(signature_b64)
    except Exception as exc:
        raise ValueError(f"Signature header is not valid base64: {exc}") from exc

    try:
        # SHA1 is required here — it is Amazon's mandated body-signing algorithm.
        certs[0].public_key().verify(sig_bytes, raw_body, padding.PKCS1v15(), hashes.SHA1())
    except InvalidSignature as exc:
        raise ValueError("RSA body signature does not match request body") from exc
    except Exception as exc:
        raise ValueError(f"Signature verification error: {exc}") from exc


# ── Cognito JWT validation ─────────────────────────────────────────────────────

def _get_jwks() -> dict:
    """Fetch and cache the Cognito JWKS (1-hour module-level cache)."""
    global _jwks_cache, _jwks_cache_expires
    if _jwks_cache and time.time() < _jwks_cache_expires:
        return _jwks_cache

    if not _COGNITO_USER_POOL_ID or not _COGNITO_REGION:
        raise RuntimeError("COGNITO_USER_POOL_ID or COGNITO_REGION not configured")

    jwks_url = (
        f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com"
        f"/{_COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    )
    try:
        with urllib.request.urlopen(jwks_url, timeout=5) as r:
            _jwks_cache = json.loads(r.read())
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Cognito JWKS: {exc}") from exc

    _jwks_cache_expires = time.time() + 3600
    return _jwks_cache


def _validate_cognito_token(token: str) -> str:
    """
    Validate a Cognito access JWT using JWKS. Returns the Cognito userId (sub).

    Checks: RS256 signature via JWKS, token expiry, issuer matches pool, token_use=access.
    Raises ValueError on any validation failure. Callers MUST NOT update state on failure.
    """
    jwks = _get_jwks()
    expected_issuer = (
        f"https://cognito-idp.{_COGNITO_REGION}.amazonaws.com/{_COGNITO_USER_POOL_ID}"
    )

    try:
        claims = _jose_jwt.decode(token, jwks, algorithms=["RS256"])
    except JWTError as exc:
        raise ValueError(f"Cognito JWT validation failed: {exc}") from exc

    if claims.get("iss") != expected_issuer:
        raise ValueError(
            f"Cognito JWT issuer mismatch: "
            f"expected {expected_issuer!r}, got {claims.get('iss')!r}"
        )
    if claims.get("token_use") != "access":
        raise ValueError(
            f"Cognito JWT token_use must be 'access', got {claims.get('token_use')!r}"
        )

    sub = claims.get("sub") or claims.get("username")
    if not sub:
        raise ValueError("Cognito JWT missing sub claim")

    return sub


# ── Event handlers ─────────────────────────────────────────────────────────────

def _handle_skill_account_linked(
    alexa_user_id: str,
    skill_id: str,
    request_id: str,
    linked_account_token: str = "",
) -> None:
    """
    Handle AlexaSkillEvent.SkillAccountLinked.

    Identity is established exclusively by validating the linked-account Cognito JWT
    (context.System.user.accessToken) via JWKS. Only on successful validation is
    alexaCustomerId stored and alexaLinked set to true.

    No state changes are made if the token is absent or fails any validation check.
    The fallback GSI lookup on unverified data is intentionally absent.
    """
    if not linked_account_token:
        logger.warning(
            "COGNITO_TOKEN_MISSING alexa_user_id=%s request_id=%s — "
            "no linked-account token in SkillAccountLinked event; no state updated",
            alexa_user_id, request_id,
        )
        return

    try:
        user_id = _validate_cognito_token(linked_account_token)
    except (ValueError, RuntimeError) as exc:
        logger.warning(
            "COGNITO_TOKEN_INVALID alexa_user_id=%s error=%s request_id=%s — "
            "token validation failed; no state updated",
            alexa_user_id, exc, request_id,
        )
        return

    logger.info("COGNITO_TOKEN_VALID userId=%s alexa_user_id=%s request_id=%s",
                user_id, alexa_user_id, request_id)

    tokens_table = _ddb().Table(_TOKENS_TABLE)
    tokens_table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET alexaCustomerId = :c",
        ExpressionAttributeValues={":c": alexa_user_id},
    )
    logger.info("ALEXA_CUSTOMER_ID_STORED userId=%s alexaCustomerId=%s request_id=%s",
                user_id, alexa_user_id, request_id)

    # Write a stable reverse-lookup item keyed by alexa_user_id.
    # Amazon user IDs ("amzn1.ask.account.*") never collide with Cognito UUIDs, so
    # they can safely share the same table. This item is NOT part of the token
    # lifecycle — nothing deletes it except SkillDisabled — which fixes the race
    # where the main token item is recreated without alexaCustomerId before SkillDisabled
    # queries the GSI.
    tokens_table.put_item(Item={
        "userId":        alexa_user_id,       # e.g. "amzn1.ask.account.AMA5WS2G..."
        "cognitoUserId": user_id,             # e.g. "11539dda-7091-70ef-830c-..."
        "linkedAt":      int(time.time()),
    })
    logger.info("ALEXA_REVERSE_MAP_STORED alexa_user_id=%s cognitoUserId=%s request_id=%s",
                alexa_user_id, user_id, request_id)

    if _USER_DEVICE_MAPPING_TABLE:
        mapping_table = _ddb().Table(_USER_DEVICE_MAPPING_TABLE)
        linked_at     = int(time.time())

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

    logger.info(
        "[AUDIT] ALEXA_APP_LINKED userId=%s alexaUserId=%s skill_id=%s request_id=%s",
        user_id, alexa_user_id, skill_id, request_id,
    )


def _handle_skill_disabled(
    alexa_user_id: str,
    skill_id: str,
    request_id: str,
    event_epoch: int = 0,
) -> None:
    """
    Clean up after a user disables the Digilux Alexa skill.

    Steps:
      1. Look up Cognito userId:
         a. Direct get_item by alexa_user_id (stable reverse-map item written by
            SkillAccountLinked — not part of token lifecycle, most reliable).
         b. GSI query on alexaCustomerId-index (fallback for older links).
         c. GSI query on amazonUserId-index (fallback for Alexa-app-initiated links).
      2. Stale-event guard: if event predates the current link's linkedAt, drop it.
         This prevents a delayed SkillDisabled from destroying a newer re-link.
      3. Delete the LWA token record and reverse-map item.
      4. Set alexaLinked=false on all user sites.
    """
    tokens_table = _ddb().Table(_TOKENS_TABLE)

    # ── 1. Find Cognito userId ────────────────────────────────────────────────
    # Step A: direct O(1) lookup via the stable reverse-map item written by
    # SkillAccountLinked. Amazon user IDs never collide with Cognito UUIDs, so
    # the item at Key={"userId": alexa_user_id} is exclusively our mapping record.
    # This is reliable even when the main token item was deleted/recreated without
    # alexaCustomerId (the race condition that defeats the GSI approach).
    reverse_map_item = tokens_table.get_item(Key={"userId": alexa_user_id}).get("Item")
    if reverse_map_item and reverse_map_item.get("cognitoUserId"):
        user_id = reverse_map_item["cognitoUserId"]
        linked_at_from_map = reverse_map_item.get("linkedAt", 0)
        logger.info("SKILL_DISABLED_USER_FOUND_VIA_REVERSE_MAP userId=%s "
                    "alexa_user_id=%s request_id=%s",
                    user_id, alexa_user_id, request_id)
    else:
        # Step B: fall back to GSI on the main token item (older links without map item,
        # or Alexa-app-initiated links that use amazonUserId).
        items = []

        if alexa_user_id.startswith("amzn1.ask.account."):
            result = tokens_table.query(
                IndexName="alexaCustomerId-index",
                KeyConditionExpression=Key("alexaCustomerId").eq(alexa_user_id),
                Limit=1,
            )
            items = result.get("Items", [])
            if items:
                logger.debug("GSI_HIT index=alexaCustomerId-index alexa_user_id=%s "
                             "request_id=%s", alexa_user_id, request_id)

        if not items:
            result = tokens_table.query(
                IndexName="amazonUserId-index",
                KeyConditionExpression=Key("amazonUserId").eq(alexa_user_id),
                Limit=1,
            )
            items = result.get("Items", [])
            if items:
                logger.debug("GSI_HIT index=amazonUserId-index alexa_user_id=%s "
                             "request_id=%s", alexa_user_id, request_id)

        if not items:
            logger.warning(
                "SKILL_DISABLED_USER_NOT_FOUND alexaUserId=%s request_id=%s — "
                "user may not have linked via this system or alexaCustomerId not yet stored",
                alexa_user_id, request_id,
            )
            return

        item              = items[0]
        user_id           = item["userId"]
        linked_at_from_map = item.get("linkedAt", 0)
        logger.info("SKILL_DISABLED_USER_FOUND userId=%s alexa_user_id=%s request_id=%s",
                    user_id, alexa_user_id, request_id)

    # ── 2. Stale-event guard ──────────────────────────────────────────────────
    # Drop the event if it predates the current link's linkedAt timestamp.
    # Both values must be non-zero; older records without linkedAt are processed normally.
    if linked_at_from_map and event_epoch and event_epoch < linked_at_from_map:
        logger.warning(
            "STALE_SKILL_DISABLED_IGNORED userId=%s event_ts=%d linked_at=%d "
            "delta_s=%d — event predates current link; state not cleared request_id=%s",
            user_id, event_epoch, linked_at_from_map, linked_at_from_map - event_epoch,
            request_id,
        )
        return

    # ── 3. Delete LWA token record and reverse-map item ───────────────────────
    tokens_table.delete_item(Key={"userId": user_id})
    logger.info("TOKEN_DELETED userId=%s request_id=%s", user_id, request_id)
    tokens_table.delete_item(Key={"userId": alexa_user_id})
    logger.debug("REVERSE_MAP_DELETED alexa_user_id=%s request_id=%s",
                 alexa_user_id, request_id)

    # ── 4. Set alexaLinked=false on all user sites ────────────────────────────
    if _USER_DEVICE_MAPPING_TABLE:
        mapping_table = _ddb().Table(_USER_DEVICE_MAPPING_TABLE)
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

    logger.info(
        "[AUDIT] ALEXA_INITIATED_UNLINK userId=%s alexaUserId=%s skill_id=%s request_id=%s",
        user_id, alexa_user_id, skill_id, request_id,
    )


def lambda_handler(event, context):  # noqa: ANN001
    request_id = getattr(context, "aws_request_id", "local")
    logger.info("REQUEST_START function=alexaSkillEvents request_id=%s", request_id)

    # ── 0. Guard: fail fast if required env vars are missing ──────────────────
    if _missing_vars:
        logger.error("CONFIG_ERROR missing=%s request_id=%s", _missing_vars, request_id)
        return _resp(500, {"error": "Server configuration error"})

    # ── 1. Full Amazon request verification (fail closed) ─────────────────────
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

    # Event timestamp for the stale-event guard in _handle_skill_disabled
    event_epoch = _parse_iso_timestamp(
        (body.get("request") or {}).get("timestamp", "")
    )

    # ── 3a. Alexa.Authorization.Error / SKILL_DISABLED (SMAPI format) ─────────
    # Amazon delivers this structure when a user disables the skill from Alexa app:
    # { "header": { "namespace": "Alexa.Authorization", "name": "Error", ... },
    #   "event":  { "type": "SKILL_DISABLED", "userId": "amzn1.ask.account.XXX" } }
    # This format has no context.System; handled before the skill-ID check.
    top_header = body.get("header") or {}
    if (top_header.get("namespace") == "Alexa.Authorization"
            and top_header.get("name") == "Error"):
        ev            = body.get("event") or {}
        event_type    = ev.get("type", "")
        alexa_user_id = ev.get("userId", "")
        logger.info("ALEXA_AUTHORIZATION_ERROR type=%s alexaUserId=%s request_id=%s",
                    event_type, alexa_user_id, request_id)
        if event_type == "SKILL_DISABLED":
            if alexa_user_id:
                try:
                    _handle_skill_disabled(alexa_user_id, "", request_id, event_epoch)
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

    # ── 4. Extract event fields ───────────────────────────────────────────────
    system        = (body.get("context") or {}).get("System", {})
    user_block    = system.get("user", {})
    alexa_user_id = user_block.get("userId", "")
    request_type  = (body.get("request") or {}).get("type", "")

    # Cognito access token from the linked account.
    # Amazon documents this in context.System.user.accessToken for SkillAccountLinked;
    # also check request.body.accessToken as a secondary location.
    linked_account_token = user_block.get("accessToken", "") or (
        (body.get("request") or {}).get("body", {}).get("accessToken", "")
    )

    logger.info("SKILL_EVENT type=%s alexaUserId=%s skill_id=%s request_id=%s",
                request_type, alexa_user_id, skill_id, request_id)

    # ── 5. Route by event type ────────────────────────────────────────────────
    if request_type == "AlexaSkillEvent.SkillDisabled":
        if not alexa_user_id:
            logger.warning("SKILL_DISABLED_NO_USER_ID request_id=%s", request_id)
        else:
            try:
                _handle_skill_disabled(alexa_user_id, skill_id, request_id, event_epoch)
            except Exception as exc:
                logger.error("SKILL_DISABLED_HANDLER_ERROR error=%s request_id=%s",
                             exc, request_id)
                # Return 200 — Amazon does not retry on 5xx for skill events;
                # investigate via CloudWatch rather than relying on retry.

    elif request_type == "AlexaSkillEvent.SkillEnabled":
        logger.info("SKILL_ENABLED alexaUserId=%s — linking handled by completeAppToApp "
                    "request_id=%s", alexa_user_id, request_id)

    elif request_type == "AlexaSkillEvent.SkillAccountLinked":
        if not alexa_user_id:
            logger.warning("SKILL_ACCOUNT_LINKED_NO_USER_ID request_id=%s", request_id)
        else:
            try:
                _handle_skill_account_linked(
                    alexa_user_id, skill_id, request_id, linked_account_token,
                )
            except Exception as exc:
                logger.error("SKILL_ACCOUNT_LINKED_HANDLER_ERROR error=%s request_id=%s",
                             exc, request_id)

    else:
        logger.info("UNHANDLED_EVENT_TYPE type=%s request_id=%s", request_type, request_id)

    # Amazon expects HTTP 200 — always return 200 to prevent Amazon's retry mechanism
    logger.info("REQUEST_OK function=alexaSkillEvents request_id=%s", request_id)
    return _resp(200, {"received": True})
