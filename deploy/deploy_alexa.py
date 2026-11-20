#!/usr/bin/env python3
"""
deploy_alexa.py — Deploy all Alexa resources for one environment,
including alexa_fulfillment to ALEXA_FULFILLMENT_REGION (eu-west-1).

Does NOT touch Google Home. Fully idempotent — safe to re-run.

Usage:
    python3 deploy/deploy_alexa.py --config deploy/env.digilux.qa.json
    python3 deploy/deploy_alexa.py --config deploy/env.honeywell.prod.json --dry-run
"""

import argparse
import os
import shutil
import sys
import time
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from deploy_common import (
    cfg, check_no_fill_in, check_referenced_resources, check_required_keys,
    check_service_access, confirm_deploy, deploy_lambda, DeployReport,
    ensure_cognito_authorizer, ensure_gsi, ensure_role, ensure_table,
    get_or_create_resource, get_root_resource_id, load_config, log_create,
    log_error, log_info, log_ok, log_skip, log_warn, make_client, make_policy,
    package_lambda, preflight_aws, set_log_retention, set_step, smoke_test,
    st_cw, st_ddb, st_iot, st_kms, st_s3, st_sm, st_stream, st_xray,
    step_header, verify_secret_accessible, wire_cors, wire_method,
)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 not installed.")
    sys.exit(1)

# ─── Required config keys ─────────────────────────────────────────────────────

REQUIRED_KEYS = [
    "AWS_REGION", "ALEXA_FULFILLMENT_REGION", "ROLE_PREFIX", "ENV_LABEL",
    # API_GATEWAY_ID intentionally omitted — empty string = auto-create a new API
    "API_STAGE", "API_BASE_URL",
    "COGNITO_USER_POOL_ID", "COGNITO_REGION", "COGNITO_CLIENT_ID",
    "COGNITO_HOSTED_UI_DOMAIN",
    "ALEXA_SESSIONS_TABLE", "ALEXA_TOKENS_TABLE", "ALEXA_PKCE_TABLE",
    "USER_DEVICE_MAPPING_TABLE", "USER_DEVICE_DETAILS_TABLE",
    "DEVICE_STATE_TABLE", "SCENE_TABLE", "SCENE_GSI_NAME",
    "METADATA_BUCKET", "ENDPOINT_KEY_SECRET_ARN", "ENDPOINT_KEY_REGION",
    "LWA_SECRET_ARN", "LWA_SECRET_REGION", "KMS_KEY_ALIAS",
    "ALEXA_SKILL_ID", "ALEXA_SKILL_STAGE",
    "ALEXA_REDIRECT_URI", "ALEXA_REDIRECT_HOSTS",
    "APP_DEEP_LINK_SCHEME", "APP_NAME", "DEVICE_MANUFACTURER",
    "LOG_LEVEL", "ALEXA_SESSION_TTL_SECONDS", "CREATE_UPDATE_FN",
    "LWA_TOKEN_URL", "LWA_REVOKE_URL", "SKILL_ENABLEMENT_URL",
    "ALEXA_GATEWAY_URL", "IOT_DATA_ENDPOINT",
    "DEVICE_TABLE_SECRET_ARN", "DEVICE_TABLE_SECRET_REGION",
    # Stage 1 SMAPI PKCE flow
    # PKCE_STATE_SECRET_ARN intentionally omitted — auto-created by _ensure_pkce_secret()
    # if empty; existing ARN is verified accessible if already set.
    "SMAPI_LINKING_ENABLED", "COGNITO_ALEXA_CLIENT_ID",
    "ALEXA_COGNITO_CALLBACK_URI", "PKCE_STATE_SECRET_REGION",
]

# ─── Lambda definitions ───────────────────────────────────────────────────────
#  (fn_name, source_dir, timeout_s, memory_mb, region_key)
#  region_key: "main" = AWS_REGION, "ireland" = ALEXA_FULFILLMENT_REGION

ALEXA_LAMBDAS = [
    ("alexa_app_to_app_link",      "lambdas/alexa_app_to_app_link",      15, 256,  "main"),
    ("alexa_complete_app_to_app",  "lambdas/alexa_complete_app_to_app",  30, 256,  "main"),
    ("alexa_server_callback",      "lambdas/alexa_server_callback",      10, 256,  "main"),
    ("alexa_app_to_app_unlink",    "lambdas/alexa_app_to_app_unlink",    15, 256,  "main"),
    ("alexa_link_status",          "lambdas/alexa_link_status",          15, 256,  "main"),
    ("alexa_skill_server_events",  "lambdas/alexa_skill_server_events",  15, 128,  "main"),
    ("alexa_state_reporter",       "lambdas/alexa_state_reporter",       30, 256,  "main"),
    ("alexa_proactive_discovery",  "lambdas/alexa_proactive_discovery",  30, 256,  "main"),
    ("alexa_fulfillment",          "lambdas/alexa_fulfillment",          30, 256,  "ireland"),
    ("alexa_cognito_callback",     "lambdas/alexa_cognito_callback",     15, 256,  "main"),
]


def _lambda_env(fn_name: str, c: dict, kms_key_arn: str) -> dict:
    region = cfg(c, "AWS_REGION")
    base = {"DATA_REGION": region, "LOG_LEVEL": cfg(c, "LOG_LEVEL")}
    if fn_name == "alexa_app_to_app_link":
        return {**base,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "SESSION_TABLE": cfg(c, "ALEXA_SESSIONS_TABLE"),
                "REDIRECT_URI": cfg(c, "ALEXA_REDIRECT_URI"),
                "ALLOWED_REDIRECT_HOSTS": cfg(c, "ALEXA_REDIRECT_HOSTS"),
                "KMS_KEY_ARN": kms_key_arn,
                "SESSION_TTL_SECONDS": cfg(c, "ALEXA_SESSION_TTL_SECONDS")}
    if fn_name == "alexa_complete_app_to_app":
        return {**base,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "SESSION_TABLE": cfg(c, "ALEXA_SESSIONS_TABLE"),
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "KMS_KEY_ARN": kms_key_arn,
                "ALEXA_SKILL_ID": cfg(c, "ALEXA_SKILL_ID"),
                "ALEXA_SKILL_STAGE": cfg(c, "ALEXA_SKILL_STAGE"),
                "REDIRECT_URI": cfg(c, "ALEXA_REDIRECT_URI"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "CREATE_UPDATE_FN": cfg(c, "CREATE_UPDATE_FN"),
                "LWA_TOKEN_URL": cfg(c, "LWA_TOKEN_URL"),
                "LWA_REVOKE_URL": cfg(c, "LWA_REVOKE_URL"),
                # Stage 1 SMAPI PKCE flow
                "SMAPI_LINKING_ENABLED":    cfg(c, "SMAPI_LINKING_ENABLED"),
                "PKCE_TABLE":               cfg(c, "ALEXA_PKCE_TABLE"),
                "COGNITO_ALEXA_CLIENT_ID":  cfg(c, "COGNITO_ALEXA_CLIENT_ID"),
                "COGNITO_HOSTED_UI_DOMAIN": cfg(c, "COGNITO_HOSTED_UI_DOMAIN"),
                "COGNITO_CALLBACK_URI":     cfg(c, "ALEXA_COGNITO_CALLBACK_URI"),
                "PKCE_STATE_SECRET_ARN":    cfg(c, "PKCE_STATE_SECRET_ARN"),
                "PKCE_STATE_SECRET_REGION": cfg(c, "PKCE_STATE_SECRET_REGION")}
    if fn_name == "alexa_server_callback":
        return {**base,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "APP_SCHEME": cfg(c, "APP_DEEP_LINK_SCHEME")}
    if fn_name == "alexa_app_to_app_unlink":
        return {**base,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "KMS_KEY_ARN": kms_key_arn,
                "ALEXA_SKILL_ID": cfg(c, "ALEXA_SKILL_ID"),
                "CREATE_UPDATE_FN": cfg(c, "CREATE_UPDATE_FN"),
                "LWA_REVOKE_URL": cfg(c, "LWA_REVOKE_URL"),
                "SKILL_ENABLEMENT_URL": cfg(c, "SKILL_ENABLEMENT_URL")}
    if fn_name == "alexa_link_status":
        return {**base,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "KMS_KEY_ARN": kms_key_arn,
                "ALEXA_SKILL_ID": cfg(c, "ALEXA_SKILL_ID"),
                "SKILL_ENABLEMENT_URL": cfg(c, "SKILL_ENABLEMENT_URL")}
    if fn_name == "alexa_skill_server_events":
        return {**base,
                "LWA_TOKENS_TABLE":          cfg(c, "ALEXA_TOKENS_TABLE"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "ALEXA_SKILL_ID":            cfg(c, "ALEXA_SKILL_ID"),
                "COGNITO_USER_POOL_ID":      cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION":            cfg(c, "COGNITO_REGION")}
    if fn_name == "alexa_state_reporter":
        return {**base,
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "METADATA_BUCKET": cfg(c, "METADATA_BUCKET"),
                "ENDPOINT_KEY_SECRET_ARN": cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
                "ENDPOINT_KEY_REGION": cfg(c, "ENDPOINT_KEY_REGION"),
                "ALEXA_GATEWAY_URL": cfg(c, "ALEXA_GATEWAY_URL")}
    if fn_name == "alexa_proactive_discovery":
        return {**base,
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "SCENE_TABLE": cfg(c, "SCENE_TABLE"),
                "METADATA_BUCKET": cfg(c, "METADATA_BUCKET"),
                "ENDPOINT_KEY_SECRET_ARN": cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
                "ENDPOINT_KEY_REGION": cfg(c, "ENDPOINT_KEY_REGION"),
                "ALEXA_GATEWAY_URL": cfg(c, "ALEXA_GATEWAY_URL")}
    if fn_name == "alexa_fulfillment":
        return {"DATA_REGION": cfg(c, "AWS_REGION"),    # NOTE: main region, NOT eu-west-1
                "LOG_LEVEL": cfg(c, "LOG_LEVEL"),
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "LWA_SECRET_ARN": cfg(c, "LWA_SECRET_ARN"),
                "LWA_SECRET_REGION": cfg(c, "LWA_SECRET_REGION"),
                "KMS_KEY_ARN": kms_key_arn,
                "COGNITO_USER_POOL_ID": cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION": cfg(c, "COGNITO_REGION"),
                "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "DEVICE_STATE_TABLE": cfg(c, "DEVICE_STATE_TABLE"),
                "SCENE_TABLE": cfg(c, "SCENE_TABLE"),
                "METADATA_BUCKET": cfg(c, "METADATA_BUCKET"),
                "ENDPOINT_KEY_SECRET_ARN": cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
                "ENDPOINT_KEY_REGION": cfg(c, "ENDPOINT_KEY_REGION"),
                "DEVICE_TABLE_SECRET_ARN": cfg(c, "DEVICE_TABLE_SECRET_ARN"),
                "DEVICE_TABLE_SECRET_REGION": cfg(c, "DEVICE_TABLE_SECRET_REGION"),
                "ALEXA_GATEWAY_URL": cfg(c, "ALEXA_GATEWAY_URL"),
                "IOT_DATA_ENDPOINT": cfg(c, "IOT_DATA_ENDPOINT"),
                "CREATE_UPDATE_FN":  cfg(c, "CREATE_UPDATE_FN")}
    if fn_name == "alexa_cognito_callback":
        return {**base,
                "COGNITO_USER_POOL_ID":    cfg(c, "COGNITO_USER_POOL_ID"),
                "COGNITO_REGION":          cfg(c, "COGNITO_REGION"),
                "PKCE_TABLE":              cfg(c, "ALEXA_PKCE_TABLE"),
                "LWA_TOKENS_TABLE":        cfg(c, "ALEXA_TOKENS_TABLE"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "KMS_KEY_ARN":             kms_key_arn,
                "COGNITO_HOSTED_UI_DOMAIN": cfg(c, "COGNITO_HOSTED_UI_DOMAIN"),
                "COGNITO_ALEXA_CLIENT_ID": cfg(c, "COGNITO_ALEXA_CLIENT_ID"),
                "COGNITO_CLIENT_SECRET":   cfg(c, "COGNITO_CLIENT_SECRET"),
                "COGNITO_CALLBACK_URI":    cfg(c, "ALEXA_COGNITO_CALLBACK_URI"),
                "PKCE_STATE_SECRET_ARN":   cfg(c, "PKCE_STATE_SECRET_ARN"),
                "PKCE_STATE_SECRET_REGION": cfg(c, "PKCE_STATE_SECRET_REGION"),
                "ALEXA_SKILL_ID":          cfg(c, "ALEXA_SKILL_ID"),
                "ALEXA_SKILL_STAGE":       cfg(c, "ALEXA_SKILL_STAGE"),
                "SKILL_ENABLEMENT_URL":    cfg(c, "SKILL_ENABLEMENT_URL"),
                "CREATE_UPDATE_FN":        cfg(c, "CREATE_UPDATE_FN"),
                "APP_SCHEME":              cfg(c, "APP_DEEP_LINK_SCHEME")}
    raise ValueError(f"Unknown Alexa Lambda: {fn_name}")


# ─── Step 1 — Load + Validate Config ─────────────────────────────────────────

def step1_config(config_path: str) -> dict:
    step_header(1, "CONFIG")
    log_info(f"Loaded: {config_path}")
    c = load_config(config_path)
    log_info(f"ENV_LABEL={cfg(c,'ENV_LABEL')}  AWS_REGION={cfg(c,'AWS_REGION')}  "
             f"ALEXA_FULFILLMENT_REGION={cfg(c,'ALEXA_FULFILLMENT_REGION')}")
    log_info("Checking required keys...")
    check_no_fill_in(c, "deploy_alexa.py")
    check_required_keys(c, REQUIRED_KEYS)
    log_ok("Config validated")
    return c


# ─── Step 2 — Preflight Checks ───────────────────────────────────────────────

def step2_preflight(session, c: dict) -> str:
    step_header(2, "PREFLIGHT")
    account_id = preflight_aws(session)
    verify_secret_accessible(session, cfg(c, "LWA_SECRET_ARN"),
                             cfg(c, "LWA_SECRET_REGION"), "LWA_SECRET_ARN")
    # Confirm Ireland region reachable
    fulfillment_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    log_info(f"Checking ALEXA_FULFILLMENT_REGION ({fulfillment_region}) connectivity...")
    try:
        lam = make_client(session, "lambda", fulfillment_region)
        lam.list_functions(MaxItems=1)
        log_ok(f"{fulfillment_region} reachable")
    except Exception as e:
        log_error(f"Cannot reach {fulfillment_region}: {e}")
        sys.exit(1)
    return account_id


# ─── Step 2b — PKCE State Secret (auto-create if absent) ─────────────────────

def _ensure_pkce_secret(session, c: dict) -> None:
    """Create the PKCE state signing secret if it doesn't exist, then patch c in-memory.

    Idempotent: describe_secret is called first; the secret is only created when
    ResourceNotFoundException is raised.  The secret value is JSON so the Lambda
    can read it with json.loads(raw)["signing_secret"].
    """
    import secrets as _secrets
    import json as _json

    region      = cfg(c, "PKCE_STATE_SECRET_REGION") or cfg(c, "AWS_REGION")
    existing    = cfg(c, "PKCE_STATE_SECRET_ARN")
    secret_name = f"{cfg(c, 'ENV_LABEL')}/alexa/pkce-state"
    sm          = make_client(session, "secretsmanager", region)

    if existing:
        # ARN already provided — just verify it's accessible
        try:
            resp = sm.describe_secret(SecretId=existing)
            log_ok(f"PKCE_STATE_SECRET accessible  name={resp.get('Name', existing)}")
        except Exception as e:
            log_error(f"PKCE_STATE_SECRET_ARN set but not accessible: {e}")
            sys.exit(1)
        return

    # ARN not set — create or retrieve by name
    try:
        resp = sm.describe_secret(SecretId=secret_name)
        arn  = resp["ARN"]
        log_ok(f"PKCE_STATE_SECRET already exists  name={secret_name}  arn={arn}")
    except sm.exceptions.ResourceNotFoundException:
        signing_secret = _secrets.token_urlsafe(32)
        secret_value   = _json.dumps({"signing_secret": signing_secret})
        resp = sm.create_secret(
            Name=secret_name,
            Description=f"PKCE state signing secret for Alexa app-to-app linking ({cfg(c, 'ENV_LABEL')})",
            SecretString=secret_value,
        )
        arn = resp["ARN"]
        log_create(f"PKCE_STATE_SECRET created  name={secret_name}  arn={arn}")

    c["PKCE_STATE_SECRET_ARN"] = arn
    log_info(f"PKCE_STATE_SECRET_ARN set to {arn}")


# ─── Step 2b — Pre-Deploy Permission Checks (A + B) ──────────────────────────

def step2b_permission_checks(session, c: dict, report: DeployReport,
                              profile: str = "") -> bool:
    """
    A — Probe deployer IAM access for every required AWS service.
        On any failure: write consolidated IAM policy JSON + print one CLI command.
    B — Verify referenced resources (secrets, CREATE_UPDATE_FN, S3 bucket) exist.
    Returns True only if both A and B pass (no FAILs).
    """
    step_header(2, "PREFLIGHT — PERMISSION CHECKS")
    main_region = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")

    a_ok = check_service_access(session, main_region, irl_region, report, profile)
    b_ok = check_referenced_resources(session, c, report)

    if not a_ok:
        log_error("Deployer lacks required AWS service access (see section A above).")
        log_error("Grant the printed permissions, then re-run deploy_alexa.py.")
        return False
    if not b_ok:
        log_warn("One or more referenced resources do not exist (see section B above).")
        log_warn("Lambdas will deploy but may fail at runtime until those resources are created.")
    return True


# ─── Step 3 — DynamoDB Tables ────────────────────────────────────────────────

def step3_ddb(session, c: dict):
    step_header(3, "DYNAMODB TABLES")
    region = cfg(c, "AWS_REGION")
    ddb = make_client(session, "dynamodb", region)
    tables = [
        (cfg(c, "ALEXA_SESSIONS_TABLE"), "state",              "ttl"),
        (cfg(c, "ALEXA_TOKENS_TABLE"),   "userId",             None),
        (cfg(c, "ALEXA_PKCE_TABLE"),     "pkceTransactionId",  "expiresAt"),
    ]
    for name, pk, ttl in tables:
        ensure_table(ddb, name, pk, ttl_attr=ttl)
    log_ok(f"DDB complete  tables={len(tables)}")

    # GSIs on the LWA tokens table for SkillDisabled/SkillAccountLinked user lookup
    tokens_table = cfg(c, "ALEXA_TOKENS_TABLE")
    ensure_gsi(ddb, tokens_table, "amazonUserId-index",    "amazonUserId")
    ensure_gsi(ddb, tokens_table, "alexaCustomerId-index", "alexaCustomerId")
    log_ok(f"DDB GSIs complete  table={tokens_table}")


# ─── Step 4 — KMS Key ────────────────────────────────────────────────────────

def step4_kms(session, c: dict, account_id: str) -> str:
    step_header(4, "KMS KEY")
    region = cfg(c, "AWS_REGION")
    kms = make_client(session, "kms", region)
    alias = cfg(c, "KMS_KEY_ALIAS")
    log_info(f"Looking up alias {alias} in {region}...")

    aliases = []
    resp = kms.list_aliases()
    aliases.extend(resp.get("Aliases", []))
    while resp.get("NextMarker"):
        resp = kms.list_aliases(Marker=resp["NextMarker"])
        aliases.extend(resp.get("Aliases", []))
    match = next((a for a in aliases if a.get("AliasName") == alias), None)
    if match:
        key_id  = match["TargetKeyId"]
        key_arn = kms.describe_key(KeyId=alias)["KeyMetadata"]["Arn"]
        log_skip(f"Alias found  KeyId={key_id}  KeyArn={key_arn}")
        return key_arn

    log_create(f"Alias not found — creating new KMS key...")
    key_resp = kms.create_key(
        Description=f"Alexa LWA tokens encryption key ({cfg(c,'ENV_LABEL')})",
        KeyUsage="ENCRYPT_DECRYPT",
    )
    key_id  = key_resp["KeyMetadata"]["KeyId"]
    key_arn = key_resp["KeyMetadata"]["Arn"]
    log_ok(f"Key created  KeyId={key_id}")
    kms.create_alias(AliasName=alias, TargetKeyId=key_id)
    log_ok(f"Alias created  {alias}  KeyArn={key_arn}")
    return key_arn


# ─── Step 5 — API Gateway ────────────────────────────────────────────────────

def step5_apigw(session, c: dict, account_id: str) -> dict:
    step_header(5, "API GATEWAY")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)

    api_gw_id = cfg(c, "API_GATEWAY_ID")
    if api_gw_id:
        log_info(f"Verifying API {api_gw_id} in {region}...")
        try:
            apigw.get_rest_api(restApiId=api_gw_id)
            log_ok(f"API exists  id={api_gw_id}")
        except ClientError as e:
            log_error(f"API {api_gw_id} not found: {e}")
            sys.exit(1)
    else:
        log_create("API_GATEWAY_ID empty — creating new REST API (REGIONAL)...")
        resp = apigw.create_rest_api(
            name=f"{cfg(c,'ROLE_PREFIX')}-smarthome-api",
            endpointConfiguration={"types": ["REGIONAL"]},
        )
        api_gw_id = resp["id"]
        log_warn(f"NEW API created  id={api_gw_id}  — save this in your config file!")

    pool_arn = (f"arn:aws:cognito-idp:{cfg(c,'COGNITO_REGION')}:{account_id}"
                f":userpool/{cfg(c,'COGNITO_USER_POOL_ID')}")
    authorizer_id = ensure_cognito_authorizer(apigw, api_gw_id, pool_arn)
    root_id = get_root_resource_id(apigw, api_gw_id)
    log_ok(f"API GW ready  id={api_gw_id}  authorizer={authorizer_id}  root={root_id}")
    return {"api_gw_id": api_gw_id, "authorizer_id": authorizer_id, "root_id": root_id}


# ─── Step 6 — IAM Roles ──────────────────────────────────────────────────────

def step6_iam(session, c: dict, account_id: str, kms_key_arn: str) -> dict:
    step_header(6, "IAM ROLES")
    iam = make_client(session, "iam")
    region      = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")
    role_pre    = cfg(c, "ROLE_PREFIX")
    tokens      = cfg(c, "ALEXA_TOKENS_TABLE")
    sessions    = cfg(c, "ALEXA_SESSIONS_TABLE")
    udm         = cfg(c, "USER_DEVICE_MAPPING_TABLE")
    udd         = cfg(c, "USER_DEVICE_DETAILS_TABLE")
    ds          = cfg(c, "DEVICE_STATE_TABLE")
    scene       = cfg(c, "SCENE_TABLE")
    bucket      = cfg(c, "METADATA_BUCKET")
    lwa_arn     = cfg(c, "LWA_SECRET_ARN")
    ep_arn      = cfg(c, "ENDPOINT_KEY_SECRET_ARN")
    dt_arn      = cfg(c, "DEVICE_TABLE_SECRET_ARN")
    cw          = st_cw(account_id, region)
    cw_irl      = st_cw(account_id, irl_region)
    xr          = st_xray()

    # Some environments encrypt the LWA tokens table with a separate KMS key
    # (e.g. alias/digilux/honeywell/app) rather than the main Alexa token key.
    # When ALEXA_TOKENS_KMS_KEY_ARN is set and differs from kms_key_arn, every
    # role that reads from ALEXA_TOKENS_TABLE also needs kms:Decrypt on that key.
    _toks_kms_arn = cfg(c, "ALEXA_TOKENS_KMS_KEY_ARN")
    tokens_kms = (st_kms(_toks_kms_arn, ["kms:Decrypt"]),) \
        if _toks_kms_arn and _toks_kms_arn != kms_key_arn else ()

    roles = [
        (f"{role_pre}-alexa-start-role", make_policy(
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem"], account_id, region, sessions),
            st_kms(kms_key_arn), cw, xr)),

        (f"{role_pre}-alexa-complete-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem"], account_id, region, sessions),
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                   account_id, region, tokens),
            st_ddb(["dynamodb:GetItem"], account_id, region, udm),
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteItem",
                    "dynamodb:TransactWriteItems"],
                   account_id, region, cfg(c, "ALEXA_PKCE_TABLE")),
            st_sm(lwa_arn, cfg(c, "PKCE_STATE_SECRET_ARN")),
            st_kms(kms_key_arn), *tokens_kms, cw, xr)),

        (f"{role_pre}-alexa-callback-role", make_policy(cw, xr)),

        (f"{role_pre}-alexa-unlink-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"],
                   account_id, region, tokens),
            st_sm(lwa_arn), st_kms(kms_key_arn, ["kms:Decrypt"]), *tokens_kms, cw, xr)),

        (f"{role_pre}-alexa-status-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_sm(lwa_arn), st_kms(kms_key_arn, ["kms:Decrypt"]), *tokens_kms, cw, xr)),

        (f"{role_pre}-alexa-skill-evt-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_ddb(["dynamodb:Query"],
                   account_id, region, tokens, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                   account_id, region, udm, idx=True),
            *tokens_kms, cw, xr)),

        (f"{role_pre}-alexa-state-reporter-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_s3(bucket), st_sm(ep_arn, lwa_arn),
            *tokens_kms, st_stream(account_id, region, ds), cw, xr)),

        (f"{role_pre}-alexa-proactive-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
            st_s3(bucket), st_sm(ep_arn, lwa_arn),
            *tokens_kms, st_stream(account_id, region, udd), cw, xr)),

        (f"{role_pre}-alexa-fulfillment-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, ds,  idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
            st_s3(bucket), st_sm(lwa_arn, ep_arn, dt_arn),
            st_kms(kms_key_arn, ["kms:Decrypt", "kms:Encrypt"]), *tokens_kms,
            st_iot(account_id, region),
            cw_irl, xr)),   # CW Logs in Ireland — Lambda writes logs where it runs

        (f"{role_pre}-alexa-cognito-cb-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, cfg(c, "ALEXA_PKCE_TABLE")),
            st_ddb(["dynamodb:GetItem"], account_id, region, tokens),
            st_ddb(["dynamodb:UpdateItem"], account_id, region, udm),
            st_sm(cfg(c, "PKCE_STATE_SECRET_ARN")),
            st_kms(kms_key_arn, ["kms:Decrypt"]), *tokens_kms,
            {"Effect": "Allow", "Action": "lambda:InvokeFunction",
             "Resource": f"arn:aws:lambda:{region}:{account_id}:function:{cfg(c,'CREATE_UPDATE_FN')}"},
            cw, xr)),
    ]

    log_info("NOTE: alexa-fulfillment-role CW Logs permission scoped to ALEXA_FULFILLMENT_REGION"
             f" ({irl_region})")
    role_arns = {}
    created_count = 0
    for role_name, policy in roles:
        was_created, arn = ensure_role(iam, role_name, policy)
        role_arns[role_name] = arn
        if was_created:
            created_count += 1

    log_ok(f"IAM roles complete  total={len(roles)}  created={created_count}  updated={len(roles)-created_count}")
    return role_arns


# ─── Step 7 — Package Lambdas ────────────────────────────────────────────────

def step7_package(c: dict, deploy_only: set | None = None) -> dict:
    step_header(7, "PACKAGE LAMBDAS")
    env_label  = cfg(c, "ENV_LABEL")
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    build_root = f"/tmp/deploy_build_{env_label}"
    if os.path.exists(build_root):
        shutil.rmtree(build_root)
    os.makedirs(build_root)
    log_info(f"Build dir: {build_root}")
    if deploy_only:
        log_info(f"Lambda filter active — packaging only: {sorted(deploy_only)}")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    zips = {}
    for fn_name, src_rel, _, _, region_key in ALEXA_LAMBDAS:
        if deploy_only and fn_name not in deploy_only:
            log_skip(f"  [SKIP] {fn_name} (not in --lambdas filter)")
            continue
        src = os.path.join(repo_root, src_rel)
        if not os.path.exists(src):
            log_error(f"Source directory not found: {src}")
            sys.exit(1)
        if region_key == "ireland":
            log_info(f"  [IRELAND LAMBDA] {fn_name} — will deploy to {irl_region}")
        zips[fn_name] = package_lambda(src, fn_name, build_root)

    log_ok(f"Packaging complete  {len(zips)} zips built"
           + (f"  (filtered from {len(ALEXA_LAMBDAS)})" if deploy_only else ""))
    return zips


# ─── Step 8 — Deploy Lambdas ─────────────────────────────────────────────────

def step8_lambdas(session, c: dict, role_arns: dict, zips: dict,
                  kms_key_arn: str, account_id: str,
                  deploy_only: set | None = None) -> dict:
    step_header(8, "LAMBDA DEPLOY")
    main_region = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")
    fn_arns     = {}

    role_map = {
        "alexa_app_to_app_link":     f"{cfg(c,'ROLE_PREFIX')}-alexa-start-role",
        "alexa_complete_app_to_app": f"{cfg(c,'ROLE_PREFIX')}-alexa-complete-role",
        "alexa_server_callback":     f"{cfg(c,'ROLE_PREFIX')}-alexa-callback-role",
        "alexa_app_to_app_unlink":   f"{cfg(c,'ROLE_PREFIX')}-alexa-unlink-role",
        "alexa_link_status":         f"{cfg(c,'ROLE_PREFIX')}-alexa-status-role",
        "alexa_skill_server_events": f"{cfg(c,'ROLE_PREFIX')}-alexa-skill-evt-role",
        "alexa_state_reporter":      f"{cfg(c,'ROLE_PREFIX')}-alexa-state-reporter-role",
        "alexa_proactive_discovery": f"{cfg(c,'ROLE_PREFIX')}-alexa-proactive-role",
        "alexa_fulfillment":         f"{cfg(c,'ROLE_PREFIX')}-alexa-fulfillment-role",
        "alexa_cognito_callback":    f"{cfg(c,'ROLE_PREFIX')}-alexa-cognito-cb-role",
    }

    main_count = 0
    for fn_name, _, timeout, memory, region_key in ALEXA_LAMBDAS:
        if region_key == "ireland":
            continue
        if deploy_only and fn_name not in deploy_only:
            log_skip(f"  [SKIP] {fn_name} (not in --lambdas filter)")
            continue
        env_vars = _lambda_env(fn_name, c, kms_key_arn)
        arn = deploy_lambda(session, fn_name, zips[fn_name],
                            role_arns[role_map[fn_name]], timeout, memory, env_vars, main_region)
        fn_arns[fn_name] = arn
        set_log_retention(session, fn_name, main_region)
        main_count += 1

    log_info(f"\n── Ireland Lambda ──────────────────────────────────────────────────────")
    if deploy_only and "alexa_fulfillment" not in deploy_only:
        log_skip(f"  [SKIP] alexa_fulfillment (not in --lambdas filter)")
    else:
        log_info(f"Deploying alexa_fulfillment → {irl_region}  timeout=30s  memory=256MB")
        log_info(f"NOTE: DATA_REGION={main_region} — DDB tables live in main region, not Ireland")
        env_vars = _lambda_env("alexa_fulfillment", c, kms_key_arn)
        arn = deploy_lambda(session, "alexa_fulfillment", zips["alexa_fulfillment"],
                            role_arns[role_map["alexa_fulfillment"]], 30, 256, env_vars, irl_region)
        fn_arns["alexa_fulfillment"] = arn
        set_log_retention(session, "alexa_fulfillment", irl_region)

    total_all = len(ALEXA_LAMBDAS)
    skipped   = total_all - len(fn_arns)
    log_ok(f"Lambdas deployed: {len(fn_arns)}/{total_all}  "
           f"({main_count} × {main_region}"
           + (f", 1 × {irl_region}" if "alexa_fulfillment" in fn_arns else "")
           + (f", {skipped} skipped" if skipped else "") + ")")
    log_warn("alexa_fulfillment is NOT wired to API GW — called directly by Alexa Smart Home backend")
    return fn_arns


# ─── Step 9 — API Gateway Resource Tree + Method Wiring ──────────────────────

def step9_routes(session, c: dict, apigw_info: dict, fn_arns: dict, account_id: str):
    step_header(9, "API GW ROUTES")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)
    api_id  = apigw_info["api_gw_id"]
    auth_id = apigw_info["authorizer_id"]
    root_id = apigw_info["root_id"]

    _lam_client = make_client(session, "lambda", region)

    def _resolve_arn(fn_name: str) -> str | None:
        if fn_name in fn_arns:
            return fn_arns[fn_name]
        # Lambda was not deployed in this run (filtered via --lambdas); look up live ARN.
        try:
            return _lam_client.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
        except _lam_client.exceptions.ResourceNotFoundException:
            log_warn(f"Lambda '{fn_name}' not found in AWS — skipping route wiring")
            return None
        except Exception as exc:
            raise RuntimeError(
                f"Cannot resolve ARN for '{fn_name}': not in this deploy and AWS lookup failed ({exc})"
            ) from exc

    def wm(resource_id, method, fn_name, auth="COGNITO_USER_POOLS"):
        arn = _resolve_arn(fn_name)
        if arn is None:
            return
        wire_method(session, apigw, api_id, resource_id, method,
                    arn, region, account_id, fn_name,
                    auth_type=auth,
                    authorizer_id=auth_id if auth == "COGNITO_USER_POOLS" else None)

    log_info("Building /api/v1/alexa/... tree")
    api_res      = get_or_create_resource(apigw, api_id, root_id,    "api")
    v1_res       = get_or_create_resource(apigw, api_id, api_res,    "v1")
    alexa_res    = get_or_create_resource(apigw, api_id, v1_res,     "alexa")
    status_res   = get_or_create_resource(apigw, api_id, alexa_res,  "status")
    start_res    = get_or_create_resource(apigw, api_id, alexa_res,  "startAppToApp")
    complete_res = get_or_create_resource(apigw, api_id, alexa_res,  "completeAppToApp")
    unlink_res   = get_or_create_resource(apigw, api_id, alexa_res,  "unlink")

    log_info("Building /alexa/callback + /alexa/skill-event tree")
    alexa_top_res  = get_or_create_resource(apigw, api_id, root_id,        "alexa")
    callback_res   = get_or_create_resource(apigw, api_id, alexa_top_res,  "callback")
    skill_evt_res  = get_or_create_resource(apigw, api_id, alexa_top_res,  "skill-event")

    log_info("Wiring methods...")

    wm(status_res,   "GET",    "alexa_link_status")
    wire_cors(apigw, api_id, status_res)

    wm(start_res,    "POST",   "alexa_app_to_app_link")
    wire_cors(apigw, api_id, start_res)

    wm(complete_res, "POST",   "alexa_complete_app_to_app")
    wire_cors(apigw, api_id, complete_res)

    wm(unlink_res,   "DELETE", "alexa_app_to_app_unlink")
    wire_cors(apigw, api_id, unlink_res)

    wm(callback_res, "GET",    "alexa_server_callback", auth="NONE")
    wire_cors(apigw, api_id, callback_res)

    wm(skill_evt_res, "POST",  "alexa_skill_server_events", auth="NONE")
    wire_cors(apigw, api_id, skill_evt_res)

    log_info("Building /api/v1/alexa/cognito/callback tree")
    cognito_res   = get_or_create_resource(apigw, api_id, alexa_res,   "cognito")
    cog_cb_res    = get_or_create_resource(apigw, api_id, cognito_res, "callback")
    wm(cog_cb_res, "GET", "alexa_cognito_callback", auth="NONE")
    wire_cors(apigw, api_id, cog_cb_res)

    log_info("NOTE: alexa_fulfillment NOT wired to API GW — triggered directly by Alexa backend")
    log_ok("Routes wired: 7 methods on 7 resources")


# ─── Step 10 — Deploy Stage ──────────────────────────────────────────────────

def step10_stage(session, c: dict, apigw_info: dict):
    step_header(10, "API GW STAGE DEPLOY")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)
    api_id = apigw_info["api_gw_id"]
    stage  = cfg(c, "API_STAGE")
    log_info(f"Deploying stage '{stage}' on API {api_id}...")
    resp = apigw.create_deployment(restApiId=api_id, stageName=stage)
    log_ok(f"Stage deployed  deployment_id={resp['id']}")
    log_info("Sleeping 5s for cold-start readiness...")
    time.sleep(5)


# ─── Step 11 — Smoke Tests ───────────────────────────────────────────────────

def step11_smoke(c: dict):
    step_header(11, "SMOKE TESTS")
    base = cfg(c, "API_BASE_URL").rstrip("/")
    tests = [
        ("POST",   f"{base}/api/v1/alexa/startAppToApp",    401),
        ("POST",   f"{base}/api/v1/alexa/completeAppToApp", 401),
        ("GET",    f"{base}/api/v1/alexa/status",           401),
        ("DELETE", f"{base}/api/v1/alexa/unlink",           401),
        ("GET",    f"{base}/alexa/callback",                200),
    ]
    passed = sum(smoke_test(m, u, e) for m, u, e in tests)
    log_ok(f"Smoke tests: {passed}/{len(tests)} passed")
    if passed < len(tests):
        log_warn(f"{len(tests) - passed} smoke test(s) failed — check API GW stage deployment")


# ─── Step 12 — Post-Deploy Manual Steps ──────────────────────────────────────

def step12_manual(c: dict, account_id: str):
    step_header(12, "MANUAL POST-DEPLOY STEPS")
    region          = cfg(c, "AWS_REGION")
    irl_region      = cfg(c, "ALEXA_FULFILLMENT_REGION")
    skill_id        = cfg(c, "ALEXA_SKILL_ID")
    ds              = cfg(c, "DEVICE_STATE_TABLE")
    udd             = cfg(c, "USER_DEVICE_DETAILS_TABLE")
    api_base        = cfg(c, "API_BASE_URL").rstrip("/")
    skill_event_url = f"{api_base}/alexa/skill-event"
    fulfillment_arn = f"arn:aws:lambda:{irl_region}:{account_id}:function:alexa_fulfillment"

    log_warn("The following steps CANNOT be automated — complete them manually:\n")
    print(f"  1. Enable DynamoDB stream on {ds} for alexa_state_reporter:")
    print(f"     aws dynamodb update-table \\")
    print(f"       --table-name {ds} \\")
    print(f"       --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \\")
    print(f"       --region {region}")
    print()
    print(f"  2. Enable DynamoDB stream on {udd} for alexa_proactive_discovery:")
    print(f"     aws dynamodb update-table \\")
    print(f"       --table-name {udd} \\")
    print(f"       --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \\")
    print(f"       --region {region}")
    print()
    print(f"  3. Add Alexa Smart Home trigger to alexa_fulfillment in {irl_region}:")
    print(f"     aws lambda add-permission \\")
    print(f"       --function-name alexa_fulfillment \\")
    print(f"       --statement-id alexa-smart-home \\")
    print(f"       --action lambda:InvokeFunction \\")
    print(f"       --principal alexa-connectedhome.amazon.com \\")
    print(f"       --event-source-token {skill_id} \\")
    print(f"       --region {irl_region}")
    print()
    print(f"  4. Register Lambda ARN in Alexa Developer Console:")
    print(f"     Developer Console → Your Skill → Build → Endpoint")
    print(f"     Default endpoint: {fulfillment_arn}")
    print()
    print(f"  5. Register skill events endpoint in Alexa Developer Console:")
    print(f"     Developer Console → Your Skill → Build → Permissions")
    print(f"     Enable: 'Alexa Skills Kit' → 'Events'")
    print(f"     Set HTTPS endpoint to: {skill_event_url}")
    print(f"     Subscribe to events:")
    print(f"       - AlexaSkillEvent.SkillAccountLinked")
    print(f"       - AlexaSkillEvent.SkillDisabled")
    print(f"     (Without this, unlink-from-Alexa-app will NOT clear alexaLinked in Digilux)")
    print()


# ─── Step 13 — Post-Deploy Verification (C–G) ────────────────────────────────

def step13_post_deploy_verify(session, c: dict, account_id: str,
                               kms_key_arn: str, fn_arns: dict,
                               report: DeployReport, profile: str = ""):
    """
    C — Verify every deployed Lambda is Active with the expected execution role.
    D — Check alexa_fulfillment has an Alexa Smart Home resource policy.
    E — Check DDB Streams are enabled on device_state + user_device_details.
    F — Compare actual Lambda env vars against expected (_lambda_env) — flag gaps.
    G — Add manual checklist items (Cognito callback URL, Alexa Dev Console).
    """
    step_header(13, "POST-DEPLOY VERIFICATION")
    main_region = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")
    profile_flag = f" --profile {profile}" if profile else " --profile <HONEYWELL_PROFILE>"

    lam_main = make_client(session, "lambda", main_region)
    lam_irl  = make_client(session, "lambda", irl_region)
    ddb      = make_client(session, "dynamodb", main_region)
    role_pre = cfg(c, "ROLE_PREFIX")

    _ROLE_MAP = {
        "alexa_app_to_app_link":      f"{role_pre}-alexa-start-role",
        "alexa_complete_app_to_app":  f"{role_pre}-alexa-complete-role",
        "alexa_server_callback":      f"{role_pre}-alexa-callback-role",
        "alexa_app_to_app_unlink":    f"{role_pre}-alexa-unlink-role",
        "alexa_link_status":          f"{role_pre}-alexa-status-role",
        "alexa_skill_server_events":  f"{role_pre}-alexa-skill-evt-role",
        "alexa_state_reporter":       f"{role_pre}-alexa-state-reporter-role",
        "alexa_proactive_discovery":  f"{role_pre}-alexa-proactive-role",
        "alexa_fulfillment":          f"{role_pre}-alexa-fulfillment-role",
        "alexa_cognito_callback":     f"{role_pre}-alexa-cognito-cb-role",
    }

    # ── C — Lambda state ─────────────────────────────────────────────────────
    report.begin_section("C", "C — Lambda State Verification")
    log_info("Checking Lambda active state and execution roles...")
    for fn_name, _, _, _, region_key in ALEXA_LAMBDAS:
        lam_client = lam_irl if region_key == "ireland" else lam_main
        deploy_region = irl_region if region_key == "ireland" else main_region
        expected_role = _ROLE_MAP.get(fn_name, "")
        try:
            conf = lam_client.get_function_configuration(FunctionName=fn_name)
            state     = conf.get("State", "Unknown")
            role_arn  = conf.get("Role", "")
            role_name = role_arn.split("/")[-1] if role_arn else ""
            if state != "Active":
                log_warn(f"  {fn_name}: state={state} (not Active)")
                report.add_check(fn_name, "WARN",
                                 f"State={state} in {deploy_region} — expected Active")
            elif expected_role and role_name != expected_role:
                log_warn(f"  {fn_name}: unexpected role {role_name} (expected {expected_role})")
                report.add_check(fn_name, "WARN",
                                 f"Active but role mismatch: got {role_name}, expected {expected_role}")
            else:
                log_ok(f"  {fn_name}: Active  role={role_name}")
                report.add_check(fn_name, "PASS",
                                 f"Active in {deploy_region}  role={role_name}")
        except Exception as exc:
            log_error(f"  {fn_name}: NOT found — {exc}")
            report.add_check(fn_name, "FAIL",
                             f"Lambda not found in {deploy_region}: {exc}")

    # ── D — Alexa fulfillment resource policy ────────────────────────────────
    report.begin_section("D", "D — alexa_fulfillment Alexa Resource Policy (eu-west-1)")
    log_info("Checking Alexa Smart Home resource policy on alexa_fulfillment...")
    skill_id = cfg(c, "ALEXA_SKILL_ID")
    try:
        policy_resp = lam_irl.get_policy(FunctionName="alexa_fulfillment")
        policy_doc  = json.loads(policy_resp["Policy"])
        stmts       = policy_doc.get("Statement", [])
        alexa_stmt  = next(
            (s for s in stmts
             if "alexa-connectedhome.amazon.com" in str(s.get("Principal", ""))),
            None,
        )
        if alexa_stmt:
            log_ok("  alexa_fulfillment: Alexa resource policy present")
            report.add_check("Alexa resource policy", "PASS",
                             "alexa-connectedhome.amazon.com principal found")
        else:
            log_warn("  alexa_fulfillment: no Alexa principal in resource policy")
            add_perm_cmd = (
                f"aws lambda add-permission \\\n"
                f"  --function-name alexa_fulfillment \\\n"
                f"  --statement-id alexa-smart-home \\\n"
                f"  --action lambda:InvokeFunction \\\n"
                f"  --principal alexa-connectedhome.amazon.com \\\n"
                f"  --event-source-token {skill_id} \\\n"
                f"  --region {irl_region}{profile_flag}"
            )
            report.add_check("Alexa resource policy", "FAIL",
                             "alexa-connectedhome.amazon.com principal missing — Alexa cannot invoke fulfillment Lambda")
            report.add_fix_command(
                "Add Alexa Smart Home trigger to alexa_fulfillment", add_perm_cmd)
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            log_warn("  alexa_fulfillment: no resource policy attached yet")
            add_perm_cmd = (
                f"aws lambda add-permission \\\n"
                f"  --function-name alexa_fulfillment \\\n"
                f"  --statement-id alexa-smart-home \\\n"
                f"  --action lambda:InvokeFunction \\\n"
                f"  --principal alexa-connectedhome.amazon.com \\\n"
                f"  --event-source-token {skill_id} \\\n"
                f"  --region {irl_region}{profile_flag}"
            )
            report.add_check("Alexa resource policy", "FAIL",
                             "No resource policy at all — Alexa cannot invoke the fulfillment Lambda")
            report.add_fix_command(
                "Add Alexa Smart Home trigger to alexa_fulfillment", add_perm_cmd)
        else:
            log_warn(f"  alexa_fulfillment policy check error: {exc}")
            report.add_check("Alexa resource policy", "WARN", f"Could not check policy: {exc}")

    # ── E — DDB Streams ──────────────────────────────────────────────────────
    report.begin_section("E", "E — DynamoDB Stream Triggers")
    log_info("Checking DDB Streams on device_state and user_device_details...")
    stream_tables = [
        (cfg(c, "DEVICE_STATE_TABLE"),       "alexa_state_reporter"),
        (cfg(c, "USER_DEVICE_DETAILS_TABLE"), "alexa_proactive_discovery"),
    ]
    for table_name, consumer_fn in stream_tables:
        try:
            desc   = ddb.describe_table(TableName=table_name)
            spec   = desc["Table"].get("StreamSpecification", {})
            stream_arn = desc["Table"].get("LatestStreamArn", "")
            enabled = spec.get("StreamEnabled", False)
            stream_view = spec.get("StreamViewType", "")
            if enabled and stream_view == "NEW_AND_OLD_IMAGES":
                # Check event source mapping exists
                lam_check = lam_main
                try:
                    esms = lam_check.list_event_source_mappings(
                        FunctionName=consumer_fn,
                        EventSourceArn=stream_arn,
                    ).get("EventSourceMappings", [])
                    if esms:
                        esm_state = esms[0].get("State", "Unknown")
                        log_ok(f"  {table_name} → {consumer_fn}: stream enabled, ESM state={esm_state}")
                        report.add_check(f"{table_name} stream", "PASS",
                                         f"Stream enabled, ESM {esm_state} → {consumer_fn}")
                    else:
                        log_warn(f"  {table_name}: stream enabled but no ESM to {consumer_fn}")
                        esm_cmd = (
                            f"aws lambda create-event-source-mapping \\\n"
                            f"  --function-name {consumer_fn} \\\n"
                            f"  --event-source-arn {stream_arn} \\\n"
                            f"  --starting-position TRIM_HORIZON \\\n"
                            f"  --batch-size 100 \\\n"
                            f"  --region {main_region}{profile_flag}"
                        )
                        report.add_check(f"{table_name} stream", "FAIL",
                                         f"Stream enabled but no event source mapping to {consumer_fn}")
                        report.add_fix_command(
                            f"Create event source mapping: {table_name} → {consumer_fn}",
                            esm_cmd)
                except Exception as esm_exc:
                    log_warn(f"  {table_name}: ESM check failed — {esm_exc}")
                    report.add_check(f"{table_name} stream", "WARN",
                                     f"Stream enabled but ESM check failed: {esm_exc}")
            else:
                log_warn(f"  {table_name}: stream not enabled (StreamEnabled={enabled}, view={stream_view})")
                enable_cmd = (
                    f"# Step 1 — enable stream on {table_name}\n"
                    f"aws dynamodb update-table \\\n"
                    f"  --table-name {table_name} \\\n"
                    f"  --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \\\n"
                    f"  --region {main_region}{profile_flag}\n\n"
                    f"# Step 2 — after stream is ENABLED, create event source mapping\n"
                    f"# (re-run step13 or deploy to get the stream ARN, then run:)\n"
                    f"aws lambda create-event-source-mapping \\\n"
                    f"  --function-name {consumer_fn} \\\n"
                    f"  --event-source-arn <STREAM_ARN_FROM_ABOVE> \\\n"
                    f"  --starting-position TRIM_HORIZON \\\n"
                    f"  --batch-size 100 \\\n"
                    f"  --region {main_region}{profile_flag}"
                )
                report.add_check(f"{table_name} stream", "FAIL",
                                 f"DDB stream not enabled — {consumer_fn} will NOT receive device state changes")
                report.add_fix_command(
                    f"Enable DDB stream + map to {consumer_fn}", enable_cmd)
        except Exception as exc:
            log_error(f"  {table_name}: describe_table failed — {exc}")
            report.add_check(f"{table_name} stream", "WARN",
                             f"Could not describe table: {exc}")

    # ── F — Env var verification ─────────────────────────────────────────────
    report.begin_section("F", "F — Lambda Environment Variable Verification")
    log_info("Comparing deployed env vars against expected config for each Lambda...")
    for fn_name, _, _, _, region_key in ALEXA_LAMBDAS:
        lam_client    = lam_irl if region_key == "ireland" else lam_main
        deploy_region = irl_region if region_key == "ireland" else main_region
        try:
            expected = _lambda_env(fn_name, c, kms_key_arn)
        except ValueError:
            continue  # unknown Lambda — skip

        try:
            conf       = lam_client.get_function_configuration(FunctionName=fn_name)
            actual_env = conf.get("Environment", {}).get("Variables", {})
        except Exception as exc:
            log_warn(f"  {fn_name}: could not fetch config — {exc}")
            report.add_check(f"{fn_name} env vars", "WARN",
                             f"get_function_configuration failed: {exc}")
            continue

        missing  = {k: v for k, v in expected.items() if k not in actual_env}
        empty    = {k for k, v in actual_env.items() if k in expected and not v}
        combined = {**missing, **{k: expected[k] for k in empty if k in expected}}

        if not combined:
            log_ok(f"  {fn_name}: all {len(expected)} env vars present")
            report.add_check(f"{fn_name}", "PASS",
                             f"All {len(expected)} expected env vars present and non-empty")
        else:
            keys_summary = ", ".join(list(combined.keys())[:5])
            if len(combined) > 5:
                keys_summary += f" … (+{len(combined)-5} more)"
            log_error(f"  {fn_name}: {len(combined)} env var(s) missing/empty — {keys_summary}")

            # Build update-function-configuration command
            vars_json = json.dumps({"Variables": combined})
            update_cmd = (
                f"aws lambda update-function-configuration \\\n"
                f"  --function-name {fn_name} \\\n"
                f"  --environment '{vars_json}' \\\n"
                f"  --region {deploy_region}{profile_flag}"
            )
            report.add_check(f"{fn_name}", "FAIL",
                             f"{len(combined)} env var(s) missing/empty: {keys_summary}",
                             fix_command=f"See fix command in Fix Commands section")
            report.add_fix_command(
                f"Set missing env vars on {fn_name} ({len(combined)} var(s))",
                update_cmd)

    # ── G — Manual checklist ─────────────────────────────────────────────────
    api_base       = cfg(c, "API_BASE_URL").rstrip("/")
    cog_client_id  = cfg(c, "COGNITO_ALEXA_CLIENT_ID")
    cog_callback   = cfg(c, "ALEXA_COGNITO_CALLBACK_URI")
    alexa_redirect = cfg(c, "ALEXA_REDIRECT_URI")
    skill_event_url = f"{api_base}/alexa/skill-event"
    fulfillment_arn = (
        f"arn:aws:lambda:{irl_region}:{account_id}:function:alexa_fulfillment"
    )

    report.add_manual_item(
        f"[Mrinal — Cognito Console] Add '{cog_callback}' to allowed callback URLs "
        f"on Cognito app client '{cog_client_id}' in {main_region}"
    )
    report.add_manual_item(
        f"[Mrinal — Alexa Developer Console] Add '{alexa_redirect}' as a redirect URI "
        f"in the skill Account Linking settings for skill {cfg(c, 'ALEXA_SKILL_ID')}"
    )
    report.add_manual_item(
        f"[Mrinal — Alexa Developer Console] Register fulfillment Lambda ARN as skill endpoint: "
        f"{fulfillment_arn}"
    )
    report.add_manual_item(
        f"[Mrinal — Alexa Developer Console] Enable skill events (SkillAccountLinked, SkillDisabled) "
        f"with HTTPS endpoint: {skill_event_url}"
    )

    log_ok("Post-deploy verification complete")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deploy all Alexa resources")
    parser.add_argument("--config",  required=True, help="Path to env JSON config file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate config + run permission checks (A+B) only. No deploy. Generates HTML report.")
    parser.add_argument("--verify-only", action="store_true",
                        help="Skip deploy steps entirely. Read-only: look up existing KMS key + Lambda ARNs, "
                             "then run post-deploy verification (C-G) on whatever is already deployed. "
                             "Safe to run on live environments — makes zero changes.")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    parser.add_argument(
        "--lambdas", default=None,
        help="Comma-separated Lambda names to deploy (default: all). "
             "DDB tables, IAM roles, API routes and stage deploy always run. "
             "Example: --lambdas alexa_complete_app_to_app",
    )
    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "━" * 68)
    print("  deploy_alexa.py — Alexa deployment (including Ireland alexa_fulfillment)")
    print("━" * 68 + "\n")

    c = step1_config(args.config)

    deploy_only = set(x.strip() for x in args.lambdas.split(",")) if args.lambdas else None
    if deploy_only:
        valid_names = {fn for fn, *_ in ALEXA_LAMBDAS}
        unknown = deploy_only - valid_names
        if unknown:
            print(f"ERROR: Unknown Lambda name(s) in --lambdas: {unknown}")
            print(f"Valid names: {sorted(valid_names)}")
            sys.exit(1)

    session_kwargs = {"region_name": cfg(c, "AWS_REGION")}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    account_id = step2_preflight(session, c)
    _ensure_pkce_secret(session, c)

    # ── Create report (collects results from all steps A–G) ──────────────────
    report = DeployReport(
        env_label          = cfg(c, "ENV_LABEL"),
        account_id         = account_id,
        region             = cfg(c, "AWS_REGION"),
        fulfillment_region = cfg(c, "ALEXA_FULFILLMENT_REGION"),
    )
    ts           = datetime.now().strftime("%Y%m%dT%H%M%S")
    report_path  = os.path.join("output", f"deploy_alexa_{cfg(c,'ENV_LABEL')}_{ts}.html")

    # ── A + B: Pre-deploy permission checks ──────────────────────────────────
    checks_ok = step2b_permission_checks(session, c, report, args.profile or "")
    if not checks_ok:
        # A failed — cannot deploy without service access; generate report + exit
        report.begin_section("DEPLOY", "Deploy Steps")
        report.add_check("Deploy skipped", "SKIP",
                         "Skipped — deployer lacks required AWS service access (see section A)")
        report.render_html(report_path)
        report.open_in_browser(report_path)
        sys.exit(1)

    lambda_count_str = (
        f"{len(deploy_only)} (filtered: {', '.join(sorted(deploy_only))})"
        if deploy_only else
        f"10 (9 × {cfg(c,'AWS_REGION')}, 1 × {cfg(c,'ALEXA_FULFILLMENT_REGION')})"
    )
    print(f"\n  {'━' * 60}")
    print(f"  DEPLOYMENT PLAN — deploy_alexa.py")
    print(f"  ENV_LABEL              : {cfg(c,'ENV_LABEL')}")
    print(f"  AWS_REGION             : {cfg(c,'AWS_REGION')}  (9 Lambdas, DDB, API GW, KMS)")
    print(f"  ALEXA_FULFILLMENT_REGION: {cfg(c,'ALEXA_FULFILLMENT_REGION')}  (alexa_fulfillment)")
    print(f"  API                    : {cfg(c,'API_GATEWAY_ID')}  →  {cfg(c,'API_BASE_URL')}")
    print(f"  Will create/update:")
    print(f"    DDB tables    : 3 (alexa_sessions, alexa_tokens, alexa_pkce_sessions)")
    print(f"    KMS key       : 1 ({cfg(c,'KMS_KEY_ALIAS')})")
    print(f"    IAM roles     : 10")
    print(f"    Lambda funcs  : {lambda_count_str}")
    print(f"    API GW routes : 6 methods on 6 resources + CORS")
    print(f"    Stage         : {cfg(c,'API_STAGE')}")
    print(f"  {'━' * 60}\n")

    # ── --verify-only: skip all deploy steps, read-only post-deploy check ────
    if args.verify_only:
        step_header(13, "VERIFY-ONLY MODE — reading existing deployment state")
        log_info("No changes will be made. Looking up existing KMS key and Lambda ARNs...")

        # Look up KMS key ARN by alias (read-only describe_key)
        kms_key_arn = ""
        try:
            kms_client  = make_client(session, "kms", cfg(c, "AWS_REGION"))
            kms_key_arn = kms_client.describe_key(
                KeyId=cfg(c, "KMS_KEY_ALIAS")
            )["KeyMetadata"]["Arn"]
            log_ok(f"KMS key found  alias={cfg(c,'KMS_KEY_ALIAS')}  arn={kms_key_arn[:60]}…")
        except Exception as exc:
            log_warn(f"KMS key not found ({cfg(c,'KMS_KEY_ALIAS')}): {exc} — env var check will use placeholder")

        # Probe each Lambda to build fn_arns (read-only get_function_configuration)
        fn_arns: dict = {}
        lam_main_v = make_client(session, "lambda", cfg(c, "AWS_REGION"))
        lam_irl_v  = make_client(session, "lambda", cfg(c, "ALEXA_FULFILLMENT_REGION"))
        for fn_name, _, _, _, region_key in ALEXA_LAMBDAS:
            lam_v = lam_irl_v if region_key == "ireland" else lam_main_v
            try:
                conf = lam_v.get_function_configuration(FunctionName=fn_name)
                fn_arns[fn_name] = conf["FunctionArn"]
                log_ok(f"  {fn_name}: found")
            except Exception:
                log_warn(f"  {fn_name}: not found — will appear as FAIL in section C")

        log_info(f"Found {len(fn_arns)}/{len(ALEXA_LAMBDAS)} Lambdas. Running post-deploy verification...")
        report.begin_section("DEPLOY", "Deploy Steps")
        report.add_check("Deploy", "SKIP",
                         f"--verify-only mode: deploy skipped. "
                         f"{len(fn_arns)}/{len(ALEXA_LAMBDAS)} existing Lambdas found.")
        try:
            step13_post_deploy_verify(
                session, c, account_id, kms_key_arn, fn_arns, report, args.profile or "")
        except Exception as exc:
            log_warn(f"Post-deploy verification error: {exc}")
            report.add_check("Verification error", "WARN", str(exc))
        abs_report = report.render_html(report_path)
        report.open_in_browser(abs_report)
        log_ok(f"Verify-only complete. Report: file://{abs_report}")
        return

    if not confirm_deploy(cfg(c, "ENV_LABEL"), "deploy_alexa.py", args.dry_run):
        report.begin_section("DEPLOY", "Deploy Steps")
        report.add_check("Deploy", "SKIP", "Cancelled by user at confirmation prompt")
        report.render_html(report_path)
        report.open_in_browser(report_path)
        return

    # ── Deploy steps 3–12 ────────────────────────────────────────────────────
    report.begin_section("DEPLOY", "Deploy Steps (3–12)")
    kms_key_arn = None
    fn_arns     = {}
    deploy_ok   = True

    try:
        set_step("START")
        log_info(f"env={cfg(c,'ENV_LABEL')}  account={account_id}")

        step3_ddb(session, c)
        report.add_check("Step 3 — DynamoDB Tables", "PASS",
                         "3 tables created/verified with TTL and GSIs")

        kms_key_arn = step4_kms(session, c, account_id)
        report.add_check("Step 4 — KMS Key", "PASS",
                         f"Key alias {cfg(c,'KMS_KEY_ALIAS')} ready  arn={kms_key_arn[:60]}…")

        apigw_info = step5_apigw(session, c, account_id)
        report.add_check("Step 5 — API Gateway", "PASS",
                         f"API {apigw_info.get('id','?')} verified")

        role_arns = step6_iam(session, c, account_id, kms_key_arn)
        report.add_check("Step 6 — IAM Roles", "PASS",
                         f"{len(role_arns)} execution roles created/updated")

        zips = step7_package(c, deploy_only)
        report.add_check("Step 7 — Package", "PASS",
                         f"{len(zips)} Lambda zip(s) built")

        fn_arns = step8_lambdas(session, c, role_arns, zips, kms_key_arn, account_id, deploy_only)
        report.add_check("Step 8 — Lambda Deploy", "PASS",
                         f"{len(fn_arns)} Lambda(s) deployed")

        step9_routes(session, c, apigw_info, fn_arns, account_id)
        report.add_check("Step 9 — API GW Routes", "PASS",
                         "6 routes wired with Cognito authorizer + CORS")

        step10_stage(session, c, apigw_info)
        report.add_check("Step 10 — Stage Deploy", "PASS",
                         f"Stage '{cfg(c,'API_STAGE')}' deployed")

        step11_smoke(c)
        report.add_check("Step 11 — Smoke Tests", "PASS",
                         "All smoke test endpoints returned expected status codes")

        step12_manual(c, account_id)
        report.add_check("Step 12 — Manual Steps", "INFO",
                         "Manual checklist printed to console (also in report section G)")

        elapsed = (datetime.now() - start_time).seconds
        set_step("SUMMARY")
        print(f"\n{'━' * 68}")
        log_ok(f"deploy_alexa.py completed  env={cfg(c,'ENV_LABEL')}")
        log_info(f"Duration: {elapsed // 60}m {elapsed % 60}s")
        log_info(f"  DDB tables  : 3")
        log_info(f"  KMS key     : {cfg(c,'KMS_KEY_ALIAS')}")
        log_info(f"  IAM roles   : 10")
        log_info(f"  Lambdas     : {len(fn_arns)} deployed"
                 + (f" (filtered)" if deploy_only else
                    f" (9 × {cfg(c,'AWS_REGION')}, 1 × {cfg(c,'ALEXA_FULFILLMENT_REGION')})"))
        log_info(f"  API GW      : 6 routes wired, stage deployed")
        print(f"{'━' * 68}\n")

    except Exception as e:
        set_step("SUMMARY")
        log_error("Deployment FAILED")
        log_error(f"Exception: {e}")
        log_error(traceback.format_exc())
        log_error("Resources deployed before failure are left in place.")
        report.add_check("Deploy exception", "FAIL", str(e))
        deploy_ok = False

    # ── Post-deploy verification C–G (runs even if deploy failed partially) ──
    if fn_arns or not deploy_ok:
        try:
            step13_post_deploy_verify(
                session, c, account_id,
                kms_key_arn or "",
                fn_arns, report,
                args.profile or "",
            )
        except Exception as exc:
            log_warn(f"Post-deploy verification error (non-fatal): {exc}")
            report.begin_section("POST", "Post-Deploy Verification")
            report.add_check("Verification error", "WARN", str(exc))

    # ── Generate HTML report + open browser (always, success or failure) ─────
    set_step("REPORT")
    try:
        abs_report = report.render_html(report_path)
        report.open_in_browser(abs_report)
        log_ok(f"Report: file://{abs_report}")
    except Exception as exc:
        log_warn(f"Could not generate HTML report: {exc}")

    if not deploy_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
