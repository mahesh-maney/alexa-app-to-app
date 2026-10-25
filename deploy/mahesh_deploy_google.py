#!/usr/bin/env python3
"""
mahesh_deploy_google.py — Zero-touch deployment of ALL Google Home resources.

Fully idempotent — safe to re-run at any time.
Prompts Y/n before each step; use --dry-run to validate config + show plan only
(steps 1 + 2 always run; steps 3-10 are skipped in dry-run mode).

Deploys:
  DDB tables   : 4  (sessions, auth_codes, tokens + 2 GSIs, oauth_transactions)
  IAM roles    : 9  (gh-start/complete/status/unlink/authorize/callback/token/fulfillment/proactive)
  Lambda funcs : 9  → AWS_REGION
  API GW routes: 10 methods on 8 resources + CORS
  Stage        : API_STAGE

Usage:
    python3 deploy/mahesh_deploy_google.py --config deploy/env.honeywell.qa.json
    python3 deploy/mahesh_deploy_google.py --config deploy/env.honeywell.qa.json --profile honeywell-qa
    python3 deploy/mahesh_deploy_google.py --config deploy/env.honeywell.qa.json --dry-run
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
    cfg, check_no_fill_in, check_required_keys, deploy_lambda,
    ensure_cognito_authorizer, ensure_gsi, ensure_role, ensure_table,
    get_or_create_resource, get_root_resource_id, load_config, log_create,
    log_error, log_info, log_ok, log_skip, log_warn, make_client, make_policy,
    package_lambda, preflight_aws, set_log_retention, set_step, smoke_test,
    st_cw, st_ddb, st_iot, st_s3, st_sm, st_stream, st_xray,
    step_header, verify_secret_accessible, wire_cors, wire_method,
)

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    print("ERROR: boto3 not installed.  Run: pip install boto3")
    sys.exit(1)


# ─── Per-step confirmation helper ─────────────────────────────────────────────

def _confirm_step(step_num: int, step_name: str, dry_run: bool) -> bool:
    """
    Returns True  → proceed with this step.
    Returns False → skip this step (dry-run or user said N).
    """
    if dry_run:
        print(f"\n  [DRY-RUN] Step {step_num} — {step_name}  (skipped)")
        return False
    try:
        ans = input(f"\n  Proceed with Step {step_num} — {step_name}? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)
    if ans in ("", "y", "yes"):
        return True
    print(f"  Skipping Step {step_num} — {step_name}")
    return False


# ─── Required config keys ──────────────────────────────────────────────────────

REQUIRED_KEYS = [
    "AWS_REGION", "ROLE_PREFIX", "ENV_LABEL", "API_STAGE", "API_BASE_URL",
    "COGNITO_USER_POOL_ID", "COGNITO_REGION", "COGNITO_CLIENT_ID",
    "COGNITO_HOSTED_UI_DOMAIN",
    "GH_SESSIONS_TABLE", "GH_AUTH_CODES_TABLE", "GH_TOKENS_TABLE",
    "GH_OAUTH_TRANSACTIONS_TABLE", "GH_TOKENS_ACCESS_TOKEN_INDEX",
    "GOOGLE_SECRET_ARN", "GOOGLE_SECRET_REGION",
    "GOOGLE_AGENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_REDIRECT_URI",
    "USER_DEVICE_DETAILS_TABLE", "DEVICE_STATE_TABLE", "SCENE_TABLE",
    "SCENE_GSI_NAME", "METADATA_BUCKET",
    "ENDPOINT_KEY_SECRET_ARN", "ENDPOINT_KEY_REGION",
    "GH_SA_SECRET_ARN", "GH_SA_SECRET_REGION",
    "DEVICE_MANUFACTURER", "DEVICE_MODEL", "IOT_DATA_ENDPOINT",
    "DEVICE_TABLE_SECRET_ARN", "DEVICE_TABLE_SECRET_REGION",
    "APP_NAME", "BRAND_TITLE", "BRAND_SUBTITLE", "TERMS_URL", "PRIVACY_POLICY_URL",
    "LOG_LEVEL", "SESSION_TTL_SECONDS", "AUTH_CODE_TTL_SECONDS",
    "ACCESS_TOKEN_TTL_SECONDS", "REFRESH_TOKEN_TTL_SECONDS",
    "TRANSACTION_TTL_SECONDS", "GH_DEEP_LINK_BASE_URL", "GH_DEEP_LINK_PATH",
    "CONSENT_PAGE_HEADING", "CONSENT_NOTICE_TEXT", "CONSENT_PERM_LABEL",
    "CONSENT_PERMISSIONS", "CONSENT_TRUST_TEXT", "CONSENT_DENY_LABEL", "CONSENT_ALLOW_LABEL",
]

# ─── Lambda definitions ────────────────────────────────────────────────────────
#  (fn_name, source_dir, timeout_s, memory_mb)

GH_LAMBDAS = [
    ("google_home_start_link",             "lambdas/google_home_start_link",           15, 256),
    ("google_home_app_link_complete",      "lambdas/google_home_app_link_complete",    15, 256),
    ("google_home_link_status",            "lambdas/google_home_link_status",          15, 256),
    ("google_home_app_unlink",             "lambdas/google_home_app_unlink",           30, 256),
    ("google_home_oauth_authorize",        "lambdas/google_home_oauth_authorize",      15, 256),
    ("google_home_oauth_cognito_callback", "lambdas/google_home_oauth_consent_screen", 15, 256),
    ("google_home_oauth_token",            "lambdas/google_home_oauth_token",          15, 256),
    ("google_home_fulfillment",            "lambdas/google_home_fulfillment",          15, 256),
    ("google_home_proactive",              "lambdas/google_home_proactive",            60, 256),
]


# ─── Per-Lambda environment variables ─────────────────────────────────────────

def _lambda_env(fn_name: str, c: dict, derived: dict) -> dict:
    region = cfg(c, "AWS_REGION")
    base = {"DATA_REGION": region, "LOG_LEVEL": cfg(c, "LOG_LEVEL")}

    if fn_name == "google_home_start_link":
        return {**base,
                "GH_SESSIONS_TABLE":    cfg(c, "GH_SESSIONS_TABLE"),
                "GOOGLE_AGENT_ID":      cfg(c, "GOOGLE_AGENT_ID"),
                "GOOGLE_CLIENT_ID":     cfg(c, "GOOGLE_CLIENT_ID"),
                "GOOGLE_REDIRECT_URI":  cfg(c, "GOOGLE_REDIRECT_URI"),
                "GOOGLE_SCOPE":         "profile",
                "OAUTH_BASE_URL":       cfg(c, "API_BASE_URL"),
                "SESSION_TTL_SECONDS":  cfg(c, "SESSION_TTL_SECONDS"),
                "GH_DEEP_LINK_BASE_URL": cfg(c, "GH_DEEP_LINK_BASE_URL"),
                "GH_DEEP_LINK_PATH":    cfg(c, "GH_DEEP_LINK_PATH")}

    if fn_name == "google_home_app_link_complete":
        return {**base,
                "GH_SESSIONS_TABLE": cfg(c, "GH_SESSIONS_TABLE"),
                "GH_TOKENS_TABLE":   cfg(c, "GH_TOKENS_TABLE")}

    if fn_name == "google_home_link_status":
        return {**base,
                "GH_TOKENS_TABLE":  cfg(c, "GH_TOKENS_TABLE"),
                "GOOGLE_AGENT_ID":  cfg(c, "GOOGLE_AGENT_ID")}

    if fn_name == "google_home_app_unlink":
        return {**base,
                "GH_TOKENS_TABLE":          cfg(c, "GH_TOKENS_TABLE"),
                "GOOGLE_SECRET_REGION":     cfg(c, "GOOGLE_SECRET_REGION"),
                "GOOGLE_SA_KEY_SECRET_ARN": cfg(c, "GOOGLE_SA_KEY_SECRET_ARN")}

    if fn_name == "google_home_oauth_authorize":
        return {**base,
                "GOOGLE_CLIENT_ID":           cfg(c, "GOOGLE_CLIENT_ID"),
                "ALLOWED_REDIRECT_URIS":      derived["ALLOWED_REDIRECT_URIS"],
                "COGNITO_CLIENT_ID":          cfg(c, "COGNITO_CLIENT_ID"),
                "COGNITO_HOSTED_UI_DOMAIN":   cfg(c, "COGNITO_HOSTED_UI_DOMAIN"),
                "COGNITO_CALLBACK_URI":       derived["COGNITO_CALLBACK_URI"],
                "GH_OAUTH_TRANSACTIONS_TABLE": cfg(c, "GH_OAUTH_TRANSACTIONS_TABLE"),
                "TRANSACTION_TTL_SECONDS":    cfg(c, "TRANSACTION_TTL_SECONDS"),
                "APP_NAME":                  cfg(c, "APP_NAME")}

    if fn_name == "google_home_oauth_cognito_callback":
        return {**base,
                "COGNITO_CLIENT_ID":           cfg(c, "COGNITO_CLIENT_ID"),
                "COGNITO_HOSTED_UI_DOMAIN":    cfg(c, "COGNITO_HOSTED_UI_DOMAIN"),
                "COGNITO_CALLBACK_URI":        derived["COGNITO_CALLBACK_URI"],
                "GH_OAUTH_TRANSACTIONS_TABLE": cfg(c, "GH_OAUTH_TRANSACTIONS_TABLE"),
                "GH_AUTH_CODES_TABLE":         cfg(c, "GH_AUTH_CODES_TABLE"),
                "AUTH_CODE_TTL_SECONDS":       cfg(c, "AUTH_CODE_TTL_SECONDS"),
                "APP_NAME":                    cfg(c, "APP_NAME"),
                "BRAND_TITLE":                 cfg(c, "BRAND_TITLE"),
                "BRAND_SUBTITLE":              cfg(c, "BRAND_SUBTITLE"),
                "TERMS_URL":                   cfg(c, "TERMS_URL"),
                "PRIVACY_POLICY_URL":          cfg(c, "PRIVACY_POLICY_URL"),
                "CONSENT_PAGE_HEADING":        cfg(c, "CONSENT_PAGE_HEADING"),
                "CONSENT_NOTICE_TEXT":         cfg(c, "CONSENT_NOTICE_TEXT"),
                "CONSENT_PERM_LABEL":          cfg(c, "CONSENT_PERM_LABEL"),
                "CONSENT_PERMISSIONS":         cfg(c, "CONSENT_PERMISSIONS"),
                "CONSENT_TRUST_TEXT":          cfg(c, "CONSENT_TRUST_TEXT"),
                "CONSENT_DENY_LABEL":          cfg(c, "CONSENT_DENY_LABEL"),
                "CONSENT_ALLOW_LABEL":         cfg(c, "CONSENT_ALLOW_LABEL")}

    if fn_name == "google_home_oauth_token":
        return {**base,
                "GH_AUTH_CODES_TABLE":         cfg(c, "GH_AUTH_CODES_TABLE"),
                "GH_TOKENS_TABLE":             cfg(c, "GH_TOKENS_TABLE"),
                "GOOGLE_CLIENT_SECRET_ARN":    cfg(c, "GOOGLE_SECRET_ARN"),
                "GOOGLE_SECRET_REGION":        cfg(c, "GOOGLE_SECRET_REGION"),
                "GOOGLE_AGENT_ID":             cfg(c, "GOOGLE_AGENT_ID"),
                "ACCESS_TOKEN_TTL_SECONDS":    cfg(c, "ACCESS_TOKEN_TTL_SECONDS"),
                "REFRESH_TOKEN_TTL_SECONDS":   cfg(c, "REFRESH_TOKEN_TTL_SECONDS")}

    if fn_name == "google_home_fulfillment":
        return {**base,
                "GH_TOKENS_TABLE":              cfg(c, "GH_TOKENS_TABLE"),
                "GH_TOKENS_ACCESS_TOKEN_INDEX": cfg(c, "GH_TOKENS_ACCESS_TOKEN_INDEX"),
                "USER_DEVICE_DETAILS_TABLE":    cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "DEVICE_STATE_TABLE":           cfg(c, "DEVICE_STATE_TABLE"),
                "SCENE_TABLE":                  cfg(c, "SCENE_TABLE"),
                "SCENE_GSI_NAME":               cfg(c, "SCENE_GSI_NAME"),
                "METADATA_BUCKET":              cfg(c, "METADATA_BUCKET"),
                "ENDPOINT_KEY_SECRET_ARN":      cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
                "ENDPOINT_KEY_REGION":          cfg(c, "ENDPOINT_KEY_REGION"),
                "DEVICE_MANUFACTURER":          cfg(c, "DEVICE_MANUFACTURER"),
                "DEVICE_MODEL":                 cfg(c, "DEVICE_MODEL")}

    if fn_name == "google_home_proactive":
        return {**base,
                "GH_TOKENS_TABLE":           cfg(c, "GH_TOKENS_TABLE"),
                "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
                "METADATA_BUCKET":           cfg(c, "METADATA_BUCKET"),
                "ENDPOINT_KEY_SECRET_ARN":   cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
                "ENDPOINT_KEY_REGION":       cfg(c, "ENDPOINT_KEY_REGION"),
                "GH_SA_SECRET_ARN":          cfg(c, "GH_SA_SECRET_ARN"),
                "GH_SA_SECRET_REGION":       cfg(c, "GH_SA_SECRET_REGION")}

    raise ValueError(f"Unknown GH Lambda: {fn_name}")


# ─── Step 1 — Load + Validate Config ──────────────────────────────────────────

def step1_config(config_path: str) -> tuple:
    step_header(1, "CONFIG")
    log_info(f"Loading config: {config_path}")
    c = load_config(config_path)
    log_info(f"ENV_LABEL   = {cfg(c, 'ENV_LABEL')}")
    log_info(f"AWS_REGION  = {cfg(c, 'AWS_REGION')}")
    log_info(f"ROLE_PREFIX = {cfg(c, 'ROLE_PREFIX')}")
    log_info(f"API_BASE_URL= {cfg(c, 'API_BASE_URL')}")
    log_info("Checking for FILL_IN placeholders...")
    check_no_fill_in(c, "mahesh_deploy_google.py")
    log_info("Checking required keys...")
    check_required_keys(c, REQUIRED_KEYS)

    base_url = cfg(c, "API_BASE_URL").rstrip("/")
    agent_id = cfg(c, "GOOGLE_AGENT_ID")
    derived = {
        "COGNITO_CALLBACK_URI": f"{base_url}/google-home/oauth/cognito/callback",
        "ALLOWED_REDIRECT_URIS": (
            f"https://oauth-redirect.googleusercontent.com/r/{agent_id},"
            f"https://oauth-redirect-sandbox.googleusercontent.com/r/{agent_id}"
        ),
    }
    log_ok(f"COGNITO_CALLBACK_URI  = {derived['COGNITO_CALLBACK_URI']}")
    log_ok(f"ALLOWED_REDIRECT_URIS = {derived['ALLOWED_REDIRECT_URIS']}")
    log_ok("Config validated — all required keys present, no FILL_IN values")
    return c, derived


# ─── Step 2 — Preflight ───────────────────────────────────────────────────────

def step2_preflight(session, c: dict) -> str:
    step_header(2, "PREFLIGHT")
    log_info("Verifying AWS credentials...")
    account_id = preflight_aws(session)
    log_ok(f"AWS credentials valid  account={account_id}  region={cfg(c, 'AWS_REGION')}")

    log_info("Verifying GOOGLE_SECRET_ARN is accessible...")
    verify_secret_accessible(session, cfg(c, "GOOGLE_SECRET_ARN"),
                             cfg(c, "GOOGLE_SECRET_REGION"), "GOOGLE_SECRET_ARN")
    log_ok("GOOGLE_SECRET_ARN accessible")

    if cfg(c, "GH_SA_SECRET_ARN"):
        log_info("Verifying GH_SA_SECRET_ARN is accessible...")
        verify_secret_accessible(session, cfg(c, "GH_SA_SECRET_ARN"),
                                 cfg(c, "GH_SA_SECRET_REGION"), "GH_SA_SECRET_ARN")
        log_ok("GH_SA_SECRET_ARN accessible")

    log_warn("REMINDER: GOOGLE_SECRET_ARN client_secret must EXACTLY match Google Actions Console")
    log_warn("  → Mismatch causes token exchange failures and 24-48h backoff in Google's OAuth system")
    return account_id


# ─── Step 3 — DynamoDB ────────────────────────────────────────────────────────

def step3_ddb(session, c: dict):
    step_header(3, "DYNAMODB TABLES")
    region = cfg(c, "AWS_REGION")
    ddb = make_client(session, "dynamodb", region)
    created_count = 0

    tables = [
        (cfg(c, "GH_SESSIONS_TABLE"),          "state",  "ttl"),
        (cfg(c, "GH_AUTH_CODES_TABLE"),         "code",   "ttl"),
        (cfg(c, "GH_TOKENS_TABLE"),             "userId", None),
        (cfg(c, "GH_OAUTH_TRANSACTIONS_TABLE"), "state",  "ttl"),
    ]

    for name, pk, ttl in tables:
        log_info(f"Checking table: {name}  pk={pk}  ttl_attr={ttl or 'none'}")
        if ensure_table(ddb, name, pk, ttl_attr=ttl):
            created_count += 1

    tokens_table = cfg(c, "GH_TOKENS_TABLE")
    log_info(f"Checking GSI accessToken-index on {tokens_table}...")
    ensure_gsi(ddb, tokens_table, "accessToken-index", "accessToken")
    log_info(f"Checking GSI refreshToken-index on {tokens_table}...")
    ensure_gsi(ddb, tokens_table, "refreshToken-index", "refreshToken")

    log_ok(f"DDB complete  tables={len(tables)}  created={created_count}  "
           f"already_existed={len(tables) - created_count}  GSIs checked=2")


# ─── Step 4 — API Gateway ─────────────────────────────────────────────────────

def step4_apigw(session, c: dict, account_id: str) -> dict:
    step_header(4, "API GATEWAY")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)

    api_gw_id = cfg(c, "API_GATEWAY_ID")
    if api_gw_id:
        log_info(f"Verifying existing API {api_gw_id} in {region}...")
        try:
            apigw.get_rest_api(restApiId=api_gw_id)
            log_ok(f"API exists  id={api_gw_id}")
        except ClientError as e:
            log_error(f"API {api_gw_id} not found: {e}")
            sys.exit(1)
    else:
        log_create(f"API_GATEWAY_ID empty — creating new REST API (REGIONAL) in {region}...")
        resp = apigw.create_rest_api(
            name=f"{cfg(c, 'ROLE_PREFIX')}-smarthome-api",
            endpointConfiguration={"types": ["REGIONAL"]},
        )
        api_gw_id = resp["id"]
        log_ok(f"New API created  id={api_gw_id}  name={cfg(c, 'ROLE_PREFIX')}-smarthome-api")
        log_warn(f"Save API_GATEWAY_ID={api_gw_id} in your config for future re-runs")

    log_info(f"Setting up Cognito authorizer for pool {cfg(c, 'COGNITO_USER_POOL_ID')}...")
    pool_arn = (f"arn:aws:cognito-idp:{cfg(c, 'COGNITO_REGION')}:{account_id}"
                f":userpool/{cfg(c, 'COGNITO_USER_POOL_ID')}")
    authorizer_id = ensure_cognito_authorizer(apigw, api_gw_id, pool_arn)
    log_ok(f"Cognito authorizer ready  id={authorizer_id}")

    root_id = get_root_resource_id(apigw, api_gw_id)
    log_ok(f"API GW ready  id={api_gw_id}  authorizer={authorizer_id}  root={root_id}")
    return {"api_gw_id": api_gw_id, "authorizer_id": authorizer_id, "root_id": root_id}


# ─── Step 5 — IAM Roles ───────────────────────────────────────────────────────

def step5_iam(session, c: dict, account_id: str) -> dict:
    step_header(5, "IAM ROLES")
    iam      = make_client(session, "iam")
    region   = cfg(c, "AWS_REGION")
    role_pre = cfg(c, "ROLE_PREFIX")

    gh_sessions   = cfg(c, "GH_SESSIONS_TABLE")
    gh_tokens     = cfg(c, "GH_TOKENS_TABLE")
    gh_auth_codes = cfg(c, "GH_AUTH_CODES_TABLE")
    gh_txn        = cfg(c, "GH_OAUTH_TRANSACTIONS_TABLE")
    udd           = cfg(c, "USER_DEVICE_DETAILS_TABLE")
    ds            = cfg(c, "DEVICE_STATE_TABLE")
    scene         = cfg(c, "SCENE_TABLE")
    bucket        = cfg(c, "METADATA_BUCKET")
    ep_arn        = cfg(c, "ENDPOINT_KEY_SECRET_ARN")
    goog_secret   = cfg(c, "GOOGLE_SECRET_ARN")
    gh_sa_arn     = cfg(c, "GH_SA_SECRET_ARN")
    cw            = st_cw(account_id, region)
    xr            = st_xray()

    roles = [
        (f"{role_pre}-gh-start-role", make_policy(
            st_ddb(["dynamodb:PutItem"], account_id, region, gh_sessions), cw, xr)),

        (f"{role_pre}-gh-complete-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, gh_sessions),
            st_ddb(["dynamodb:GetItem"], account_id, region, gh_tokens), cw, xr)),

        (f"{role_pre}-gh-status-role", make_policy(
            st_ddb(["dynamodb:GetItem"], account_id, region, gh_tokens), cw, xr)),

        (f"{role_pre}-gh-unlink-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:DeleteItem"], account_id, region, gh_tokens),
            st_sm(*([goog_secret] + ([cfg(c, "GOOGLE_SA_KEY_SECRET_ARN")]
                                     if cfg(c, "GOOGLE_SA_KEY_SECRET_ARN") else []))),
            cw, xr)),

        (f"{role_pre}-gh-authorize-role", make_policy(
            st_ddb(["dynamodb:PutItem"], account_id, region, gh_txn), cw, xr)),

        (f"{role_pre}-gh-callback-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                   account_id, region, gh_txn),
            st_ddb(["dynamodb:PutItem"], account_id, region, gh_auth_codes), cw, xr)),

        (f"{role_pre}-gh-token-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:DeleteItem"], account_id, region, gh_auth_codes),
            st_ddb(["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"],
                   account_id, region, gh_tokens),
            st_sm(goog_secret), cw, xr)),

        (f"{role_pre}-gh-fulfillment-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:Query", "dynamodb:DeleteItem"],
                   account_id, region, gh_tokens, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd,   idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, ds,    idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
            st_s3(bucket), st_sm(ep_arn),
            st_iot(account_id, region), cw, xr)),

        (f"{role_pre}-gh-proactive-role", make_policy(
            st_ddb(["dynamodb:GetItem", "dynamodb:Query"],
                   account_id, region, gh_tokens, idx=True),
            st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
            st_s3(bucket), st_sm(ep_arn, gh_sa_arn),
            st_stream(account_id, region, udd), cw, xr)),
    ]

    role_arns = {}
    created_count = 0
    for role_name, policy in roles:
        log_info(f"Processing role: {role_name}")
        was_created, arn = ensure_role(iam, role_name, policy)
        role_arns[role_name] = arn
        if was_created:
            created_count += 1

    log_ok(f"IAM roles complete  total={len(roles)}  created={created_count}  "
           f"updated={len(roles) - created_count}")
    return role_arns


# ─── Step 6 — Package Lambdas ─────────────────────────────────────────────────

def step6_package(c: dict) -> dict:
    step_header(6, "PACKAGE LAMBDAS")
    env_label  = cfg(c, "ENV_LABEL")
    build_root = f"/tmp/deploy_build_{env_label}"

    if os.path.exists(build_root):
        log_info(f"Removing old build dir: {build_root}")
        shutil.rmtree(build_root)
    os.makedirs(build_root)
    log_info(f"Build dir: {build_root}")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    zips = {}
    for fn_name, src_rel, _, _ in GH_LAMBDAS:
        src = os.path.join(repo_root, src_rel)
        log_info(f"Packaging {fn_name}  src={src}")
        if not os.path.exists(src):
            log_error(f"Source directory not found: {src}")
            sys.exit(1)
        zips[fn_name] = package_lambda(src, fn_name, build_root)
        log_ok(f"Packaged {fn_name}  → {zips[fn_name]}")

    log_ok(f"Packaging complete  {len(zips)} zips built")
    return zips


# ─── Step 7 — Deploy Lambdas ──────────────────────────────────────────────────

def step7_lambdas(session, c: dict, role_arns: dict, zips: dict, derived: dict) -> dict:
    step_header(7, "LAMBDA DEPLOY")
    region  = cfg(c, "AWS_REGION")
    fn_arns = {}

    role_map = {
        "google_home_start_link":             f"{cfg(c, 'ROLE_PREFIX')}-gh-start-role",
        "google_home_app_link_complete":       f"{cfg(c, 'ROLE_PREFIX')}-gh-complete-role",
        "google_home_link_status":             f"{cfg(c, 'ROLE_PREFIX')}-gh-status-role",
        "google_home_app_unlink":              f"{cfg(c, 'ROLE_PREFIX')}-gh-unlink-role",
        "google_home_oauth_authorize":         f"{cfg(c, 'ROLE_PREFIX')}-gh-authorize-role",
        "google_home_oauth_cognito_callback":  f"{cfg(c, 'ROLE_PREFIX')}-gh-callback-role",
        "google_home_oauth_token":             f"{cfg(c, 'ROLE_PREFIX')}-gh-token-role",
        "google_home_fulfillment":             f"{cfg(c, 'ROLE_PREFIX')}-gh-fulfillment-role",
        "google_home_proactive":               f"{cfg(c, 'ROLE_PREFIX')}-gh-proactive-role",
    }

    for fn_name, _, timeout, memory in GH_LAMBDAS:
        log_info(f"Deploying {fn_name}  timeout={timeout}s  memory={memory}MB  region={region}")
        env_vars = _lambda_env(fn_name, c, derived)
        arn = deploy_lambda(session, fn_name, zips[fn_name],
                            role_arns[role_map[fn_name]],
                            timeout, memory, env_vars, region)
        fn_arns[fn_name] = arn
        log_ok(f"Deployed {fn_name}  arn={arn}")
        set_log_retention(session, fn_name, region)
        log_ok(f"Log retention set  /aws/lambda/{fn_name}  30 days")

    log_ok(f"All Lambdas deployed: {len(fn_arns)}/{len(GH_LAMBDAS)}  region={region}")
    return fn_arns


# ─── Step 8 — API GW Routes ───────────────────────────────────────────────────

def step8_routes(session, c: dict, apigw_info: dict, fn_arns: dict, account_id: str):
    step_header(8, "API GW ROUTES")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)
    api_id  = apigw_info["api_gw_id"]
    auth_id = apigw_info["authorizer_id"]
    root_id = apigw_info["root_id"]

    def wm(resource_id, method, fn_name, auth="COGNITO_USER_POOLS"):
        log_info(f"Wiring {method} → {fn_name}  auth={auth}")
        wire_method(session, apigw, api_id, resource_id, method,
                    fn_arns[fn_name], region, account_id, fn_name,
                    auth_type=auth,
                    authorizer_id=auth_id if auth == "COGNITO_USER_POOLS" else None)
        log_ok(f"Wired {method} → {fn_name}")

    log_info("Building resource tree: /api/v1/voice/google-home/account-linking/deep-link/...")
    api_res   = get_or_create_resource(apigw, api_id, root_id,   "api")
    v1_res    = get_or_create_resource(apigw, api_id, api_res,   "v1")
    voice_res = get_or_create_resource(apigw, api_id, v1_res,    "voice")
    gh_app    = get_or_create_resource(apigw, api_id, voice_res, "google-home")
    acct_link = get_or_create_resource(apigw, api_id, gh_app,    "account-linking")
    deep_link = get_or_create_resource(apigw, api_id, acct_link, "deep-link")
    start_res = get_or_create_resource(apigw, api_id, deep_link, "start")
    comp_res  = get_or_create_resource(apigw, api_id, deep_link, "complete")
    log_ok("Resource tree /api/v1/voice/google-home/... built")

    log_info("Building resource tree: /google-home/oauth/...")
    gh_root   = get_or_create_resource(apigw, api_id, root_id,   "google-home")
    oauth_res = get_or_create_resource(apigw, api_id, gh_root,   "oauth")
    auth_res  = get_or_create_resource(apigw, api_id, oauth_res, "authorize")
    token_res = get_or_create_resource(apigw, api_id, oauth_res, "token")
    cog_res   = get_or_create_resource(apigw, api_id, oauth_res, "cognito")
    cb_res    = get_or_create_resource(apigw, api_id, cog_res,   "callback")
    con_res   = get_or_create_resource(apigw, api_id, oauth_res, "consent")
    ful_res   = get_or_create_resource(apigw, api_id, gh_root,   "fulfillment")
    log_ok("Resource tree /google-home/oauth/... built")

    log_info("Wiring methods and CORS...")

    wm(acct_link, "GET",    "google_home_link_status")
    wm(acct_link, "DELETE", "google_home_app_unlink")
    wire_cors(apigw, api_id, acct_link)

    wm(start_res, "POST", "google_home_start_link")
    wire_cors(apigw, api_id, start_res)

    wm(comp_res, "POST", "google_home_app_link_complete")
    wire_cors(apigw, api_id, comp_res)

    wm(auth_res, "GET",  "google_home_oauth_authorize", auth="NONE")
    wm(auth_res, "POST", "google_home_oauth_authorize", auth="NONE")
    wire_cors(apigw, api_id, auth_res)

    wm(cb_res, "GET", "google_home_oauth_cognito_callback", auth="NONE")
    wire_cors(apigw, api_id, cb_res)

    wm(con_res, "POST", "google_home_oauth_cognito_callback", auth="NONE")
    wire_cors(apigw, api_id, con_res)

    wm(token_res, "POST", "google_home_oauth_token", auth="NONE")
    wire_cors(apigw, api_id, token_res)

    wm(ful_res, "POST", "google_home_fulfillment", auth="NONE")
    wire_cors(apigw, api_id, ful_res)

    log_ok("Routes complete  10 methods wired on 8 resources + CORS on all")


# ─── Step 9 — Deploy Stage ────────────────────────────────────────────────────

def step9_stage(session, c: dict, apigw_info: dict):
    step_header(9, "API GW STAGE DEPLOY")
    region = cfg(c, "AWS_REGION")
    apigw  = make_client(session, "apigateway", region)
    api_id = apigw_info["api_gw_id"]
    stage  = cfg(c, "API_STAGE")
    log_info(f"Creating deployment on API {api_id}  stage={stage}...")
    resp = apigw.create_deployment(restApiId=api_id, stageName=stage)
    log_ok(f"Stage deployed  stage={stage}  deployment_id={resp['id']}")
    log_info("Sleeping 5s for API GW cold-start readiness...")
    time.sleep(5)
    log_ok("Stage ready")


# ─── Step 10 — Smoke Tests ────────────────────────────────────────────────────

def step10_smoke(c: dict):
    step_header(10, "SMOKE TESTS")
    base = cfg(c, "API_BASE_URL").rstrip("/")
    tests = [
        ("POST",   f"{base}/api/v1/voice/google-home/account-linking/deep-link/start",    401),
        ("POST",   f"{base}/api/v1/voice/google-home/account-linking/deep-link/complete", 401),
        ("GET",    f"{base}/api/v1/voice/google-home/account-linking",                    401),
        ("DELETE", f"{base}/api/v1/voice/google-home/account-linking",                    401),
        ("GET",    f"{base}/google-home/oauth/authorize",                                  400),
        ("GET",    f"{base}/google-home/oauth/cognito/callback",                           400),
        ("POST",   f"{base}/google-home/oauth/token",                                      400),
    ]
    passed = 0
    for method, url, expected in tests:
        log_info(f"Smoke test  {method} {url}  expect={expected}")
        ok = smoke_test(method, url, expected)
        if ok:
            log_ok(f"PASS  {method} {url}")
            passed += 1
        else:
            log_warn(f"FAIL  {method} {url}  (expected {expected})")

    log_ok(f"Smoke tests: {passed}/{len(tests)} passed")
    if passed < len(tests):
        log_warn(f"{len(tests) - passed} test(s) failed — check API GW stage deployment")


# ─── Step 11 — Post-Deploy Summary ────────────────────────────────────────────

def step11_summary(c: dict, account_id: str):
    step_header(11, "POST-DEPLOY SUMMARY")
    base   = cfg(c, "API_BASE_URL").rstrip("/")
    region = cfg(c, "AWS_REGION")
    agent  = cfg(c, "GOOGLE_AGENT_ID")

    print(f"\n  ── Google Home Developer Console — configure these values ────────────────")
    print(f"  Fulfillment URL   : {base}/google-home/fulfillment")
    print(f"  Authorization URL : {base}/google-home/oauth/authorize")
    print(f"  Token URL         : {base}/google-home/oauth/token")
    print(f"  Client ID         : {cfg(c, 'GOOGLE_CLIENT_ID')}")
    print(f"  Client Secret     : (from secret {cfg(c, 'GOOGLE_SECRET_ARN')})")
    print(f"  Agent / Project ID: {agent}")
    print(f"\n  ── Cognito App Client — add allowed callback URL ─────────────────────────")
    print(f"  {base}/google-home/oauth/cognito/callback")
    print(f"\n  ── Manual step (cannot be automated) ────────────────────────────────────")
    print(f"  Enable DynamoDB stream on {cfg(c, 'USER_DEVICE_DETAILS_TABLE')} (NEW_AND_OLD_IMAGES),")
    print(f"  then create event source mapping for google_home_proactive:")
    print(f"     aws dynamodb update-table \\")
    print(f"       --table-name {cfg(c, 'USER_DEVICE_DETAILS_TABLE')} \\")
    print(f"       --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \\")
    print(f"       --region {region}")
    print()
    print(f"  ── Lambda ARNs ───────────────────────────────────────────────────────────")
    for fn_name, _, _, _ in GH_LAMBDAS:
        print(f"  {fn_name:<45} : arn:aws:lambda:{region}:{account_id}:function:{fn_name}")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Zero-touch Google Home deployment — prompts Y/n per step, optional --dry-run"
    )
    parser.add_argument("--config",   required=True,      help="Path to env JSON config file")
    parser.add_argument("--profile",  default=None,       help="AWS CLI profile name")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Validate config + preflight only; skip all AWS resource creation")
    args = parser.parse_args()
    dry_run = args.dry_run

    start_time = datetime.now()

    print("\n" + "━" * 68)
    print("  mahesh_deploy_google.py — Zero-Touch Google Home Deployment")
    print("━" * 68 + "\n")

    # ── Steps 1 + 2 always run ────────────────────────────────────────────────
    c, derived = step1_config(args.config)

    session_kwargs = {"region_name": cfg(c, "AWS_REGION")}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    account_id = step2_preflight(session, c)

    # ── Deployment plan banner ────────────────────────────────────────────────
    print(f"\n  {'━' * 60}")
    print(f"  ZERO-TOUCH DEPLOYMENT PLAN")
    print(f"  ENV_LABEL    : {cfg(c, 'ENV_LABEL')}")
    print(f"  AWS_REGION   : {cfg(c, 'AWS_REGION')}")
    print(f"  ACCOUNT_ID   : {account_id}")
    print(f"  ROLE_PREFIX  : {cfg(c, 'ROLE_PREFIX')}")
    print(f"  API_GATEWAY  : {cfg(c, 'API_GATEWAY_ID') or '(new — will be created)'}  →  {cfg(c, 'API_BASE_URL')}")
    print(f"  Will create/update:")
    print(f"    DDB tables   : 4  (sessions, auth_codes, tokens + 2 GSIs, oauth_transactions)")
    print(f"    IAM roles    : 9  (gh-start/complete/status/unlink/authorize/callback/token/fulfillment/proactive)")
    print(f"    Lambda funcs : 9  →  {cfg(c, 'AWS_REGION')}")
    print(f"    API GW routes: 10 methods on 8 resources + CORS")
    print(f"    Stage        : {cfg(c, 'API_STAGE')}")
    if dry_run:
        print(f"  MODE: DRY-RUN — config + preflight validated; no AWS resources will be created")
    else:
        print(f"  MODE: INTERACTIVE — will prompt Y/n before each step")
    print(f"  {'━' * 60}\n")

    # ── Deploy ────────────────────────────────────────────────────────────────
    try:
        set_step("START")
        log_info(f"Deployment started  env={cfg(c, 'ENV_LABEL')}  account={account_id}"
                 + ("  [DRY-RUN]" if dry_run else ""))

        apigw_info = role_arns = zips = fn_arns = None

        if _confirm_step(3, "DYNAMODB TABLES", dry_run):
            step3_ddb(session, c)

        if _confirm_step(4, "API GATEWAY", dry_run):
            apigw_info = step4_apigw(session, c, account_id)

        if _confirm_step(5, "IAM ROLES", dry_run):
            role_arns = step5_iam(session, c, account_id)

        if _confirm_step(6, "PACKAGE LAMBDAS", dry_run):
            zips = step6_package(c)

        if _confirm_step(7, "LAMBDA DEPLOY", dry_run):
            if role_arns is None or zips is None:
                log_warn("Step 7 skipped — Step 5 (IAM) or Step 6 (package) was not run")
            else:
                fn_arns = step7_lambdas(session, c, role_arns, zips, derived)

        if _confirm_step(8, "API GW ROUTES", dry_run):
            if apigw_info is None or fn_arns is None:
                log_warn("Step 8 skipped — Step 4 (API GW) or Step 7 (Lambdas) was not run")
            else:
                step8_routes(session, c, apigw_info, fn_arns, account_id)

        if _confirm_step(9, "API GW STAGE DEPLOY", dry_run):
            if apigw_info is None:
                log_warn("Step 9 skipped — Step 4 (API GW) was not run")
            else:
                step9_stage(session, c, apigw_info)

        if _confirm_step(10, "SMOKE TESTS", dry_run):
            step10_smoke(c)

        step11_summary(c, account_id)

        elapsed = (datetime.now() - start_time).seconds
        set_step("SUMMARY")
        print(f"\n{'━' * 68}")
        if dry_run:
            log_ok(f"mahesh_deploy_google.py DRY-RUN COMPLETE  env={cfg(c, 'ENV_LABEL')}")
            log_info("No AWS resources were created — config + preflight validated successfully")
        else:
            log_ok(f"mahesh_deploy_google.py COMPLETED  env={cfg(c, 'ENV_LABEL')}")
        log_info(f"Duration      : {elapsed // 60}m {elapsed % 60}s")
        log_info(f"Account       : {account_id}")
        log_info(f"Region        : {cfg(c, 'AWS_REGION')}")
        print(f"{'━' * 68}\n")

    except Exception as e:
        set_step("SUMMARY")
        log_error("Deployment FAILED")
        log_error(f"Exception: {e}")
        log_error(traceback.format_exc())
        log_error("Resources deployed before failure are left in place — script is idempotent, safe to re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
