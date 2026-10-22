#!/usr/bin/env python3
"""
deploy_preflight.py — Digilux / Honeywell Deployment Preflight Discovery

Interactive, READ-ONLY script that connects to the target AWS account,
discovers all existing resources, reads secret values, and produces
env.honeywell.qa.discovered.json ready for use with deploy_google.py,
deploy_alexa.py, and deploy_alexa_ireland.py.

Usage:
    python3 deploy/deploy_preflight.py
    python3 deploy/deploy_preflight.py --profile honeywell-qa
    python3 deploy/deploy_preflight.py --profile honeywell-qa --region eu-west-1
    python3 deploy/deploy_preflight.py --profile honeywell-qa --output deploy/my_env.json
"""

import argparse
import json
import sys
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3")
    sys.exit(1)

# ─── Logging ──────────────────────────────────────────────────────────────────

LEVEL_WIDTH = 8
PHASE_TAG = ""


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(level: str, msg: str):
    tag = f"[{PHASE_TAG}]" if PHASE_TAG else ""
    print(f"[{_ts()}] [{level:<{LEVEL_WIDTH}}] {tag}  {msg}" if tag else f"[{_ts()}] [{level:<{LEVEL_WIDTH}}]  {msg}")


def log_info(msg):    _log("INFO",    msg)
def log_ok(msg):      _log("OK",      msg)
def log_found(msg):   _log("FOUND",   msg)
def log_miss(msg):    _log("MISS",    msg)
def log_derived(msg): _log("DERIVED", msg)
def log_default(msg): _log("DEFAULT", msg)
def log_manual(msg):  _log("MANUAL",  msg)
def log_warn(msg):    _log("WARN",    msg)
def log_error(msg):   _log("ERROR",   msg)


def phase_header(n: int, name: str):
    global PHASE_TAG
    PHASE_TAG = f"PHASE {n} — {name}"
    print(f"\n{'━' * 60}")
    print(f"  Phase {n} — {name}")
    print(f"{'━' * 60}\n")


# ─── AWS client helpers ────────────────────────────────────────────────────────

def make_session(profile: str, region: str) -> boto3.Session:
    kwargs = {"region_name": region}
    if profile and profile != "default":
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def client(session: boto3.Session, service: str, region: str = None):
    if region:
        return session.client(service, region_name=region)
    return session.client(service)


# ─── Input helpers ────────────────────────────────────────────────────────────

def prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {question}{suffix}:  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default


def prompt_yn(question: str, default: bool = True) -> bool:
    default_str = "Y/n" if default else "y/N"
    try:
        val = input(f"  {question} [{default_str}]:  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not val:
        return default
    return val.startswith("y")


def pick_from_list(items: list, label: str, suggestion_idx: int = None,
                   allow_skip: bool = False) -> str | None:
    """
    Display a numbered list and return the selected item string.
    items: list of strings already formatted for display.
    Returns None if user skips (only when allow_skip=True).
    """
    for i, item in enumerate(items, 1):
        print(f"    [{i}] {item}")
    skip_hint = " or 0 to skip" if allow_skip else ""
    default_hint = f" (default: {suggestion_idx})" if suggestion_idx else ""
    default_num = str(suggestion_idx) if suggestion_idx else ""
    try:
        raw = input(f"\n  Enter number{skip_hint}{default_hint}:  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not raw:
        raw = default_num
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    if n == 0 and allow_skip:
        return None
    if 1 <= n <= len(items):
        return items[n - 1]
    return None


# ─── Phase 0 — Banner + Region Setup ─────────────────────────────────────────

def phase0(args) -> tuple[str, str, str]:
    """Returns (profile, region, output_path)."""
    print("\n" + "━" * 63)
    print("  Digilux / Honeywell — Deployment Preflight Discovery")
    print("  This script is READ-ONLY. No AWS resources will be created.")
    print("━" * 63 + "\n")

    profile = args.profile if args.profile else prompt("AWS Profile", "default")
    region = args.region if args.region else prompt("Primary AWS Region", "ap-south-1")
    output = args.output if args.output else "deploy/env.honeywell.qa.discovered.json"

    print()
    log_info(f"Connecting to AWS (profile={profile}, region={region})...")

    try:
        sess = make_session(profile, region)
        sts = client(sess, "sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        log_ok(f"Connected — Account: {account_id}  Region: {region}")
        return profile, region, output, sess, account_id
    except ProfileNotFound:
        log_error(f"AWS credentials failed. Profile '{profile}' not found in ~/.aws/credentials")
        sys.exit(1)
    except NoCredentialsError:
        log_error("No AWS credentials found. Configure with 'aws configure' or set AWS_* env vars.")
        sys.exit(1)
    except ClientError as e:
        log_error(f"AWS credentials failed: {e}")
        sys.exit(1)


# ─── Phase 1 — Existing Lambda Scan ──────────────────────────────────────────

PRIMARY_LAMBDAS = [
    "google_home_start_link", "google_home_app_link_complete",
    "google_home_link_status", "google_home_app_unlink",
    "google_home_oauth_authorize", "google_home_oauth_cognito_callback",
    "google_home_oauth_token", "google_home_fulfillment",
    "google_home_proactive", "alexa_app_to_app_link",
    "alexa_complete_app_to_app", "alexa_server_callback",
    "alexa_app_to_app_unlink", "alexa_link_status",
    "alexa_skill_server_events", "alexa_state_reporter",
    "alexa_proactive_discovery",
]
IRELAND_LAMBDAS = ["alexa_fulfillment"]


def phase1(sess: boto3.Session, primary_region: str) -> dict:
    phase_header(1, "Existing Lambda Scan")
    candidates = {}
    count = 0

    def scan_region(region: str, names: list):
        nonlocal count
        tag = f"(eu-west-1)" if region == "eu-west-1" and region != primary_region else ""
        log_info(f"Scanning for existing Lambdas in {region}...")
        lam = client(sess, "lambda", region)
        for name in names:
            try:
                cfg = lam.get_function_configuration(FunctionName=name)
                env_vars = cfg.get("Environment", {}).get("Variables", {})
                label = f"{name} {tag}".strip()
                log_found(f"{label} — extracting {len(env_vars)} env var(s)...")
                for k, v in env_vars.items():
                    if k not in candidates:
                        candidates[k] = v
                count += len(env_vars)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
                    log_miss(f"{name} {tag}— not deployed yet".strip())
                else:
                    log_warn(f"Could not read {name}: {e}")

    scan_region(primary_region, PRIMARY_LAMBDAS)
    if primary_region != "eu-west-1":
        scan_region("eu-west-1", IRELAND_LAMBDAS)
    else:
        scan_region("eu-west-1", IRELAND_LAMBDAS)

    if candidates:
        log_info(f"Lambda scan complete. Pre-filled {count} value(s) from existing Lambdas.")
    else:
        log_info("No existing Lambdas found — will discover from AWS resources.")

    return candidates


# ─── Phase 2 — Cognito Discovery ─────────────────────────────────────────────

def phase2(sess: boto3.Session, region: str, prefilled: dict) -> dict:
    phase_header(2, "Cognito User Pool")
    result = {}

    idp = client(sess, "cognito-idp", region)

    # --- User pool ---
    try:
        log_info(f"Listing Cognito User Pools in {region}...")
        pools = []
        resp = idp.list_user_pools(MaxResults=60)
        pools.extend(resp.get("UserPools", []))
        while resp.get("NextToken"):
            resp = idp.list_user_pools(MaxResults=60, NextToken=resp["NextToken"])
            pools.extend(resp.get("UserPools", []))
    except ClientError as e:
        log_error(f"Failed to list Cognito pools: {e}")
        return result

    if not pools:
        log_miss("No Cognito User Pools found in this region.")
        result["COGNITO_USER_POOL_ID"] = "FILL_IN"
        result["COGNITO_REGION"] = region
        result["COGNITO_CLIENT_ID"] = "FILL_IN"
        result["COGNITO_HOSTED_UI_DOMAIN"] = "FILL_IN"
        return result

    log_found(f"{len(pools)} Cognito User Pool(s):")
    pool_lines = []
    for p in pools:
        created = p.get("CreationDate", "")
        if hasattr(created, "strftime"):
            created = created.strftime("%Y-%m-%d")
        pool_lines.append(f"{p['Id']}  —  Name: {p['Name']}  —  Created: {created}")
        print(f"  {pool_lines[-1]}")

    if len(pools) == 1:
        print()
        use_it = prompt_yn(f"Use this pool?", True)
        chosen_pool = pools[0] if use_it else None
    else:
        chosen = pick_from_list(pool_lines, "Cognito User Pool",
                                suggestion_idx=1, allow_skip=True)
        chosen_pool = None
        if chosen:
            idx = pool_lines.index(chosen)
            chosen_pool = pools[idx]

    if not chosen_pool:
        log_miss("Skipped — COGNITO_USER_POOL_ID = FILL_IN")
        result["COGNITO_USER_POOL_ID"] = "FILL_IN"
        result["COGNITO_REGION"] = region
        result["COGNITO_CLIENT_ID"] = "FILL_IN"
        result["COGNITO_HOSTED_UI_DOMAIN"] = "FILL_IN"
        return result

    pool_id = chosen_pool["Id"]
    log_ok(f"COGNITO_USER_POOL_ID = {pool_id}")
    result["COGNITO_USER_POOL_ID"] = pool_id
    result["COGNITO_REGION"] = region

    # --- App clients ---
    try:
        clients_resp = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)
        app_clients = clients_resp.get("UserPoolClients", [])
    except ClientError as e:
        log_warn(f"Could not list app clients: {e}")
        result["COGNITO_CLIENT_ID"] = "FILL_IN"
        app_clients = []

    if app_clients:
        log_found(f"{len(app_clients)} App Client(s) for {pool_id}:")
        client_lines = []
        for c in app_clients:
            # Try to get more detail
            try:
                detail = idp.describe_user_pool_client(
                    UserPoolId=pool_id, ClientId=c["ClientId"]
                )["UserPoolClient"]
                oauth = "Yes" if detail.get("AllowedOAuthFlows") else "No"
            except Exception:
                oauth = "?"
            client_lines.append(f"{c['ClientId']}  —  Name: {c['ClientName']:<30}  —  OAuth: {oauth}")
            print(f"    {client_lines[-1]}")

        print()
        print("  (Pick the client used by the mobile app — the one with OAuth enabled)")
        chosen_client = pick_from_list(client_lines, "App Client",
                                       suggestion_idx=1, allow_skip=True)
        if chosen_client:
            client_id = chosen_client.split("  —  ")[0].strip()
            log_ok(f"COGNITO_CLIENT_ID = {client_id}")
            result["COGNITO_CLIENT_ID"] = client_id
        else:
            result["COGNITO_CLIENT_ID"] = "FILL_IN"
    elif not app_clients:
        result["COGNITO_CLIENT_ID"] = "FILL_IN"

    # --- Hosted UI domain ---
    try:
        pool_detail = idp.describe_user_pool(UserPoolId=pool_id)["UserPool"]
        domain = pool_detail.get("Domain", "")
        custom_domain = pool_detail.get("CustomDomain", "")
        hosted_domain = custom_domain or (f"{domain}.auth.{region}.amazoncognito.com" if domain else "")
        if hosted_domain:
            log_ok(f"COGNITO_HOSTED_UI_DOMAIN = {hosted_domain}")
            result["COGNITO_HOSTED_UI_DOMAIN"] = hosted_domain
        else:
            log_miss("No custom domain configured — you may need to set this up in Cognito first.")
            entered = prompt("COGNITO_HOSTED_UI_DOMAIN (enter manually or press Enter to skip)", "FILL_IN")
            result["COGNITO_HOSTED_UI_DOMAIN"] = entered
    except ClientError as e:
        log_warn(f"Could not describe pool: {e}")
        result["COGNITO_HOSTED_UI_DOMAIN"] = "FILL_IN"

    return result


# ─── Phase 3 — DynamoDB Table Discovery ───────────────────────────────────────

# Required table keys → fuzzy match patterns
REQUIRED_TABLES = [
    ("ALEXA_SESSIONS_TABLE",        ["alexa_app_linking", "alexa_sessions", "alexa_linking_sessions"]),
    ("ALEXA_TOKENS_TABLE",          ["lwa_tokens", "alexa_lwa", "alexa_tokens"]),
    ("GH_SESSIONS_TABLE",           ["google_home_link_sessions", "gh_sessions", "google_home_sessions"]),
    ("GH_AUTH_CODES_TABLE",         ["google_home_auth_codes", "gh_auth_codes"]),
    ("GH_TOKENS_TABLE",             ["google_home_tokens", "gh_tokens"]),
    ("GH_OAUTH_TRANSACTIONS_TABLE", ["google_home_oauth_transactions", "gh_transactions", "gh_oauth"]),
    ("USER_DEVICE_DETAILS_TABLE",   ["user_device_details", "device_details"]),
    ("DEVICE_STATE_TABLE",          ["device_state"]),
    ("SCENE_TABLE",                 ["scene_data", "scenes"]),
    ("USER_DEVICE_MAPPING_TABLE",   ["user_device_mapping", "device_mapping"]),
]


def _fuzzy_match(table_names: list, patterns: list) -> str | None:
    for pattern in patterns:
        for t in table_names:
            if pattern.lower() in t.lower():
                return t
    return None


def phase3(sess: boto3.Session, region: str, prefilled: dict) -> dict:
    phase_header(3, "DynamoDB Tables")
    result = {}

    ddb = client(sess, "dynamodb", region)
    try:
        log_info(f"Listing DynamoDB tables in {region}...")
        table_names = []
        resp = ddb.list_tables()
        table_names.extend(resp.get("TableNames", []))
        while resp.get("LastEvaluatedTableName"):
            resp = ddb.list_tables(ExclusiveStartTableName=resp["LastEvaluatedTableName"])
            table_names.extend(resp.get("TableNames", []))
    except ClientError as e:
        log_error(f"Failed to list DynamoDB tables: {e}")
        return {k: "FILL_IN" for k, _ in REQUIRED_TABLES}

    log_found(f"{len(table_names)} DynamoDB table(s) in {region}:")
    col_w = max((len(t) for t in table_names), default=20) + 2
    cols = 3
    for i in range(0, len(table_names), cols):
        row = table_names[i:i + cols]
        print("  " + "  ".join(f"{t:<{col_w}}" for t in row))

    print("\n  Assign tables to required config keys.")
    print("  Press Enter to accept a suggestion, type exact name, or type 'skip'.\n")

    for key, patterns in REQUIRED_TABLES:
        suggestion = (
            prefilled.get(key)
            or _fuzzy_match(table_names, patterns)
        )
        if suggestion:
            print(f"  {key}")
            print(f"    Suggestion: {suggestion}  (matched by name)")
            val = prompt("", suggestion)
        else:
            print(f"  {key}")
            matching = [t for t in table_names if any(p.lower() in t.lower()
                                                       for p in key.lower().replace("_table", "").split("_"))]
            if matching:
                print(f"    No direct match — candidates: {', '.join(matching)}")
            else:
                print(f"    No suggestion — all tables listed above")
            val = prompt("Enter table name or 'skip'", "FILL_IN")

        if val.lower() == "skip":
            val = "FILL_IN"
        result[key] = val
        log_ok(f"{key} = {val}")
        print()

    # --- GSI for SCENE_TABLE ---
    scene_table = result.get("SCENE_TABLE", "FILL_IN")
    if scene_table and scene_table != "FILL_IN":
        log_info(f"Reading GSIs on {scene_table}...")
        try:
            desc = ddb.describe_table(TableName=scene_table)["Table"]
            gsis = desc.get("GlobalSecondaryIndexes", [])
            if gsis:
                gsi_names = [g["IndexName"] for g in gsis]
                log_found(f"GSIs: {', '.join(gsi_names)}")
                suggestion = prefilled.get("SCENE_GSI_NAME") or gsi_names[0]
                print(f"\n  SCENE_GSI_NAME")
                print(f"    Suggestion: {suggestion}")
                val = prompt("", suggestion)
                result["SCENE_GSI_NAME"] = val
                log_ok(f"SCENE_GSI_NAME = {val}")
            else:
                log_miss(f"No GSIs on {scene_table}")
                result["SCENE_GSI_NAME"] = "FILL_IN"
        except ClientError as e:
            log_warn(f"Could not describe table {scene_table}: {e}")
            result["SCENE_GSI_NAME"] = "FILL_IN"
    else:
        result["SCENE_GSI_NAME"] = "FILL_IN"

    return result


# ─── Phase 4 — Secrets Manager Discovery ──────────────────────────────────────

REQUIRED_SECRETS = [
    ("LWA_SECRET_ARN",          "LWA_SECRET_REGION",
     "Alexa Login-with-Amazon credentials",
     ["alexa/lwa", "alexa_lwa", "lwa_secret", "alexa/login"]),
    ("GOOGLE_SECRET_ARN",       "GOOGLE_SECRET_REGION",
     "Google OAuth client_id + client_secret",
     ["google.home.oauth", "google_home_oauth", "google-home-oauth", "google_oauth"]),
    ("ENDPOINT_KEY_SECRET_ARN", "ENDPOINT_KEY_REGION",
     "Alexa/GH endpoint encryption key",
     ["endpoint_key", "endpoint-key", "alexa/endpoint"]),
    ("GH_SA_SECRET_ARN",        "GH_SA_SECRET_REGION",
     "Google Home service account key for proactive reports",
     ["google.home.sa", "google_home_sa", "homegraph", "service_account", "sa_key", "sa-key"]),
    ("DEVICE_TABLE_SECRET_ARN", "DEVICE_TABLE_SECRET_REGION",
     "Device table encryption key",
     ["device_table_secret", "encrypt_data", "device_secret", "device-secret"]),
]


def _list_secrets_paginated(sm) -> list:
    secrets = []
    resp = sm.list_secrets(MaxResults=100)
    secrets.extend(resp.get("SecretList", []))
    while resp.get("NextToken"):
        resp = sm.list_secrets(MaxResults=100, NextToken=resp["NextToken"])
        secrets.extend(resp.get("SecretList", []))
    return secrets


def _arn_region(arn: str) -> str:
    # arn:aws:secretsmanager:REGION:account:secret:name
    parts = arn.split(":")
    return parts[3] if len(parts) >= 4 else "FILL_IN"


def phase4(sess: boto3.Session, primary_region: str, prefilled: dict) -> dict:
    phase_header(4, "Secrets Manager")
    result = {}
    all_secrets = []   # list of (region, secret_dict)

    for reg in sorted(set([primary_region, "eu-west-1"])):
        try:
            log_info(f"Scanning secrets in {reg}...")
            sm = client(sess, "secretsmanager", reg)
            secrets = _list_secrets_paginated(sm)
            log_info(f"  → found {len(secrets)} secret(s)")
            for s in secrets:
                all_secrets.append((reg, s))
        except ClientError as e:
            log_warn(f"Could not list secrets in {reg}: {e}")

    if not all_secrets:
        log_miss("No secrets found in either region.")
        for key, region_key, _, _ in REQUIRED_SECRETS:
            result[key] = "FILL_IN"
            result[region_key] = "FILL_IN"
        return result

    print(f"\n  All secrets found ({len(all_secrets)} total):\n")
    secret_lines = []
    prev_reg = None
    for reg, s in all_secrets:
        if reg != prev_reg:
            print(f"  [{reg}]")
            prev_reg = reg
        arn = s.get("ARN", "")
        name = s.get("Name", "")
        line = f"[{reg}]  {name:<45}  {arn}"
        secret_lines.append((reg, s, line))
        print(f"    [{len(secret_lines)}]  {name:<45}  {arn}")

    print("\n  Assign secrets to required config keys.\n")

    for key, region_key, description, patterns in REQUIRED_SECRETS:
        print(f"  {key}  ({description})")

        # Suggest from prefilled first
        prefilled_arn = prefilled.get(key)
        suggestion_idx = None
        if prefilled_arn:
            for i, (reg, s, line) in enumerate(secret_lines, 1):
                if s.get("ARN") == prefilled_arn or s.get("Name") in prefilled_arn:
                    suggestion_idx = i
                    break

        # Fuzzy match if no prefilled
        if not suggestion_idx:
            for i, (reg, s, line) in enumerate(secret_lines, 1):
                name = s.get("Name", "").lower()
                if any(p.lower() in name for p in patterns):
                    suggestion_idx = i
                    break

        if suggestion_idx:
            _, s_matched, line_matched = secret_lines[suggestion_idx - 1]
            print(f"    Suggestion: [{suggestion_idx}] {s_matched.get('Name')}  ({secret_lines[suggestion_idx - 1][0]})")
        else:
            print(f"    No suggestion matched")

        default_num = str(suggestion_idx) if suggestion_idx else ""
        try:
            raw = input(f"    Enter number (or 0 to skip){f', default={suggestion_idx}' if suggestion_idx else ''}:  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if not raw:
            raw = default_num

        if not raw or raw == "0":
            result[key] = "FILL_IN"
            result[region_key] = "FILL_IN"
            log_miss(f"Skipped — {key} = FILL_IN")
        else:
            try:
                n = int(raw)
                if 1 <= n <= len(secret_lines):
                    reg, s_chosen, _ = secret_lines[n - 1]
                    arn = s_chosen.get("ARN", "FILL_IN")
                    parsed_region = _arn_region(arn)
                    result[key] = arn
                    result[region_key] = parsed_region
                    log_ok(f"{key}    = {arn}")
                    log_ok(f"{region_key} = {parsed_region}  (parsed from ARN)")
                else:
                    result[key] = "FILL_IN"
                    result[region_key] = "FILL_IN"
            except ValueError:
                result[key] = "FILL_IN"
                result[region_key] = "FILL_IN"
        print()

    return result


# ─── Phase 5 — Read Secret Values ─────────────────────────────────────────────

def phase5(sess: boto3.Session, secrets_result: dict) -> dict:
    phase_header(5, "Read Secret Values")
    result = {}

    log_warn("This phase reads the actual secret values.")
    log_warn("They will appear in your terminal and in the output JSON.")
    log_warn("Make sure your terminal session is private.\n")

    do_read = prompt_yn("Read secret values to extract config?", True)
    if not do_read:
        log_info("Skipped secret value reading.")
        return result

    # Google OAuth secret → extract GOOGLE_CLIENT_ID
    google_arn = secrets_result.get("GOOGLE_SECRET_ARN", "FILL_IN")
    google_region = secrets_result.get("GOOGLE_SECRET_REGION", "FILL_IN")
    if google_arn and google_arn != "FILL_IN":
        try:
            log_info(f"Reading: GOOGLE_SECRET_ARN ({google_arn.split(':')[-1].split('-')[0]}, {google_region})...")
            sm = client(sess, "secretsmanager", google_region)
            sv = sm.get_secret_value(SecretId=google_arn)
            secret_str = sv.get("SecretString", "") or ""
            try:
                secret_json = json.loads(secret_str)
                found_keys = list(secret_json.keys())
                log_info(f"  Secret is JSON. Found keys: {', '.join(found_keys)}")
                if "client_id" in secret_json:
                    result["GOOGLE_CLIENT_ID"] = secret_json["client_id"]
                    log_ok(f"  GOOGLE_CLIENT_ID = {secret_json['client_id']}  (from secret key 'client_id')")
                if "client_secret" in secret_json:
                    log_info("  [NOTE] client_secret is present — NOT written to JSON output")
                    log_info("         (it lives in Secrets Manager, not in env vars)")
            except json.JSONDecodeError:
                log_info("  Secret is not JSON — no env vars extracted.")
        except ClientError as e:
            log_warn(f"  Could not read GOOGLE_SECRET_ARN: {e}")

    # LWA secret — just confirm accessibility
    lwa_arn = secrets_result.get("LWA_SECRET_ARN", "FILL_IN")
    lwa_region = secrets_result.get("LWA_SECRET_REGION", "FILL_IN")
    if lwa_arn and lwa_arn != "FILL_IN":
        try:
            log_info(f"Reading: LWA_SECRET_ARN (eu-west-1)...")
            sm = client(sess, "secretsmanager", lwa_region)
            sv = sm.get_secret_value(SecretId=lwa_arn)
            secret_str = sv.get("SecretString", "") or ""
            try:
                secret_json = json.loads(secret_str)
                found_keys = list(secret_json.keys())
                log_info(f"  Secret is JSON. Found keys: {', '.join(found_keys)}")
            except json.JSONDecodeError:
                pass
            log_ok("  LWA credentials accessible — no env var extracted (used at runtime by Lambda)")
        except ClientError as e:
            log_warn(f"  Could not read LWA_SECRET_ARN: {e}")

    # Endpoint key — just confirm accessibility
    ep_arn = secrets_result.get("ENDPOINT_KEY_SECRET_ARN", "FILL_IN")
    ep_region = secrets_result.get("ENDPOINT_KEY_REGION", "FILL_IN")
    if ep_arn and ep_arn != "FILL_IN":
        try:
            log_info(f"Reading: ENDPOINT_KEY_SECRET_ARN...")
            sm = client(sess, "secretsmanager", ep_region)
            sm.get_secret_value(SecretId=ep_arn)
            log_ok("  Endpoint key secret accessible — no env var extracted (binary key, used at runtime)")
        except ClientError as e:
            log_warn(f"  Could not read ENDPOINT_KEY_SECRET_ARN: {e}")

    return result


# ─── Phase 6 — KMS Key Discovery ──────────────────────────────────────────────

def phase6(sess: boto3.Session, region: str, prefilled: dict) -> dict:
    phase_header(6, "KMS Key")
    result = {}
    default_alias = "alias/honeywell-alexa-tokens"

    try:
        log_info(f"Scanning KMS aliases in {region}...")
        kms = client(sess, "kms", region)
        aliases = []
        resp = kms.list_aliases()
        aliases.extend(resp.get("Aliases", []))
        while resp.get("NextMarker"):
            resp = kms.list_aliases(Marker=resp["NextMarker"])
            aliases.extend(resp.get("Aliases", []))
    except ClientError as e:
        log_warn(f"Could not list KMS aliases: {e}")
        val = prompt(f"KMS_KEY_ALIAS (alias to encrypt/decrypt LWA tokens)", default_alias)
        result["KMS_KEY_ALIAS"] = val
        return result

    relevant = [a for a in aliases
                if any(k in a.get("AliasName", "").lower()
                       for k in ["alexa", "honeywell", "digilux", "token", "lwa"])]

    if relevant:
        log_found(f"Aliases with 'alexa'/'honeywell'/'token' in name:")
        alias_lines = [f"{a['AliasName']}    →  KeyId: {a.get('TargetKeyId', 'N/A')}" for a in relevant]
        for line in alias_lines:
            print(f"    {line}")

        suggestion_idx = None
        prefilled_alias = prefilled.get("KMS_KEY_ALIAS")
        for i, a in enumerate(relevant, 1):
            if prefilled_alias and prefilled_alias == a["AliasName"]:
                suggestion_idx = i
                break
        if not suggestion_idx:
            suggestion_idx = 1

        print(f"\n  KMS_KEY_ALIAS  (used to encrypt/decrypt LWA tokens)")
        print(f"    Suggestion: [{suggestion_idx}] {relevant[suggestion_idx - 1]['AliasName']}")
        chosen = pick_from_list(alias_lines, "KMS alias", suggestion_idx=suggestion_idx, allow_skip=True)
        if chosen:
            val = chosen.split("    →  ")[0].strip()
        else:
            val = prompt(f"Enter KMS alias name", default_alias)
    else:
        log_miss("No relevant KMS aliases found.")
        log_info("This alias will be created by deploy_alexa.py.")
        val = prompt(f"Enter desired alias name", default_alias)

    log_ok(f"KMS_KEY_ALIAS = {val}")
    result["KMS_KEY_ALIAS"] = val
    return result


# ─── Phase 7 — S3 Bucket Discovery ────────────────────────────────────────────

def phase7(sess: boto3.Session, primary_region: str, prefilled: dict) -> dict:
    phase_header(7, "S3 Metadata Bucket")
    result = {}

    try:
        log_info("Listing S3 buckets (global)...")
        s3 = client(sess, "s3")
        resp = s3.list_buckets()
        all_buckets = resp.get("Buckets", [])
    except ClientError as e:
        log_warn(f"Could not list S3 buckets: {e}")
        val = prompt("METADATA_BUCKET", "FILL_IN")
        result["METADATA_BUCKET"] = val
        return result

    # Get bucket locations
    buckets_with_region = []
    for b in all_buckets:
        try:
            loc = s3.get_bucket_location(Bucket=b["Name"])
            reg = loc.get("LocationConstraint") or "us-east-1"
        except Exception:
            reg = "unknown"
        buckets_with_region.append((b["Name"], reg))

    keywords = ["metadata", "honeywell", "digilux", "device", "config", "asset"]
    relevant = [(name, reg) for name, reg in buckets_with_region
                if any(kw in name.lower() for kw in keywords)]

    if relevant:
        log_found(f"Buckets containing relevant keywords:")
        bucket_lines = [f"{name:<50} ({reg})" for name, reg in relevant]
        for line in bucket_lines:
            print(f"    {line}")

        prefilled_bucket = prefilled.get("METADATA_BUCKET")
        suggestion_idx = None
        if prefilled_bucket:
            for i, (name, _) in enumerate(relevant, 1):
                if name == prefilled_bucket:
                    suggestion_idx = i
                    break
        if not suggestion_idx:
            # Prefer bucket with 'metadata' in name
            for i, (name, _) in enumerate(relevant, 1):
                if "metadata" in name.lower():
                    suggestion_idx = i
                    break
        if not suggestion_idx:
            suggestion_idx = 1

        print(f"\n  METADATA_BUCKET  (device metadata JSON files for SYNC/QUERY responses)")
        print(f"    Suggestion: [{suggestion_idx}] {relevant[suggestion_idx - 1][0]}")
        chosen = pick_from_list(bucket_lines, "S3 bucket", suggestion_idx=suggestion_idx, allow_skip=True)
        if chosen:
            val = chosen.split("(")[0].strip()
        else:
            val = prompt("Enter bucket name", "FILL_IN")
    else:
        log_miss("No relevant S3 buckets found.")
        val = prompt("METADATA_BUCKET", "FILL_IN")

    log_ok(f"METADATA_BUCKET = {val}")
    result["METADATA_BUCKET"] = val
    return result


# ─── Phase 8 — IoT Endpoint Discovery ─────────────────────────────────────────

def phase8(sess: boto3.Session, region: str) -> dict:
    phase_header(8, "IoT Data Endpoint")
    result = {}

    try:
        log_info(f"Fetching IoT ATS endpoint for {region}...")
        iot = client(sess, "iot", region)
        resp = iot.describe_endpoint(endpointType="iot:Data-ATS")
        endpoint = resp.get("endpointAddress", "")
        if endpoint:
            # Endpoint doesn't include scheme
            if not endpoint.startswith("https://"):
                endpoint = f"https://{endpoint}"
            log_ok(f"IOT_DATA_ENDPOINT = {endpoint}")
            result["IOT_DATA_ENDPOINT"] = endpoint
        else:
            log_miss("IoT endpoint returned empty.")
            val = prompt("IOT_DATA_ENDPOINT", "FILL_IN")
            result["IOT_DATA_ENDPOINT"] = val
    except ClientError as e:
        log_warn(f"Could not fetch IoT endpoint: {e}")
        val = prompt("IOT_DATA_ENDPOINT", "FILL_IN")
        result["IOT_DATA_ENDPOINT"] = val

    return result


# ─── Phase 9 — API Gateway Discovery ──────────────────────────────────────────

def phase9(sess: boto3.Session, region: str, prefilled: dict) -> dict:
    phase_header(9, "API Gateway")
    result = {}

    try:
        log_info(f"Listing REST APIs in {region}...")
        apigw = client(sess, "apigateway", region)
        resp = apigw.get_rest_apis(limit=500)
        apis = resp.get("items", [])
    except ClientError as e:
        log_warn(f"Could not list REST APIs: {e}")
        result["API_GATEWAY_ID"] = "FILL_IN"
        result["API_BASE_URL"] = "FILL_IN"
        result["API_STAGE"] = "smarthome"
        return result

    if not apis:
        log_miss("No REST APIs found.")
        result["API_GATEWAY_ID"] = prompt("API_GATEWAY_ID", "FILL_IN")
        result["API_BASE_URL"] = prompt("API_BASE_URL", "FILL_IN")
        result["API_STAGE"] = prompt("API_STAGE", "smarthome")
        return result

    log_found(f"{len(apis)} REST API(s) in {region}:")
    api_lines = []
    for a in apis:
        created = a.get("createdDate", "")
        if hasattr(created, "strftime"):
            created = created.strftime("%Y-%m-%d")
        api_lines.append(f"{a['id']}  —  Name: {a['name']:<40}  —  Created: {created}")
        print(f"    {api_lines[-1]}")

    prefilled_gw = prefilled.get("API_GATEWAY_ID")
    suggestion_idx = None
    if prefilled_gw:
        for i, a in enumerate(apis, 1):
            if a["id"] == prefilled_gw:
                suggestion_idx = i
                break
    if not suggestion_idx:
        suggestion_idx = 1

    print(f"\n  API_GATEWAY_ID:")
    print(f"    Suggestion: [{suggestion_idx}] {apis[suggestion_idx - 1]['id']}  ({apis[suggestion_idx - 1]['name']})")
    chosen = pick_from_list(api_lines, "REST API", suggestion_idx=suggestion_idx, allow_skip=True)
    if not chosen:
        result["API_GATEWAY_ID"] = prompt("API_GATEWAY_ID", "FILL_IN")
        result["API_BASE_URL"] = prompt("API_BASE_URL", "FILL_IN")
        result["API_STAGE"] = prompt("API_STAGE", "smarthome")
        return result

    api_id = chosen.split("  —  ")[0].strip()
    log_ok(f"API_GATEWAY_ID = {api_id}")
    result["API_GATEWAY_ID"] = api_id

    # Custom domain mapping
    log_info(f"Checking custom domain mappings for API {api_id}...")
    api_base_url = None
    api_stage = "smarthome"
    try:
        domains = apigw.get_domain_names(limit=100).get("items", [])
        for domain in domains:
            domain_name = domain["domainName"]
            try:
                mappings = apigw.get_base_path_mappings(domainName=domain_name, limit=100).get("items", [])
                for m in mappings:
                    if m.get("restApiId") == api_id:
                        stage = m.get("stage", "smarthome")
                        base_path = m.get("basePath", "")
                        if base_path and base_path != "(none)":
                            url = f"https://{domain_name}/{base_path}"
                        else:
                            url = f"https://{domain_name}/{stage}"
                        log_found(f"Custom domain: {domain_name}  →  stage: {stage}")
                        api_base_url = url
                        api_stage = stage
                        break
            except ClientError:
                pass
            if api_base_url:
                break
    except ClientError as e:
        log_warn(f"Could not check domain mappings: {e}")

    if api_base_url:
        log_ok(f"API_BASE_URL = {api_base_url}")
        log_ok(f"API_STAGE    = {api_stage}")
        confirmed = prompt(f"Confirm API_BASE_URL", api_base_url)
        result["API_BASE_URL"] = confirmed
        result["API_STAGE"] = api_stage
    else:
        constructed = f"https://{api_id}.execute-api.{region}.amazonaws.com/{api_stage}"
        log_miss(f"No custom domain mapping found.")
        log_info(f"Constructed URL: {constructed}")
        val = prompt("Enter API_BASE_URL", constructed)
        result["API_BASE_URL"] = val
        stage_val = prompt("API_STAGE", "smarthome")
        result["API_STAGE"] = stage_val

    return result


# ─── Phase 10 — Lambda CREATE_UPDATE_FN Discovery ─────────────────────────────

def phase10(sess: boto3.Session, region: str, prefilled: dict) -> dict:
    phase_header(10, "Lambda Functions")
    result = {}

    try:
        log_info(f"Scanning Lambda functions in {region} for CREATE_UPDATE_FN...")
        lam = client(sess, "lambda", region)
        functions = []
        resp = lam.list_functions(MaxItems=500)
        functions.extend(resp.get("Functions", []))
        while resp.get("NextMarker"):
            resp = lam.list_functions(MaxItems=500, Marker=resp["NextMarker"])
            functions.extend(resp.get("Functions", []))
    except ClientError as e:
        log_warn(f"Could not list Lambda functions: {e}")
        val = prompt("CREATE_UPDATE_FN", prefilled.get("CREATE_UPDATE_FN", "FILL_IN"))
        result["CREATE_UPDATE_FN"] = val
        return result

    keywords = ["create", "update", "data", "sync", "device"]
    candidates = [f for f in functions
                  if any(kw in f["FunctionName"].lower() for kw in keywords)
                  and f["FunctionName"] not in PRIMARY_LAMBDAS + IRELAND_LAMBDAS]

    if candidates:
        log_found(f"Candidate Lambda(s) for CREATE_UPDATE_FN:")
        fn_lines = [f["FunctionName"] for f in candidates]
        for fn in fn_lines:
            print(f"    {fn}")

        prefilled_fn = prefilled.get("CREATE_UPDATE_FN")
        suggestion_idx = None
        if prefilled_fn:
            for i, fn in enumerate(fn_lines, 1):
                if fn == prefilled_fn:
                    suggestion_idx = i
                    break
        if not suggestion_idx:
            suggestion_idx = 1

        print(f"\n  CREATE_UPDATE_FN  (Lambda that syncs device data after Alexa linking):")
        print(f"    Suggestion: [{suggestion_idx}] {fn_lines[suggestion_idx - 1]}")
        chosen = pick_from_list(fn_lines, "Lambda function", suggestion_idx=suggestion_idx, allow_skip=True)
        if chosen:
            val = chosen
        else:
            val = prompt("CREATE_UPDATE_FN", prefilled_fn or "FILL_IN")
    else:
        log_miss("No matching Lambda functions found.")
        val = prompt("CREATE_UPDATE_FN  (Lambda that syncs device data after Alexa linking)",
                     prefilled.get("CREATE_UPDATE_FN", "FILL_IN"))

    log_ok(f"CREATE_UPDATE_FN = {val}")
    result["CREATE_UPDATE_FN"] = val
    return result


# ─── Phase 11 — eu-west-1 Alexa Fulfillment Region ───────────────────────────

def phase11(sess: boto3.Session, primary_region: str, prefilled: dict) -> dict:
    phase_header(11, "Alexa Fulfillment Region")
    result = {}

    log_info("Checking for alexa_fulfillment Lambda in eu-west-1...")
    try:
        lam = client(sess, "lambda", "eu-west-1")
        lam.get_function_configuration(FunctionName="alexa_fulfillment")
        log_found("alexa_fulfillment exists in eu-west-1")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "404"):
            log_miss("alexa_fulfillment not found in eu-west-1.")
            log_info("For Indian customers, Alexa routes to eu-west-1 (Ireland).")
        else:
            log_warn(f"Could not check alexa_fulfillment: {e}")

    val = prompt("ALEXA_FULFILLMENT_REGION", "eu-west-1")
    log_ok(f"ALEXA_FULFILLMENT_REGION = {val}")
    result["ALEXA_FULFILLMENT_REGION"] = val
    return result


# ─── Phase 12 — Derived Values ────────────────────────────────────────────────

def phase12(collected: dict) -> dict:
    phase_header(12, "Derived Values")
    result = {}

    region = collected.get("AWS_REGION", "FILL_IN")
    result["COGNITO_REGION"] = region
    log_derived(f"COGNITO_REGION = {region}  (same as AWS_REGION)")

    api_base = collected.get("API_BASE_URL", "FILL_IN")
    if api_base and api_base != "FILL_IN":
        alexa_redirect = f"{api_base.rstrip('/')}/alexa/callback"
        result["ALEXA_REDIRECT_URI"] = alexa_redirect
        log_derived(f"ALEXA_REDIRECT_URI = {alexa_redirect}  (derived from API_BASE_URL)")

        parsed = urlparse(alexa_redirect)
        alexa_redirect_hosts = parsed.hostname or "FILL_IN"
        result["ALEXA_REDIRECT_HOSTS"] = alexa_redirect_hosts
        log_derived(f"ALEXA_REDIRECT_HOSTS = {alexa_redirect_hosts}  (hostname from ALEXA_REDIRECT_URI)")
    else:
        result["ALEXA_REDIRECT_URI"] = "FILL_IN"
        result["ALEXA_REDIRECT_HOSTS"] = "FILL_IN"
        log_miss("API_BASE_URL not set — ALEXA_REDIRECT_URI and ALEXA_REDIRECT_HOSTS = FILL_IN")

    log_info("GOOGLE_REDIRECT_URI = https://oauth-redirect.googleusercontent.com/r/{GOOGLE_AGENT_ID}")
    log_info("  (will be completed after GOOGLE_AGENT_ID is entered in Phase 13)")

    return result


# ─── Phase 13 — Manual Values ─────────────────────────────────────────────────

MANUAL_FIELDS = [
    ("ALEXA_SKILL_ID",
     "from Alexa Developer Console → Skills → Your Skill → Skill ID",
     "FILL_IN"),
    ("ALEXA_SKILL_STAGE",
     "development / live",
     "development"),
    ("GOOGLE_AGENT_ID",
     "from Google Home Developer Console — your project/agent ID",
     "FILL_IN"),
    ("APP_DEEP_LINK_SCHEME",
     "your Android app's URI scheme, e.g. 'digilux', 'honeywell'",
     "FILL_IN"),
    ("APP_NAME",
     "display name shown in consent page, e.g. 'Honeywell Smart Home'",
     "FILL_IN"),
    ("DEVICE_MANUFACTURER",
     "shown in Google Home device list",
     "FILL_IN"),
    ("DEVICE_MODEL",
     "shown in Google Home device list",
     "FILL_IN"),
    ("BRAND_TITLE",
     "consent page header — company name",
     "FILL_IN"),
    ("BRAND_SUBTITLE",
     "consent page sub-header",
     "Identity Server"),
    ("TERMS_URL",
     "link shown on consent page",
     "FILL_IN"),
    ("PRIVACY_POLICY_URL",
     "link shown on consent page",
     "FILL_IN"),
]

CONSENT_FIELDS = [
    ("CONSENT_PAGE_HEADING",  "Authorization"),
    ("CONSENT_NOTICE_TEXT",   "By clicking Allow, you authorize Google to access the following information."),
    ("CONSENT_PERM_LABEL",    "Granting permission to:"),
    ("CONSENT_PERMISSIONS",   "discoverdevices,controldevices"),
    ("CONSENT_TRUST_TEXT",    "Make sure you trust Google"),
    ("CONSENT_DENY_LABEL",    "DENY"),
    ("CONSENT_ALLOW_LABEL",   "ALLOW"),
]


def phase13(prefilled: dict) -> dict:
    phase_header(13, "Manual Values")
    result = {}

    print("  The following values cannot be discovered from AWS.")
    print("  Enter each value, or press Enter to leave as FILL_IN.\n")

    for key, description, default in MANUAL_FIELDS:
        # Use prefilled if available
        effective_default = prefilled.get(key) or default
        print(f"  {key}  ({description})")
        val = prompt("", effective_default)
        result[key] = val
        log_manual(f"{key} = {val}")

        # Derive GOOGLE_REDIRECT_URI as soon as GOOGLE_AGENT_ID is entered
        if key == "GOOGLE_AGENT_ID" and val and val != "FILL_IN":
            redirect_uri = f"https://oauth-redirect.googleusercontent.com/r/{val}"
            result["GOOGLE_REDIRECT_URI"] = redirect_uri
            log_derived(f"GOOGLE_REDIRECT_URI = {redirect_uri}  (computed automatically)")
        print()

    if "GOOGLE_REDIRECT_URI" not in result:
        result["GOOGLE_REDIRECT_URI"] = "FILL_IN"

    print("\n  Consent page text — press Enter to keep defaults.\n")
    for key, default in CONSENT_FIELDS:
        effective_default = prefilled.get(key) or default
        val = prompt(f"{key:<30}  [{effective_default[:40]}]", effective_default)
        result[key] = val
        log_default(f"{key} = {val}")
        print()

    return result


# ─── Phase 14 — Fixed / Well-Known Values ─────────────────────────────────────

FIXED_VALUES = {
    "LWA_TOKEN_URL":             "https://api.amazon.com/auth/o2/token",
    "LWA_REVOKE_URL":            "https://api.amazon.com/auth/o2/revoke",
    "SKILL_ENABLEMENT_URL":      "https://api.amazonalexa.com/v1/users/~current/skills",
    "ALEXA_GATEWAY_URL":         "https://api.eu.amazonalexa.com/v3/events",
    "GH_DEEP_LINK_BASE_URL":     "https://home.google.com/home-app/",
    "GH_DEEP_LINK_PATH":         "setup/ha_linking",
    "LOG_LEVEL":                 "INFO",
    "SESSION_TTL_SECONDS":       "1800",
    "AUTH_CODE_TTL_SECONDS":     "900",
    "ACCESS_TOKEN_TTL_SECONDS":  "3600",
    "REFRESH_TOKEN_TTL_SECONDS": "15552000",
    "TRANSACTION_TTL_SECONDS":   "600",
    "ALEXA_SESSION_TTL_SECONDS": "600",
    "GH_TOKENS_ACCESS_TOKEN_INDEX": "accessToken-index",
    "DEPLOY_TARGET":             "all",
}

DEFAULT_TABLE_NAMES = {
    "GH_SESSIONS_TABLE":           "google_home_link_sessions",
    "GH_AUTH_CODES_TABLE":         "google_home_auth_codes",
    "GH_TOKENS_TABLE":             "google_home_tokens",
    "GH_OAUTH_TRANSACTIONS_TABLE": "google_home_oauth_transactions",
    "ALEXA_SESSIONS_TABLE":        "alexa_app_linking_sessions",
}


def phase14(collected: dict) -> dict:
    phase_header(14, "Fixed / Well-Known Values")
    result = {}

    for key, val in FIXED_VALUES.items():
        result[key] = val
        log_default(f"{key} = {val}")

    # Apply default table names only if not already set
    for key, default in DEFAULT_TABLE_NAMES.items():
        if not collected.get(key) or collected[key] == "FILL_IN":
            result[key] = default
            log_default(f"{key} = {default}  (well-known default)")

    print()
    role_prefix = prompt("ROLE_PREFIX", collected.get("ROLE_PREFIX", "honeywell"))
    env_label = prompt("ENV_LABEL", collected.get("ENV_LABEL", "honeywell-qa"))
    result["ROLE_PREFIX"] = role_prefix
    result["ENV_LABEL"] = env_label
    log_ok(f"ROLE_PREFIX = {role_prefix}")
    log_ok(f"ENV_LABEL   = {env_label}")

    return result


# ─── Phase 15 — Generate Output ───────────────────────────────────────────────

# Keys in canonical output order (matches env.honeywell.prod.json)
OUTPUT_KEY_ORDER = [
    "_description", "_deploy_target",
    "DEPLOY_TARGET", "ROLE_PREFIX", "ENV_LABEL",
    "AWS_REGION", "ALEXA_FULFILLMENT_REGION",
    "API_GATEWAY_ID", "API_STAGE", "API_BASE_URL",
    "COGNITO_USER_POOL_ID", "COGNITO_REGION", "COGNITO_CLIENT_ID", "COGNITO_HOSTED_UI_DOMAIN",
    "ALEXA_SESSIONS_TABLE", "ALEXA_TOKENS_TABLE",
    "GH_SESSIONS_TABLE", "GH_AUTH_CODES_TABLE", "GH_TOKENS_TABLE",
    "GH_OAUTH_TRANSACTIONS_TABLE",
    "LWA_SECRET_ARN", "LWA_SECRET_REGION",
    "KMS_KEY_ALIAS",
    "ALEXA_SKILL_ID", "ALEXA_SKILL_STAGE",
    "ALEXA_REDIRECT_URI", "ALEXA_REDIRECT_HOSTS",
    "GOOGLE_SECRET_ARN", "GOOGLE_SECRET_REGION",
    "GOOGLE_AGENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_REDIRECT_URI",
    "APP_DEEP_LINK_SCHEME", "APP_NAME", "DEVICE_MANUFACTURER",
    "LOG_LEVEL",
    "SESSION_TTL_SECONDS", "AUTH_CODE_TTL_SECONDS",
    "ACCESS_TOKEN_TTL_SECONDS", "REFRESH_TOKEN_TTL_SECONDS",
    "GH_TOKENS_ACCESS_TOKEN_INDEX",
    "GH_DEEP_LINK_BASE_URL", "GH_DEEP_LINK_PATH",
    "GOOGLE_SA_KEY_SECRET_ARN",
    "USER_DEVICE_DETAILS_TABLE", "DEVICE_STATE_TABLE",
    "SCENE_TABLE", "SCENE_GSI_NAME",
    "METADATA_BUCKET",
    "ENDPOINT_KEY_SECRET_ARN", "ENDPOINT_KEY_REGION",
    "DEVICE_MODEL",
    "GH_SA_SECRET_ARN", "GH_SA_SECRET_REGION",
    "USER_DEVICE_MAPPING_TABLE",
    "ALEXA_SESSION_TTL_SECONDS",
    "CREATE_UPDATE_FN",
    "LWA_TOKEN_URL", "LWA_REVOKE_URL",
    "SKILL_ENABLEMENT_URL", "ALEXA_GATEWAY_URL",
    "IOT_DATA_ENDPOINT",
    "DEVICE_TABLE_SECRET_ARN", "DEVICE_TABLE_SECRET_REGION",
    "TRANSACTION_TTL_SECONDS",
    "BRAND_TITLE", "BRAND_SUBTITLE",
    "TERMS_URL", "PRIVACY_POLICY_URL",
    "CONSENT_PAGE_HEADING", "CONSENT_NOTICE_TEXT", "CONSENT_PERM_LABEL",
    "CONSENT_PERMISSIONS", "CONSENT_TRUST_TEXT",
    "CONSENT_DENY_LABEL", "CONSENT_ALLOW_LABEL",
]

# Source classification for summary table
def _classify_source(key: str, collected: dict, prefilled_lambda: dict,
                     manual_keys: set, derived_keys: set, default_keys: set) -> tuple[str, str]:
    val = collected.get(key, "FILL_IN")
    if val == "FILL_IN" or val is None:
        return "MISSING", "—"
    if key in derived_keys:
        return "DERIVED", "computed"
    if key in manual_keys:
        return "MANUAL", "user input"
    if key in default_keys:
        return "DEFAULT", "standard constant"
    if key in prefilled_lambda:
        return "DISCOVERED", "lambda:GetFunctionConfiguration"
    return "DISCOVERED", "aws api"


def phase15(collected: dict, output_path: str, account_id: str,
            prefilled_lambda: dict, manual_keys: set, derived_keys: set, default_keys: set):
    phase_header(15, "Output")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_obj = {
        "_description": f"Honeywell QA — generated by deploy_preflight.py on {today}",
        "_deploy_target": "all",
    }

    for key in OUTPUT_KEY_ORDER:
        if key in ("_description", "_deploy_target"):
            continue
        val = collected.get(key)
        if val is None:
            if key in ("GOOGLE_CLIENT_ID", "GOOGLE_SA_KEY_SECRET_ARN"):
                output_obj[key] = collected.get(key, "")
            else:
                output_obj[key] = "FILL_IN"
        else:
            output_obj[key] = val

    # Add any extra keys not in the canonical order
    for key, val in collected.items():
        if key not in output_obj and not key.startswith("_"):
            output_obj[key] = val

    log_info(f"Writing: {output_path}")
    try:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output_obj, f, indent=2, default=str)
            f.write("\n")
        log_ok(f"File written successfully")
    except Exception as e:
        log_error(f"Failed to write output: {e}")
        sys.exit(1)

    # Summary table
    print(f"\n{'━' * 75}")
    print(f"  DISCOVERY SUMMARY")
    print(f"{'━' * 75}\n")
    print(f"  {'KEY':<42} {'STATUS':<12} {'SOURCE'}")
    print(f"  {'─' * 42} {'─' * 12} {'─' * 18}")

    counts = {"DISCOVERED": 0, "DERIVED": 0, "MANUAL": 0, "DEFAULT": 0, "MISSING": 0}
    for key in OUTPUT_KEY_ORDER:
        if key.startswith("_"):
            continue
        val = collected.get(key)
        status, source = _classify_source(key, collected, prefilled_lambda,
                                          manual_keys, derived_keys, default_keys)
        if val and val != "FILL_IN":
            counts[status] = counts.get(status, 0) + 1
        else:
            counts["MISSING"] = counts.get("MISSING", 0) + 1
            status = "MISSING"
            source = "—"
        print(f"  {key:<42} {status:<12} {source}")

    print(f"\n  {'─' * 75}")
    summary_parts = []
    for label in ["DISCOVERED", "DERIVED", "MANUAL", "DEFAULT", "MISSING"]:
        summary_parts.append(f"{label}: {counts.get(label, 0)}")
    print(f"  {' | '.join(summary_parts)}\n")

    if counts.get("MISSING", 0) > 0:
        print(f"  MISSING values (still FILL_IN in output):")
        for key in OUTPUT_KEY_ORDER:
            if key.startswith("_"):
                continue
            if not collected.get(key) or collected.get(key) == "FILL_IN":
                hint = ""
                if "SKILL_ID" in key:
                    hint = " — get from Alexa Developer Console"
                elif "AGENT_ID" in key:
                    hint = " — get from Google Home Developer Console"
                print(f"    {key}{hint}")
        print()
        print("  These will appear as FILL_IN in the output JSON.")
        print("  The deploy scripts will refuse to run until they are filled in.\n")
    else:
        print("  All values resolved. Output is ready.")
        print(f"  Share {output_path} with your developer.\n")

    print(f"  Output file: {output_path}")
    print(f"{'━' * 75}\n")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Digilux/Honeywell Deployment Preflight — READ-ONLY AWS discovery"
    )
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    parser.add_argument("--region",  default=None, help="Primary AWS region")
    parser.add_argument("--output",  default=None, help="Output JSON file path")
    args = parser.parse_args()

    # Phase 0
    profile, region, output_path, sess, account_id = phase0(args)

    collected = {"AWS_REGION": region}

    # Phase 1 — existing Lambda scan (pre-fills candidates)
    prefilled_lambda = phase1(sess, region)
    collected.update(prefilled_lambda)

    # Phase 2 — Cognito
    cognito_result = phase2(sess, region, prefilled_lambda)
    collected.update(cognito_result)

    # Phase 3 — DynamoDB
    ddb_result = phase3(sess, region, prefilled_lambda)
    collected.update(ddb_result)

    # Phase 4 — Secrets Manager
    secrets_result = phase4(sess, region, prefilled_lambda)
    collected.update(secrets_result)

    # Phase 5 — Read secret values
    secret_values = phase5(sess, secrets_result)
    collected.update(secret_values)

    # Phase 6 — KMS
    kms_result = phase6(sess, region, prefilled_lambda)
    collected.update(kms_result)

    # Phase 7 — S3
    s3_result = phase7(sess, region, prefilled_lambda)
    collected.update(s3_result)

    # Phase 8 — IoT
    iot_result = phase8(sess, region)
    collected.update(iot_result)

    # Phase 9 — API Gateway
    apigw_result = phase9(sess, region, prefilled_lambda)
    collected.update(apigw_result)

    # Phase 10 — Lambda CREATE_UPDATE_FN
    lambda_result = phase10(sess, region, prefilled_lambda)
    collected.update(lambda_result)

    # Phase 11 — Alexa fulfillment region
    ireland_result = phase11(sess, region, prefilled_lambda)
    collected.update(ireland_result)

    # Phase 12 — Derived values
    derived_result = phase12(collected)
    derived_keys = set(derived_result.keys())
    collected.update(derived_result)

    # Phase 13 — Manual values
    manual_result = phase13(prefilled_lambda)
    manual_keys = set(manual_result.keys())
    collected.update(manual_result)

    # Phase 14 — Fixed/well-known values
    fixed_result = phase14(collected)
    default_keys = set(fixed_result.keys())
    collected.update(fixed_result)

    # Phase 15 — Output
    phase15(
        collected=collected,
        output_path=output_path,
        account_id=account_id,
        prefilled_lambda=prefilled_lambda,
        manual_keys=manual_keys,
        derived_keys=derived_keys,
        default_keys=default_keys,
    )


if __name__ == "__main__":
    main()
