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
    cfg, check_no_fill_in, check_required_keys, confirm_deploy, deploy_lambda,
    ensure_cognito_authorizer, ensure_role, ensure_table, get_or_create_resource,
    get_root_resource_id, load_config, log_create, log_error, log_info, log_ok,
    log_skip, log_warn, make_client, make_policy, package_lambda, preflight_aws,
    set_log_retention, set_step, smoke_test, st_cw, st_ddb, st_iot, st_kms,
    st_s3, st_sm, st_stream, st_xray, step_header, verify_secret_accessible,
    wire_cors, wire_method,
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
    "ALEXA_SESSIONS_TABLE", "ALEXA_TOKENS_TABLE",
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
                "LWA_REVOKE_URL": cfg(c, "LWA_REVOKE_URL")}
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
                "KMS_KEY_ARN": kms_key_arn}
    if fn_name == "alexa_skill_server_events":
        return {**base,
                "LWA_TOKENS_TABLE": cfg(c, "ALEXA_TOKENS_TABLE"),
                "USER_DEVICE_MAPPING_TABLE": cfg(c, "USER_DEVICE_MAPPING_TABLE"),
                "ALEXA_SKILL_ID": cfg(c, "ALEXA_SKILL_ID")}
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


# ─── Step 3 — DynamoDB Tables ────────────────────────────────────────────────

def step3_ddb(session, c: dict):
    step_header(3, "DYNAMODB TABLES")
    region = cfg(c, "AWS_REGION")
    ddb = make_client(session, "dynamodb", region)
    tables = [
        (cfg(c, "ALEXA_SESSIONS_TABLE"), "state",  "ttl"),
        (cfg(c, "ALEXA_TOKENS_TABLE"),   "userId", None),
    ]
    for name, pk, ttl in tables:
        ensure_table(ddb, name, pk, ttl_attr=ttl)
    log_ok(f"DDB complete  tables={len(tables)}")


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

    roles = [
        (f"{role_pre}-alexa-start-role", make_policy(
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem"], account_id, region, sessions),
            st_kms(kms_key_arn), cw, xr)),

        (f"{role_pre}-alexa-complete-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem"], account_id, region, sessions),
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                   account_id, region, tokens),
            st_sm(lwa_arn), st_kms(kms_key_arn), cw, xr)),

        (f"{role_pre}-alexa-callback-role", make_policy(cw, xr)),

        (f"{role_pre}-alexa-unlink-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem"],
                   account_id, region, tokens),
            st_sm(lwa_arn), st_kms(kms_key_arn, ["kms:Decrypt"]), cw, xr)),

        (f"{role_pre}-alexa-status-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_sm(lwa_arn), st_kms(kms_key_arn, ["kms:Decrypt"]), cw, xr)),

        (f"{role_pre}-alexa-skill-evt-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"],
                   account_id, region, udm, idx=True),
            cw, xr)),

        (f"{role_pre}-alexa-state-reporter-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_s3(bucket), st_sm(ep_arn, lwa_arn),
            st_stream(account_id, region, ds), cw, xr)),

        (f"{role_pre}-alexa-proactive-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
            st_s3(bucket), st_sm(ep_arn, lwa_arn),
            st_stream(account_id, region, udd), cw, xr)),

        (f"{role_pre}-alexa-fulfillment-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, tokens),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, ds,  idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
            st_s3(bucket), st_sm(lwa_arn, ep_arn, dt_arn),
            st_kms(kms_key_arn, ["kms:Decrypt"]),
            st_iot(account_id, region),
            cw_irl, xr)),   # CW Logs in Ireland — Lambda writes logs where it runs
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

def step7_package(c: dict) -> dict:
    step_header(7, "PACKAGE LAMBDAS")
    env_label  = cfg(c, "ENV_LABEL")
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    build_root = f"/tmp/deploy_build_{env_label}"
    if os.path.exists(build_root):
        shutil.rmtree(build_root)
    os.makedirs(build_root)
    log_info(f"Build dir: {build_root}")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    zips = {}
    for fn_name, src_rel, _, _, region_key in ALEXA_LAMBDAS:
        src = os.path.join(repo_root, src_rel)
        if not os.path.exists(src):
            log_error(f"Source directory not found: {src}")
            sys.exit(1)
        if region_key == "ireland":
            log_info(f"  [IRELAND LAMBDA] {fn_name} — will deploy to {irl_region}")
        zips[fn_name] = package_lambda(src, fn_name, build_root)

    log_ok(f"Packaging complete  {len(zips)} zips built")
    return zips


# ─── Step 8 — Deploy Lambdas ─────────────────────────────────────────────────

def step8_lambdas(session, c: dict, role_arns: dict, zips: dict,
                  kms_key_arn: str, account_id: str) -> dict:
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
    }

    main_count = 0
    for fn_name, _, timeout, memory, region_key in ALEXA_LAMBDAS:
        if region_key == "ireland":
            continue
        env_vars = _lambda_env(fn_name, c, kms_key_arn)
        arn = deploy_lambda(session, fn_name, zips[fn_name],
                            role_arns[role_map[fn_name]], timeout, memory, env_vars, main_region)
        fn_arns[fn_name] = arn
        set_log_retention(session, fn_name, main_region)
        main_count += 1

    log_info(f"\n── Ireland Lambda ──────────────────────────────────────────────────────")
    log_info(f"Deploying alexa_fulfillment → {irl_region}  timeout=30s  memory=256MB")
    log_info(f"NOTE: DATA_REGION={main_region} — DDB tables live in main region, not Ireland")
    env_vars = _lambda_env("alexa_fulfillment", c, kms_key_arn)
    arn = deploy_lambda(session, "alexa_fulfillment", zips["alexa_fulfillment"],
                        role_arns[role_map["alexa_fulfillment"]], 30, 256, env_vars, irl_region)
    fn_arns["alexa_fulfillment"] = arn
    set_log_retention(session, "alexa_fulfillment", irl_region)

    log_ok(f"Lambdas deployed: {len(fn_arns)}/{len(ALEXA_LAMBDAS)}  "
           f"({main_count} × {main_region}, 1 × {irl_region})")
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

    def wm(resource_id, method, fn_name, auth="COGNITO_USER_POOLS"):
        wire_method(session, apigw, api_id, resource_id, method,
                    fn_arns[fn_name], region, account_id, fn_name,
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

    log_info("NOTE: alexa_fulfillment NOT wired to API GW — triggered directly by Alexa backend")
    log_ok("Routes wired: 6 methods on 6 resources")


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
    region      = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")
    skill_id    = cfg(c, "ALEXA_SKILL_ID")
    ds          = cfg(c, "DEVICE_STATE_TABLE")
    udd         = cfg(c, "USER_DEVICE_DETAILS_TABLE")
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


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deploy all Alexa resources")
    parser.add_argument("--config",  required=True, help="Path to env JSON config file")
    parser.add_argument("--dry-run", action="store_true", help="Validate + preflight only, no deploy")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "━" * 68)
    print("  deploy_alexa.py — Alexa deployment (including Ireland alexa_fulfillment)")
    print("━" * 68 + "\n")

    c = step1_config(args.config)

    session_kwargs = {"region_name": cfg(c, "AWS_REGION")}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    account_id = step2_preflight(session, c)

    print(f"\n  {'━' * 60}")
    print(f"  DEPLOYMENT PLAN — deploy_alexa.py")
    print(f"  ENV_LABEL              : {cfg(c,'ENV_LABEL')}")
    print(f"  AWS_REGION             : {cfg(c,'AWS_REGION')}  (8 Lambdas, DDB, API GW, KMS)")
    print(f"  ALEXA_FULFILLMENT_REGION: {cfg(c,'ALEXA_FULFILLMENT_REGION')}  (alexa_fulfillment)")
    print(f"  API                    : {cfg(c,'API_GATEWAY_ID')}  →  {cfg(c,'API_BASE_URL')}")
    print(f"  Will create/update:")
    print(f"    DDB tables    : 2 (alexa_sessions, alexa_tokens)")
    print(f"    KMS key       : 1 ({cfg(c,'KMS_KEY_ALIAS')})")
    print(f"    IAM roles     : 9")
    print(f"    Lambda funcs  : 9 (8 × {cfg(c,'AWS_REGION')}, 1 × {cfg(c,'ALEXA_FULFILLMENT_REGION')})")
    print(f"    API GW routes : 6 methods on 6 resources + CORS")
    print(f"    Stage         : {cfg(c,'API_STAGE')}")
    print(f"  {'━' * 60}\n")

    if not confirm_deploy(cfg(c, "ENV_LABEL"), "deploy_alexa.py", args.dry_run):
        return

    try:
        set_step("START")
        log_info(f"env={cfg(c,'ENV_LABEL')}  account={account_id}")

        step3_ddb(session, c)
        kms_key_arn = step4_kms(session, c, account_id)
        apigw_info  = step5_apigw(session, c, account_id)
        role_arns   = step6_iam(session, c, account_id, kms_key_arn)
        zips        = step7_package(c)
        fn_arns     = step8_lambdas(session, c, role_arns, zips, kms_key_arn, account_id)
        step9_routes(session, c, apigw_info, fn_arns, account_id)
        step10_stage(session, c, apigw_info)
        step11_smoke(c)
        step12_manual(c, account_id)

        elapsed = (datetime.now() - start_time).seconds
        set_step("SUMMARY")
        print(f"\n{'━' * 68}")
        log_ok(f"deploy_alexa.py completed  env={cfg(c,'ENV_LABEL')}")
        log_info(f"Duration: {elapsed // 60}m {elapsed % 60}s")
        log_info(f"  DDB tables  : 2")
        log_info(f"  KMS key     : {cfg(c,'KMS_KEY_ALIAS')}")
        log_info(f"  IAM roles   : 9")
        log_info(f"  Lambdas     : 9 (8 × {cfg(c,'AWS_REGION')}, 1 × {cfg(c,'ALEXA_FULFILLMENT_REGION')})")
        log_info(f"  API GW      : 6 routes wired, stage deployed")
        print(f"{'━' * 68}\n")

    except Exception as e:
        set_step("SUMMARY")
        log_error("Deployment FAILED")
        log_error(f"Exception: {e}")
        log_error(traceback.format_exc())
        log_error("Resources deployed before failure are left in place.")
        sys.exit(1)


if __name__ == "__main__":
    main()
