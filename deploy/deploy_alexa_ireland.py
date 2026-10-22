#!/usr/bin/env python3
"""
deploy_alexa_ireland.py — Deploy ONLY alexa_fulfillment to ALEXA_FULFILLMENT_REGION (eu-west-1).

Use this for hotfixes to alexa_fulfillment without re-deploying the full Alexa stack.
The KMS key and all DDB tables must already exist (created by deploy_alexa.py).

Fully idempotent — safe to re-run.

Usage:
    python3 deploy/deploy_alexa_ireland.py --config deploy/env.digilux.qa.json
    python3 deploy/deploy_alexa_ireland.py --config deploy/env.honeywell.prod.json --dry-run
"""

import argparse
import os
import shutil
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from deploy_common import (
    cfg, check_no_fill_in, check_required_keys, confirm_deploy, deploy_lambda,
    ensure_role, load_config, log_error, log_info, log_ok, log_skip, log_warn,
    make_client, make_policy, package_lambda, preflight_aws, set_log_retention,
    set_step, st_cw, st_ddb, st_iot, st_kms, st_s3, st_sm, st_xray,
    step_header, wire_method,
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
    "ALEXA_TOKENS_TABLE", "LWA_SECRET_ARN", "LWA_SECRET_REGION", "KMS_KEY_ALIAS",
    "USER_DEVICE_DETAILS_TABLE", "DEVICE_STATE_TABLE", "SCENE_TABLE",
    "METADATA_BUCKET", "ENDPOINT_KEY_SECRET_ARN", "ENDPOINT_KEY_REGION",
    "COGNITO_USER_POOL_ID", "COGNITO_REGION",
    "IOT_DATA_ENDPOINT", "DEVICE_TABLE_SECRET_ARN", "DEVICE_TABLE_SECRET_REGION",
    "ALEXA_GATEWAY_URL", "LOG_LEVEL", "ALEXA_SKILL_ID",
]


# ─── Step 1 — Load + Validate Config ─────────────────────────────────────────

def step1_config(config_path: str) -> dict:
    step_header(1, "CONFIG")
    log_info(f"Loaded: {config_path}")
    c = load_config(config_path)
    log_info(f"ENV_LABEL={cfg(c,'ENV_LABEL')}")
    log_info(f"AWS_REGION={cfg(c,'AWS_REGION')}  (main region — DDB tables live here)")
    log_info(f"ALEXA_FULFILLMENT_REGION={cfg(c,'ALEXA_FULFILLMENT_REGION')}  (Ireland — Lambda deploys here)")
    log_info("Checking required keys...")
    check_no_fill_in(c, "deploy_alexa_ireland.py")
    check_required_keys(c, REQUIRED_KEYS)
    log_ok("All required config keys present")
    return c


# ─── Step 2 — Preflight Checks ───────────────────────────────────────────────

def step2_preflight(session, c: dict) -> tuple:
    step_header(2, "PREFLIGHT")

    log_info("Verifying AWS credentials...")
    account_id = preflight_aws(session)

    # Resolve KMS key ARN — must already exist (created by deploy_alexa.py)
    region = cfg(c, "AWS_REGION")
    alias  = cfg(c, "KMS_KEY_ALIAS")
    log_info(f"Resolving KMS alias: {alias} in {region}...")
    try:
        kms = make_client(session, "kms", region)
        aliases = kms.list_aliases().get("Aliases", [])
        match = next((a for a in aliases if a.get("AliasName") == alias), None)
        if not match:
            log_error(f"KMS alias '{alias}' not found in {region}")
            log_error("The KMS key is created by deploy_alexa.py — run that first")
            set_step("SUMMARY")
            log_error("Deployment aborted at preflight.")
            sys.exit(1)
        key_arn = kms.describe_key(KeyId=alias)["KeyMetadata"]["Arn"]
        log_ok(f"KMS alias found  KeyArn={key_arn}")
    except ClientError as e:
        log_error(f"KMS lookup failed: {e}")
        sys.exit(1)

    # Check Ireland reachable
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    log_info(f"Checking ALEXA_FULFILLMENT_REGION ({irl_region}) connectivity...")
    try:
        make_client(session, "lambda", irl_region).list_functions(MaxItems=1)
        log_ok(f"{irl_region} reachable")
    except Exception as e:
        log_error(f"Cannot reach {irl_region}: {e}")
        sys.exit(1)

    # Check Lambda source dir
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo_root, "lambdas/alexa_fulfillment")
    log_info(f"Checking Lambda source: {src}")
    if not os.path.exists(src):
        log_error(f"Source directory not found: {src}")
        sys.exit(1)
    log_ok("Source directory found")

    return account_id, key_arn


# ─── Step 3 — IAM Role ───────────────────────────────────────────────────────

def step3_iam(session, c: dict, account_id: str, kms_key_arn: str) -> str:
    step_header(3, "IAM ROLE")
    iam         = make_client(session, "iam")
    region      = cfg(c, "AWS_REGION")
    irl_region  = cfg(c, "ALEXA_FULFILLMENT_REGION")
    role_name   = f"{cfg(c,'ROLE_PREFIX')}-alexa-fulfillment-role"
    tokens      = cfg(c, "ALEXA_TOKENS_TABLE")
    udd         = cfg(c, "USER_DEVICE_DETAILS_TABLE")
    ds          = cfg(c, "DEVICE_STATE_TABLE")
    scene       = cfg(c, "SCENE_TABLE")
    bucket      = cfg(c, "METADATA_BUCKET")
    lwa_arn     = cfg(c, "LWA_SECRET_ARN")
    ep_arn      = cfg(c, "ENDPOINT_KEY_SECRET_ARN")
    dt_arn      = cfg(c, "DEVICE_TABLE_SECRET_ARN")

    policy = make_policy(
        st_ddb(["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
               account_id, region, tokens),
        st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, udd, idx=True),
        st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, ds,  idx=True),
        st_ddb(["dynamodb:Query", "dynamodb:GetItem"], account_id, region, scene, idx=True),
        st_s3(bucket),
        st_sm(lwa_arn, ep_arn, dt_arn),
        st_kms(kms_key_arn, ["kms:Decrypt"]),
        st_iot(account_id, region),
        st_cw(account_id, irl_region),   # CloudWatch Logs in Ireland (where Lambda runs)
        st_xray(),
    )

    log_info(f"Policy covers: DDB({region}), S3, SecretsManager, KMS, IoT, CWLogs({irl_region}), XRay")
    _, role_arn = ensure_role(iam, role_name, policy)
    return role_arn


# ─── Step 4 — Package Lambda ─────────────────────────────────────────────────

def step4_package(c: dict) -> str:
    step_header(4, "PACKAGE LAMBDA")
    env_label  = cfg(c, "ENV_LABEL")
    build_root = f"/tmp/deploy_build_{env_label}_ireland"
    if os.path.exists(build_root):
        shutil.rmtree(build_root)
    os.makedirs(build_root)
    log_info(f"Build dir: {build_root}")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo_root, "lambdas/alexa_fulfillment")
    return package_lambda(src, "alexa_fulfillment", build_root)


# ─── Step 5 — Deploy Lambda ──────────────────────────────────────────────────

def step5_deploy(session, c: dict, role_arn: str, zip_path: str,
                 kms_key_arn: str, account_id: str) -> str:
    step_header(5, "DEPLOY LAMBDA")
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    main_region = cfg(c, "AWS_REGION")

    log_info(f"Deploying alexa_fulfillment to {irl_region}")
    log_info(f"timeout=30s  memory=256MB  runtime=python3.12")
    log_info(f"NOTE: DATA_REGION={main_region} — DDB tables live in main region, not Ireland")

    env_vars = {
        "DATA_REGION":               main_region,
        "LOG_LEVEL":                 cfg(c, "LOG_LEVEL"),
        "LWA_TOKENS_TABLE":          cfg(c, "ALEXA_TOKENS_TABLE"),
        "LWA_SECRET_ARN":            cfg(c, "LWA_SECRET_ARN"),
        "LWA_SECRET_REGION":         cfg(c, "LWA_SECRET_REGION"),
        "KMS_KEY_ARN":               kms_key_arn,
        "COGNITO_USER_POOL_ID":      cfg(c, "COGNITO_USER_POOL_ID"),
        "COGNITO_REGION":            cfg(c, "COGNITO_REGION"),
        "USER_DEVICE_DETAILS_TABLE": cfg(c, "USER_DEVICE_DETAILS_TABLE"),
        "DEVICE_STATE_TABLE":        cfg(c, "DEVICE_STATE_TABLE"),
        "SCENE_TABLE":               cfg(c, "SCENE_TABLE"),
        "METADATA_BUCKET":           cfg(c, "METADATA_BUCKET"),
        "ENDPOINT_KEY_SECRET_ARN":   cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
        "ENDPOINT_KEY_REGION":       cfg(c, "ENDPOINT_KEY_REGION"),
        "DEVICE_TABLE_SECRET_ARN":   cfg(c, "DEVICE_TABLE_SECRET_ARN"),
        "DEVICE_TABLE_SECRET_REGION":cfg(c, "DEVICE_TABLE_SECRET_REGION"),
        "ALEXA_GATEWAY_URL":         cfg(c, "ALEXA_GATEWAY_URL"),
        "IOT_DATA_ENDPOINT":         cfg(c, "IOT_DATA_ENDPOINT"),
    }

    arn = deploy_lambda(session, "alexa_fulfillment", zip_path, role_arn,
                        30, 256, env_vars, irl_region)
    log_ok(f"ARN: {arn}")
    return arn


# ─── Step 6 — CloudWatch Log Retention ───────────────────────────────────────

def step6_logs(session, c: dict):
    step_header(6, "LOG RETENTION")
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    log_info(f"Setting log retention: /aws/lambda/alexa_fulfillment → 30 days ({irl_region})")
    set_log_retention(session, "alexa_fulfillment", irl_region, days=30)


# ─── Step 7 — Post-Deploy Instructions ───────────────────────────────────────

def step7_post_deploy(c: dict, account_id: str, fn_arn: str):
    step_header(7, "POST-DEPLOY INSTRUCTIONS")
    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    skill_id   = cfg(c, "ALEXA_SKILL_ID")

    log_info(f"Lambda ARN: {fn_arn}")
    log_warn("The following CANNOT be automated — complete them manually:\n")
    print(f"  1. Add Alexa Smart Home trigger (resource policy):")
    print(f"     aws lambda add-permission \\")
    print(f"       --function-name alexa_fulfillment \\")
    print(f"       --statement-id alexa-smart-home \\")
    print(f"       --action lambda:InvokeFunction \\")
    print(f"       --principal alexa-connectedhome.amazon.com \\")
    print(f"       --event-source-token {skill_id} \\")
    print(f"       --region {irl_region}")
    print()
    print(f"     Note: if statement-id already exists, remove it first:")
    print(f"     aws lambda remove-permission \\")
    print(f"       --function-name alexa_fulfillment \\")
    print(f"       --statement-id alexa-smart-home \\")
    print(f"       --region {irl_region}")
    print()
    print(f"  2. Register Lambda ARN in Alexa Developer Console:")
    print(f"     Developer Console → Your Skill → Build → Endpoint")
    print(f"     Default endpoint: {fn_arn}")
    print(f"     → Save and re-build the skill model")
    print()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Deploy alexa_fulfillment to ALEXA_FULFILLMENT_REGION (Ireland) only"
    )
    parser.add_argument("--config",  required=True, help="Path to env JSON config file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + preflight (resolve KMS) only, no deploy")
    parser.add_argument("--profile", default=None, help="AWS CLI profile")
    args = parser.parse_args()

    start_time = datetime.now()

    print("\n" + "━" * 68)
    print("  deploy_alexa_ireland.py — alexa_fulfillment (Ireland) deployment")
    print("━" * 68 + "\n")

    c = step1_config(args.config)

    session_kwargs = {"region_name": cfg(c, "AWS_REGION")}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    account_id, kms_key_arn = step2_preflight(session, c)

    irl_region = cfg(c, "ALEXA_FULFILLMENT_REGION")
    print(f"\n  {'━' * 58}")
    print(f"  DEPLOYMENT PLAN — deploy_alexa_ireland.py")
    print(f"  ENV_LABEL                : {cfg(c,'ENV_LABEL')}")
    print(f"  AWS_REGION               : {cfg(c,'AWS_REGION')}  (DDB + IAM + KMS)")
    print(f"  ALEXA_FULFILLMENT_REGION : {irl_region}  (Lambda deploy target)")
    print(f"  KMS_KEY_ARN              : {kms_key_arn}")
    print(f"  Will create/update:")
    print(f"    IAM role : {cfg(c,'ROLE_PREFIX')}-alexa-fulfillment-role")
    print(f"    Lambda   : alexa_fulfillment → {irl_region}")
    print(f"    CW Logs  : retention 30 days in {irl_region}")
    print(f"  NOT touched: API GW, DDB tables, KMS key, any other Lambda")
    print(f"  {'━' * 58}\n")

    if not confirm_deploy(cfg(c, "ENV_LABEL"), "deploy_alexa_ireland.py", args.dry_run):
        return

    try:
        role_arn = step3_iam(session, c, account_id, kms_key_arn)
        zip_path = step4_package(c)
        fn_arn   = step5_deploy(session, c, role_arn, zip_path, kms_key_arn, account_id)
        step6_logs(session, c)
        step7_post_deploy(c, account_id, fn_arn)

        elapsed = (datetime.now() - start_time).seconds
        set_step("SUMMARY")
        print(f"\n{'━' * 68}")
        log_ok(f"deploy_alexa_ireland.py completed  env={cfg(c,'ENV_LABEL')}")
        log_info(f"Duration: {elapsed}s")
        log_info(f"Lambda ARN: {fn_arn}")
        log_warn(f"Remember: add Alexa Smart Home trigger + register ARN in Developer Console")
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
