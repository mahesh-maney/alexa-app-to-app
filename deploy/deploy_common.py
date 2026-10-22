#!/usr/bin/env python3
"""
deploy_common.py — Shared utilities for deploy_google.py, deploy_alexa.py, deploy_alexa_ireland.py

Not intended to be run directly.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3")
    sys.exit(1)

# ─── Logging ──────────────────────────────────────────────────────────────────

_step_tag = ""


def set_step(tag: str):
    global _step_tag
    _step_tag = tag


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(level: str, msg: str):
    tag = f"[{_step_tag}]  " if _step_tag else ""
    print(f"[{_ts()}] [{level:<7}]  {tag}{msg}", flush=True)


def log_info(m):   _log("INFO",   m)
def log_ok(m):     _log("OK",     m)
def log_skip(m):   _log("SKIP",   m)
def log_create(m): _log("CREATE", m)
def log_update(m): _log("UPDATE", m)
def log_warn(m):   _log("WARN",   m)
def log_error(m):  _log("ERROR",  m)


def step_header(n: int, name: str):
    tag = f"STEP {n} — {name}"
    set_step(tag)
    print(f"\n{'━' * 68}")
    print(f"  Step {n} — {name}")
    print(f"{'━' * 68}\n", flush=True)


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        log_error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def cfg(c: dict, key: str, fallback: str = "") -> str:
    return str(c.get(key) or fallback)


def check_no_fill_in(c: dict, script_name: str):
    bad = [k for k, v in c.items() if v == "FILL_IN"]
    if bad:
        log_error(f"Config has {len(bad)} FILL_IN placeholder(s) in: {', '.join(bad)}")
        log_error(f"Fill in all FILL_IN values in the config before running {script_name}")
        sys.exit(1)
    log_ok("No FILL_IN placeholders found")


def check_required_keys(c: dict, required: list):
    missing = []
    for k in required:
        v = c.get(k, "")
        if not v:
            missing.append(k)
        else:
            log_ok(f"  {k:<46} = {str(v)[:60]}")
    if missing:
        log_error(f"Missing or empty required config keys: {', '.join(missing)}")
        sys.exit(1)


# ─── AWS Helpers ──────────────────────────────────────────────────────────────

def make_client(session, service: str, region: str = None):
    if region:
        return session.client(service, region_name=region)
    return session.client(service)


def preflight_aws(session) -> str:
    """Verify AWS credentials. Returns account ID."""
    try:
        sts = make_client(session, "sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        log_ok(f"AWS credentials valid  account={account_id}  region={session.region_name}")
        return account_id
    except (NoCredentialsError, ClientError) as e:
        log_error(f"AWS credentials check failed: {e}")
        sys.exit(1)


def verify_secret_accessible(session, secret_arn: str, secret_region: str, label: str):
    sm = make_client(session, "secretsmanager", secret_region)
    try:
        resp = sm.describe_secret(SecretId=secret_arn)
        log_ok(f"{label} accessible  name={resp.get('Name', secret_arn)}")
    except ClientError as e:
        log_error(f"{label} NOT accessible: {e}")
        sys.exit(1)


# ─── DynamoDB ─────────────────────────────────────────────────────────────────

def ensure_table(ddb, table_name: str, pk: str, ttl_attr: str = None) -> bool:
    """Create table if not exists. PAY_PER_REQUEST. Returns True if created."""
    try:
        ddb.describe_table(TableName=table_name)
        log_skip(f"Table {table_name} already exists")
        if ttl_attr:
            _ensure_ttl(ddb, table_name, ttl_attr)
        return False
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    log_create(f"Table {table_name} not found — creating  pk={pk}  billing=PAY_PER_REQUEST")
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    log_info(f"Waiting for {table_name} to become ACTIVE...")
    ddb.get_waiter("table_exists").wait(TableName=table_name,
                                        WaiterConfig={"Delay": 3, "MaxAttempts": 40})
    log_ok(f"Table {table_name} ACTIVE")
    if ttl_attr:
        _ensure_ttl(ddb, table_name, ttl_attr)
    return True


def _ensure_ttl(ddb, table_name: str, attr: str):
    try:
        ttl = ddb.describe_time_to_live(TableName=table_name)
        status = ttl.get("TimeToLiveDescription", {}).get("TimeToLiveStatus", "DISABLED")
        if status in ("ENABLED", "ENABLING"):
            log_skip(f"TTL already {status} on {table_name}")
            return
    except ClientError:
        pass
    log_info(f"Enabling TTL on {table_name} (attribute: {attr})")
    try:
        ddb.update_time_to_live(TableName=table_name,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": attr})
        log_ok(f"TTL enabled on {table_name}")
    except ClientError as e:
        log_warn(f"TTL update on {table_name}: {e}")


def ensure_gsi(ddb, table_name: str, gsi_name: str, pk_attr: str):
    """Add a GSI to a table if it doesn't already exist."""
    log_info(f"Checking GSI: {gsi_name} on {table_name}")
    desc = ddb.describe_table(TableName=table_name)["Table"]
    existing = {g["IndexName"] for g in desc.get("GlobalSecondaryIndexes", [])}
    if gsi_name in existing:
        log_skip(f"GSI {gsi_name} already exists on {table_name}")
        return
    log_create(f"Adding GSI {gsi_name} to {table_name}  pk={pk_attr}")
    ddb.update_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": pk_attr, "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[{"Create": {
            "IndexName": gsi_name,
            "KeySchema": [{"AttributeName": pk_attr, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }}],
    )
    log_info(f"Waiting for GSI {gsi_name} to become ACTIVE...")
    for _ in range(40):
        time.sleep(5)
        try:
            d2 = ddb.describe_table(TableName=table_name)["Table"]
            for g in d2.get("GlobalSecondaryIndexes", []):
                if g["IndexName"] == gsi_name and g.get("IndexStatus") == "ACTIVE":
                    log_ok(f"GSI {gsi_name} ACTIVE on {table_name}")
                    return
        except ClientError:
            pass
    log_warn(f"GSI {gsi_name} may not be ACTIVE yet — check manually")


# ─── IAM ──────────────────────────────────────────────────────────────────────

_TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]
})


def ensure_role(iam, role_name: str, inline_policy: dict) -> tuple:
    """Get-or-create IAM role, always overwrite inline policy. Returns (was_created, role_arn)."""
    log_info(f"Checking role: {role_name}")
    created = False
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        log_skip(f"Role exists — updating policy  arn={role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntityException":
            raise
        log_create(f"Role not found — creating {role_name}...")
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=_TRUST_POLICY,
            Description=f"deploy-managed role {role_name}",
        )["Role"]["Arn"]
        log_ok(f"Role created  arn={role_arn}")
        log_info("Sleeping 10s for IAM propagation...")
        time.sleep(10)
        created = True

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{role_name}-policy",
        PolicyDocument=json.dumps(inline_policy),
    )
    log_ok(f"Role policy applied  arn={role_arn}")
    return created, role_arn


# ─── IAM Policy Statement Helpers ─────────────────────────────────────────────

def _stmt(effect, actions, resources):
    return {"Effect": effect, "Action": actions, "Resource": resources}


def st_cw(account_id, region):
    return _stmt("Allow",
                 ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                 f"arn:aws:logs:{region}:{account_id}:log-group:/aws/lambda/*")


def st_xray():
    return _stmt("Allow", ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "*")


def st_ddb(actions, account_id, region, *tables, idx=False):
    arns = []
    for t in tables:
        arns.append(f"arn:aws:dynamodb:{region}:{account_id}:table/{t}")
        if idx:
            arns.append(f"arn:aws:dynamodb:{region}:{account_id}:table/{t}/index/*")
    return _stmt("Allow", actions, arns)


def st_sm(*secret_arns):
    return _stmt("Allow", ["secretsmanager:GetSecretValue"], list(secret_arns))


def st_s3(bucket):
    return _stmt("Allow", ["s3:GetObject", "s3:ListBucket"],
                 [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"])


def st_kms(key_arn, actions=None):
    return _stmt("Allow", actions or ["kms:Decrypt", "kms:GenerateDataKey"], key_arn)


def st_iot(account_id, region):
    return _stmt("Allow", ["iot:Publish"],
                 f"arn:aws:iot:{region}:{account_id}:topic/iot/device/*")


def st_stream(account_id, region, table_name):
    return _stmt("Allow",
                 ["dynamodb:GetRecords", "dynamodb:GetShardIterator",
                  "dynamodb:DescribeStream", "dynamodb:ListStreams"],
                 f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}/stream/*")


def make_policy(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


# ─── Lambda Packaging ─────────────────────────────────────────────────────────

def package_lambda(source_dir: str, fn_name: str, build_root: str) -> str:
    """Package a Lambda into a zip. Returns zip path."""
    zip_path = os.path.join(build_root, f"{fn_name}.zip")
    log_info(f"Packaging {fn_name} from {source_dir}")

    req_file = os.path.join(source_dir, "requirements.txt")
    deps_dir = os.path.join(build_root, f"{fn_name}_deps")

    if os.path.exists(req_file):
        if os.path.exists(deps_dir):
            shutil.rmtree(deps_dir)
        os.makedirs(deps_dir, exist_ok=True)
        log_info(f"  requirements.txt found — installing Python deps (manylinux/python3.12)...")
        cmd = [sys.executable, "-m", "pip", "install", "-r", req_file, "-t", deps_dir,
               "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
               "--only-binary=:all:", "--implementation", "cp", "--quiet"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            log_warn("  manylinux wheels unavailable — retrying without platform flags...")
            cmd2 = [sys.executable, "-m", "pip", "install", "-r", req_file,
                    "-t", deps_dir, "--quiet"]
            res2 = subprocess.run(cmd2, capture_output=True, text=True)
            if res2.returncode != 0:
                log_error(f"  pip install failed:\n{res2.stderr}")
                raise RuntimeError(f"pip install failed for {fn_name}")
        size_kb = sum(os.path.getsize(os.path.join(dp, f))
                      for dp, _, files in os.walk(deps_dir)
                      for f in files) // 1024
        log_ok(f"  Deps installed  size={size_kb} KB")
    else:
        deps_dir = None

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if deps_dir and os.path.exists(deps_dir):
            for root, dirs, files in os.walk(deps_dir):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fname in files:
                    if fname.endswith(".pyc"):
                        continue
                    fp = os.path.join(root, fname)
                    zf.write(fp, os.path.relpath(fp, deps_dir))
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
            for fname in files:
                if fname.endswith(".pyc") or fname == "requirements.txt":
                    continue
                fp = os.path.join(root, fname)
                zf.write(fp, os.path.relpath(fp, source_dir))

    kb = os.path.getsize(zip_path) // 1024
    log_ok(f"  Zip ready: {zip_path}  ({kb} KB)")
    return zip_path


# ─── Lambda Deploy ────────────────────────────────────────────────────────────

def deploy_lambda(session, fn_name: str, zip_path: str, role_arn: str,
                  timeout: int, memory: int, env_vars: dict, region: str) -> str:
    """Create or update a Lambda. Returns function ARN."""
    log_info(f"Deploying {fn_name} → {region}  timeout={timeout}s  memory={memory}MB")
    lam = make_client(session, "lambda", region)
    env = {"Variables": {k: str(v) for k, v in env_vars.items() if v is not None and v != ""}}

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    try:
        existing = lam.get_function(FunctionName=fn_name)
        arn = existing["Configuration"]["FunctionArn"]
        log_update(f"Function exists — uploading new code ({len(zip_bytes)//1024} KB)...")
        lam.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        log_info("Waiting for code update to complete...")
        lam.get_waiter("function_updated").wait(FunctionName=fn_name,
                                                WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok("Code updated")
        log_info("Updating configuration (role, env vars, timeout)...")
        lam.update_function_configuration(
            FunctionName=fn_name, Role=role_arn,
            Timeout=timeout, MemorySize=memory,
            Runtime="python3.12",
            Handler="lambda_function.lambda_handler",
            Environment=env,
        )
        log_info("Waiting for configuration update to complete...")
        lam.get_waiter("function_updated").wait(FunctionName=fn_name,
                                                WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok(f"Lambda {fn_name} deployed  region={region}  arn={arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log_create(f"Function not found — creating {fn_name}...")
        resp = lam.create_function(
            FunctionName=fn_name, Runtime="python3.12", Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Timeout=timeout, MemorySize=memory, Environment=env,
        )
        arn = resp["FunctionArn"]
        log_info("Waiting for function to become active...")
        lam.get_waiter("function_active").wait(FunctionName=fn_name,
                                               WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok(f"Lambda {fn_name} created  region={region}  arn={arn}")
        return arn


def set_log_retention(session, fn_name: str, region: str, days: int = 30):
    logs = make_client(session, "logs", region)
    log_group = f"/aws/lambda/{fn_name}"
    try:
        logs.put_retention_policy(logGroupName=log_group, retentionInDays=days)
        log_ok(f"Log retention set: {log_group} → {days} days  ({region})")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            log_info(f"Log group {log_group} not yet created — will be set on first invocation")
        else:
            log_warn(f"Could not set log retention for {log_group}: {e}")


# ─── API Gateway ──────────────────────────────────────────────────────────────

def get_root_resource_id(apigw, api_id: str) -> str:
    for r in apigw.get_resources(restApiId=api_id, limit=500).get("items", []):
        if r.get("path") == "/":
            return r["id"]
    raise RuntimeError(f"Root resource '/' not found in API {api_id}")


def get_or_create_resource(apigw, api_id: str, parent_id: str, path_part: str) -> str:
    """Idempotent: return existing resource ID or create and return new one."""
    for r in apigw.get_resources(restApiId=api_id, limit=500).get("items", []):
        if r.get("parentId") == parent_id and r.get("pathPart") == path_part:
            log_skip(f"  Resource /{path_part} exists  id={r['id']}")
            return r["id"]
    log_create(f"  Creating resource /{path_part} under {parent_id}")
    resp = apigw.create_resource(restApiId=api_id, parentId=parent_id, pathPart=path_part)
    log_ok(f"  Resource /{path_part} created  id={resp['id']}")
    return resp["id"]


def ensure_cognito_authorizer(apigw, api_id: str, pool_arn: str) -> str:
    """Get or create Cognito authorizer named 'cognito_authorizer'."""
    for a in apigw.get_authorizers(restApiId=api_id).get("items", []):
        if a.get("name") == "cognito_authorizer":
            log_skip(f"Cognito authorizer exists  id={a['id']}")
            return a["id"]
    log_create("Creating Cognito authorizer 'cognito_authorizer'...")
    resp = apigw.create_authorizer(
        restApiId=api_id, name="cognito_authorizer",
        type="COGNITO_USER_POOLS", providerARNs=[pool_arn],
        identitySource="method.request.header.Authorization",
        authorizerResultTtlInSeconds=300,
    )
    log_ok(f"Cognito authorizer created  id={resp['id']}")
    return resp["id"]


def wire_method(session, apigw, api_id: str, resource_id: str, http_method: str,
                lambda_arn: str, api_region: str, account_id: str,
                fn_name: str, auth_type: str = "NONE", authorizer_id: str = None):
    """Wire HTTP method → Lambda (idempotent). Adds Lambda invoke permission."""
    log_info(f"  Wiring {http_method} → {fn_name}  auth={auth_type}")
    uri = (f"arn:aws:apigateway:{api_region}:lambda:path/2015-03-31"
           f"/functions/{lambda_arn}/invocations")

    # put_method (ConflictException = already exists, that's fine)
    try:
        kwargs = dict(restApiId=api_id, resourceId=resource_id,
                      httpMethod=http_method, authorizationType=auth_type)
        if auth_type == "COGNITO_USER_POOLS" and authorizer_id:
            kwargs["authorizerId"] = authorizer_id
        apigw.put_method(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        log_skip(f"    Method {http_method} already exists — updating integration")

    # put_integration (always overwrites)
    apigw.put_integration(restApiId=api_id, resourceId=resource_id,
                          httpMethod=http_method, type="AWS_PROXY",
                          integrationHttpMethod="POST", uri=uri)

    # Lambda permission (remove-then-add = idempotent)
    lam = make_client(session, "lambda", api_region)
    stmt_id = f"apigw-{fn_name.replace('_', '-')}-{http_method.lower()}"[:64]
    try:
        lam.remove_permission(FunctionName=lambda_arn, StatementId=stmt_id)
    except ClientError:
        pass
    try:
        lam.add_permission(
            FunctionName=lambda_arn, StatementId=stmt_id,
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{api_region}:{account_id}:{api_id}/*/*",
        )
        log_ok(f"    Lambda invoke permission granted → {fn_name}")
    except ClientError as e:
        log_warn(f"    Could not add Lambda permission: {e}")


def wire_cors(apigw, api_id: str, resource_id: str):
    """Add OPTIONS/MOCK CORS handler (idempotent)."""
    try:
        apigw.put_method(restApiId=api_id, resourceId=resource_id,
                         httpMethod="OPTIONS", authorizationType="NONE")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise

    apigw.put_integration(restApiId=api_id, resourceId=resource_id,
                          httpMethod="OPTIONS", type="MOCK",
                          requestTemplates={"application/json": '{"statusCode":200}'})
    _silent_put(apigw.put_method_response, restApiId=api_id, resourceId=resource_id,
                httpMethod="OPTIONS", statusCode="200",
                responseParameters={
                    "method.response.header.Access-Control-Allow-Origin": False,
                    "method.response.header.Access-Control-Allow-Headers": False,
                    "method.response.header.Access-Control-Allow-Methods": False,
                })
    _silent_put(apigw.put_integration_response, restApiId=api_id, resourceId=resource_id,
                httpMethod="OPTIONS", statusCode="200",
                responseParameters={
                    "method.response.header.Access-Control-Allow-Origin": "'*'",
                    "method.response.header.Access-Control-Allow-Headers": "'Content-Type,Authorization'",
                    "method.response.header.Access-Control-Allow-Methods": "'GET,POST,PUT,DELETE,OPTIONS'",
                },
                responseTemplates={"application/json": ""})
    log_ok(f"  CORS OPTIONS wired for resource {resource_id}")


def _silent_put(fn, **kwargs):
    try:
        fn(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise


# ─── Smoke Tests ──────────────────────────────────────────────────────────────

def smoke_test(method: str, url: str, expected: int, body: bytes = None) -> bool:
    try:
        req = Request(url, method=method, data=body)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=15) as r:
                actual = r.status
        except HTTPError as e:
            actual = e.code
        passed = actual == expected
        if passed:
            log_ok(f"  {method:<7} {url}  →  HTTP {actual}  expected={expected}  [PASS]")
        else:
            log_warn(f"  {method:<7} {url}  →  HTTP {actual}  expected={expected}  [FAIL]")
        return passed
    except URLError as e:
        log_warn(f"  {method:<7} {url}  →  connection error: {e}  [FAIL]")
        return False


# ─── Interactive Deploy Confirm ───────────────────────────────────────────────

def confirm_deploy(env_label: str, script_name: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"\n  [DRY-RUN] Validation complete. No resources were created.")
        return False
    print(f"\n  {'━' * 58}")
    print(f"  Ready to deploy: {script_name}  env={env_label}")
    print(f"  This will CREATE or UPDATE AWS resources.")
    print(f"  {'━' * 58}")
    try:
        ans = input("  Proceed? [Y/n]:  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans == "" or ans.startswith("y")
