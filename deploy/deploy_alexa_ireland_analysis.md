# deploy_alexa_ireland.py — Analysis

Python script that deploys **only `alexa_fulfillment`** to `ALEXA_FULFILLMENT_REGION` (eu-west-1 / Ireland).

This is a focused re-deploy script for the Alexa Smart Home fulfillment Lambda.
Use it when you need to update `alexa_fulfillment` without touching anything else —
for example, after a code change, or to re-deploy it independently of the full Alexa stack.

**Does NOT touch:** API Gateway, DDB, KMS, any other Lambda, or any other region.
Fully idempotent — safe to re-run.

---

## Usage

```bash
python3 deploy/deploy_alexa_ireland.py --config deploy/env.digilux.qa.json
python3 deploy/deploy_alexa_ireland.py --config deploy/env.honeywell.prod.json
python3 deploy/deploy_alexa_ireland.py --config deploy/env.honeywell.prod.json --dry-run
```

**Flags:**
- `--config <file>` — path to env JSON (required)
- `--dry-run` — validate config + resolve KMS key, print plan, exit without deploying

---

## Prerequisites

- Python 3.9+, `boto3`, `pip`
- AWS credentials that can deploy to `ALEXA_FULFILLMENT_REGION` (eu-west-1)
- KMS key alias **must already exist** in `AWS_REGION` — created by `deploy_alexa.py`
  - This script reads the key ARN from the existing alias; it does NOT create the key
- `lambdas/alexa_fulfillment/` directory must exist with `lambda_function.py`

---

## Config Keys Consumed

This script uses only the subset of config needed for `alexa_fulfillment`.

| Key | Example (Digilux QA) | Purpose |
|-----|----------------------|---------|
| `AWS_REGION` | `ap-south-1` | Where DDB tables live (used in IAM ARNs + Lambda env vars) |
| `ALEXA_FULFILLMENT_REGION` | `eu-west-1` | Where the Lambda is deployed |
| `ROLE_PREFIX` | `digilux` | IAM role name prefix |
| `ENV_LABEL` | `digilux-qa` | Display label |
| `ALEXA_TOKENS_TABLE` | `digilux_honeywell_alexa_lwa_tokens` | Lambda env var + IAM |
| `USER_DEVICE_DETAILS_TABLE` | `user_device_details` | Lambda env var + IAM |
| `DEVICE_STATE_TABLE` | `device_state` | Lambda env var + IAM |
| `SCENE_TABLE` | `digilux_scene_data` | Lambda env var + IAM |
| `METADATA_BUCKET` | `digilux-metadata-bucket` | Lambda env var + IAM |
| `ENDPOINT_KEY_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | Lambda env var + IAM |
| `ENDPOINT_KEY_REGION` | `eu-west-1` | Lambda env var |
| `LWA_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | Lambda env var + IAM |
| `LWA_SECRET_REGION` | `eu-west-1` | Lambda env var |
| `KMS_KEY_ALIAS` | `alias/digilux-alexa-tokens` | Resolved to KMS_KEY_ARN at runtime |
| `COGNITO_USER_POOL_ID` | `ap-south-1_h1o8s7257` | Lambda env var |
| `COGNITO_REGION` | `ap-south-1` | Lambda env var |
| `IOT_DATA_ENDPOINT` | `https://a2yxnt6tjmcgb1-ats.iot…` | Lambda env var |
| `DEVICE_TABLE_SECRET_ARN` | `arn:aws:secretsmanager:ap-south-1:…` | Lambda env var + IAM |
| `DEVICE_TABLE_SECRET_REGION` | `ap-south-1` | Lambda env var |
| `ALEXA_GATEWAY_URL` | `https://api.eu.amazonalexa.com/v3/events` | Lambda env var |
| `LOG_LEVEL` | `INFO` | Lambda env var |
| `ALEXA_SKILL_ID` | `amzn1.ask.skill.fc2dcda9-…` | Printed in post-deploy instructions |

---

## Step-by-Step Execution

### Step 1 — Load + Validate Config

1. Parse `--config` argument. Load JSON file.
2. Abort if any value is literally `"FILL_IN"`.
3. Check required keys are non-empty:
   - `AWS_REGION`, `ALEXA_FULFILLMENT_REGION`, `ROLE_PREFIX`
   - `ALEXA_TOKENS_TABLE`, `LWA_SECRET_ARN`, `LWA_SECRET_REGION`, `KMS_KEY_ALIAS`
   - `USER_DEVICE_DETAILS_TABLE`, `DEVICE_STATE_TABLE`, `METADATA_BUCKET`
   - `ENDPOINT_KEY_SECRET_ARN`, `IOT_DATA_ENDPOINT`

**Idempotency:** validation-only, no AWS calls.

---

### Step 2 — Preflight Checks

1. Call `sts.get_caller_identity()` → confirm AWS credentials valid, capture `ACCOUNT_ID`.
2. Resolve KMS key ARN:
   - `kms.list_aliases(region=AWS_REGION)` → find alias `KMS_KEY_ALIAS`
   - If found → `kms.describe_key(KeyId=alias, region=AWS_REGION)` → capture `KMS_KEY_ARN`
   - If NOT found → **abort with clear error**: "KMS key alias not found — run deploy_alexa.py first"
3. Verify `ALEXA_FULFILLMENT_REGION` is accessible: call `lambda.list_functions(region=ALEXA_FULFILLMENT_REGION, MaxItems=1)` — no-op but confirms connectivity.
4. Verify Lambda source directory exists: `lambdas/alexa_fulfillment/`

**Idempotency:** read-only AWS calls.

---

### Step 3 — IAM Role: `{ROLE_PREFIX}-alexa-fulfillment-role`

IAM is global — the role exists once and applies to the Lambda regardless of region.

**Strategy:** `iam.get_role(RoleName)` → if `NoSuchEntityException` → `create_role`, sleep 10. Always `put_role_policy`.

Trust policy:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
```

Inline policy — exact permissions:
```
DynamoDB (region: AWS_REGION):
  - GetItem, UpdateItem, DeleteItem  on  ALEXA_TOKENS_TABLE
  - Query, GetItem                   on  USER_DEVICE_DETAILS_TABLE + index/*
  - Query, GetItem                   on  DEVICE_STATE_TABLE + index/*
  - Query, GetItem                   on  SCENE_TABLE + index/*

S3:
  - GetObject, ListBucket            on  METADATA_BUCKET

Secrets Manager:
  - GetSecretValue                   on  LWA_SECRET_ARN
  - GetSecretValue                   on  ENDPOINT_KEY_SECRET_ARN
  - GetSecretValue                   on  DEVICE_TABLE_SECRET_ARN

KMS (region: AWS_REGION):
  - Decrypt                          on  KMS_KEY_ARN

IoT (region: AWS_REGION):
  - Publish                          on  arn:aws:iot:{AWS_REGION}:{ACCOUNT_ID}:topic/iot/device/*

CloudWatch Logs (region: ALEXA_FULFILLMENT_REGION):
  - CreateLogGroup, CreateLogStream, PutLogEvents
    on  arn:aws:logs:{ALEXA_FULFILLMENT_REGION}:{ACCOUNT_ID}:log-group:/aws/lambda/*

X-Ray:
  - PutTraceSegments, PutTelemetryRecords  on  *
```

**Why logs use `ALEXA_FULFILLMENT_REGION`:** Lambda writes its logs to the region it runs in.
The log group `/aws/lambda/alexa_fulfillment` will be created in eu-west-1, so the IAM
policy must grant PutLogEvents in eu-west-1, not ap-south-1.

**Idempotency:** `get_role` → skip `create_role` if exists. `put_role_policy` always overwrites.

---

### Step 4 — Package `alexa_fulfillment`

1. Create fresh build directory: `/tmp/deploy_build_{ENV_LABEL}_ireland/`
2. `alexa_fulfillment` has `requirements.txt` → run pip install:
   ```
   pip install -r lambdas/alexa_fulfillment/requirements.txt \
     -t /tmp/.../alexa_fulfillment_deps/ \
     --platform manylinux2014_x86_64 \
     --python-version 3.12 \
     --only-binary=:all: \
     --implementation cp
   ```
   Fallback: re-run without platform flags if binary wheels unavailable.
3. Create `alexa_fulfillment.zip`:
   - Add all files from deps directory first
   - Add all `.py` files from `lambdas/alexa_fulfillment/`
   - Exclude: `__pycache__/`, `*.pyc`, `tests/`, `requirements.txt`
4. Print zip size to stdout.

---

### Step 5 — Deploy `alexa_fulfillment` to `ALEXA_FULFILLMENT_REGION`

**Region:** `ALEXA_FULFILLMENT_REGION` (eu-west-1)

Check if function exists:
```
lambda.get_function(FunctionName='alexa_fulfillment', region=ALEXA_FULFILLMENT_REGION)
```

**If exists:**
1. `update_function_code(FunctionName, ZipFile=zip_bytes, region=eu-west-1)`
2. `lambda.get_waiter('function_updated').wait(FunctionName, region=eu-west-1)`
3. `update_function_configuration(role=ROLE_ARN, timeout=30, memory=256, environment=env_vars, region=eu-west-1)`
4. `lambda.get_waiter('function_updated').wait(FunctionName, region=eu-west-1)`
5. Print: "alexa_fulfillment updated in eu-west-1"

**If does not exist:**
1. `create_function(FunctionName, runtime=python3.12, role=ROLE_ARN, handler=lambda_function.lambda_handler, ZipFile, timeout=30, memory=256, environment=env_vars, region=eu-west-1)`
2. `lambda.get_waiter('function_active').wait(FunctionName, region=eu-west-1)`
3. Print: "alexa_fulfillment created in eu-west-1"

**Environment variables set on the Lambda:**
```
DATA_REGION            = {AWS_REGION}           ← note: ap-south-1, NOT eu-west-1
LOG_LEVEL              = {LOG_LEVEL}
LWA_TOKENS_TABLE       = {ALEXA_TOKENS_TABLE}
LWA_SECRET_ARN         = {LWA_SECRET_ARN}
LWA_SECRET_REGION      = {LWA_SECRET_REGION}
KMS_KEY_ARN            = {KMS_KEY_ARN}           ← resolved from alias
COGNITO_USER_POOL_ID   = {COGNITO_USER_POOL_ID}
COGNITO_REGION         = {COGNITO_REGION}
USER_DEVICE_DETAILS_TABLE = {USER_DEVICE_DETAILS_TABLE}
DEVICE_STATE_TABLE     = {DEVICE_STATE_TABLE}
SCENE_TABLE            = {SCENE_TABLE}
METADATA_BUCKET        = {METADATA_BUCKET}
ENDPOINT_KEY_SECRET_ARN = {ENDPOINT_KEY_SECRET_ARN}
ENDPOINT_KEY_REGION    = {ENDPOINT_KEY_REGION}
DEVICE_TABLE_SECRET_ARN = {DEVICE_TABLE_SECRET_ARN}
DEVICE_TABLE_SECRET_REGION = {DEVICE_TABLE_SECRET_REGION}
ALEXA_GATEWAY_URL      = {ALEXA_GATEWAY_URL}
IOT_DATA_ENDPOINT      = {IOT_DATA_ENDPOINT}
```

**Key distinction:** `DATA_REGION` is `AWS_REGION` (ap-south-1) because all DynamoDB tables
and IoT are in the main region. The Lambda executes in eu-west-1 but reads/writes data in ap-south-1.

---

### Step 6 — CloudWatch Log Retention

```
logs.put_retention_policy(
  logGroupName='/aws/lambda/alexa_fulfillment',
  retentionInDays=30,
  region=ALEXA_FULFILLMENT_REGION
)
```

This call is idempotent — if the log group doesn't exist yet it may return an error which is silently ignored (Lambda will create it on first invocation). If it does exist, retention is set/updated to 30 days.

---

### Step 7 — Print Lambda ARN + Post-Deploy Instructions

Prints to stdout:
```
Lambda ARN:
  arn:aws:lambda:eu-west-1:{ACCOUNT_ID}:function:alexa_fulfillment

MANUAL STEPS (must be done once per new account):

1. Add Alexa Smart Home trigger (resource policy):
   aws lambda add-permission \
     --function-name alexa_fulfillment \
     --statement-id alexa-smart-home \
     --action lambda:InvokeFunction \
     --principal alexa-connectedhome.amazon.com \
     --event-source-token {ALEXA_SKILL_ID} \
     --region eu-west-1

   Note: If statement-id already exists, remove it first:
   aws lambda remove-permission \
     --function-name alexa_fulfillment \
     --statement-id alexa-smart-home \
     --region eu-west-1

2. Register this Lambda ARN in the Alexa Developer Console:
   Developer Console → Your Skill → Build → Endpoint
   → Default endpoint: arn:aws:lambda:eu-west-1:{ACCOUNT_ID}:function:alexa_fulfillment
   → Save and re-build the skill model
```

---

## What Gets Created / Updated (Summary)

| Resource | Action | Notes |
|---|---|---|
| IAM role `{PREFIX}-alexa-fulfillment-role` | Create or update policy | Global IAM, no region |
| Lambda `alexa_fulfillment` in eu-west-1 | Create or update | Code + config both updated |
| CloudWatch log retention | Set to 30 days | In eu-west-1 |
| KMS key | **Read only** — never created by this script | Must pre-exist |
| DDB tables | **Not touched** | Must pre-exist |
| API Gateway | **Not touched** | fulfillment is not API GW-backed |

---

## Rollback Considerations

- **Lambda**: previous code in Lambda version history — roll back by re-running this script with older source
- **IAM role**: `put_role_policy` overwrites — but role itself is never deleted
- **This script never deletes anything**

---

## When to Use This Script vs deploy_alexa.py

| Scenario | Use |
|---|---|
| First-time deployment of Alexa stack on a new account | `deploy_alexa.py` |
| Updating only `alexa_fulfillment` code | `deploy_alexa_ireland.py` |
| Re-deploying all Alexa Lambdas after a bulk code change | `deploy_alexa.py` |
| Honeywell go-live: deploy Alexa stack fresh | `deploy_alexa.py` |
| Hotfix to fulfillment only, don't want to re-deploy 8 other Lambdas | `deploy_alexa_ireland.py` |

---

## Logging Specification

### Format

Every log line is printed to stdout with a consistent prefix:

```
[HH:MM:SS] [LEVEL]  [STEP X — STEP NAME]  message
```

### Log Levels

| Level | Meaning |
|-------|---------|
| `INFO` | Normal progress — a check is starting or in progress |
| `OK` | Step succeeded |
| `SKIP` | Resource already in desired state — no action taken |
| `CREATE` | Resource did not exist — creating now |
| `UPDATE` | Resource exists — updating |
| `WARN` | Non-fatal — continues but needs attention |
| `ERROR` | Fatal — stops immediately, full exception printed |

### Per-Step Log Detail

**Step 1 — Config:**
```
[14:32:00] [INFO]  [STEP 1 — CONFIG]  Loaded: deploy/env.honeywell.prod.json
[14:32:00] [INFO]  [STEP 1 — CONFIG]  ENV_LABEL=honeywell-prod
[14:32:00] [INFO]  [STEP 1 — CONFIG]  AWS_REGION=eu-west-1  (main region — DDB lives here)
[14:32:00] [INFO]  [STEP 1 — CONFIG]  ALEXA_FULFILLMENT_REGION=eu-west-1  (Ireland — Lambda deploys here)
[14:32:00] [INFO]  [STEP 1 — CONFIG]  Checking required keys...
[14:32:00] [OK]    [STEP 1 — CONFIG]  All required config keys present
```

**Step 2 — Preflight:**
```
[14:32:01] [INFO]  [STEP 2 — PREFLIGHT]  Verifying AWS credentials...
[14:32:01] [OK]    [STEP 2 — PREFLIGHT]  Credentials valid  account=123456789012
[14:32:01] [INFO]  [STEP 2 — PREFLIGHT]  Resolving KMS alias: alias/honeywell-alexa-tokens in eu-west-1...
[14:32:02] [OK]    [STEP 2 — PREFLIGHT]  KMS alias found  KeyArn=arn:aws:kms:eu-west-1:…
[14:32:02] [INFO]  [STEP 2 — PREFLIGHT]  Checking ALEXA_FULFILLMENT_REGION (eu-west-1) connectivity...
[14:32:02] [OK]    [STEP 2 — PREFLIGHT]  eu-west-1 reachable
[14:32:02] [INFO]  [STEP 2 — PREFLIGHT]  Checking Lambda source: lambdas/alexa_fulfillment/
[14:32:02] [OK]    [STEP 2 — PREFLIGHT]  Source directory found
```
If KMS alias not found:
```
[14:32:02] [ERROR] [STEP 2 — PREFLIGHT]  KMS alias 'alias/honeywell-alexa-tokens' not found in eu-west-1
[14:32:02] [ERROR] [STEP 2 — PREFLIGHT]  The KMS key is created by deploy_alexa.py — run that first
[14:32:02] [ERROR] [SUMMARY]             Deployment aborted at preflight.
```

**Step 3 — IAM Role:**
```
[14:32:03] [INFO]  [STEP 3 — IAM ROLE]  Checking role: honeywell-alexa-fulfillment-role
[14:32:03] [SKIP]  [STEP 3 — IAM ROLE]  Role exists — updating inline policy
[14:32:03] [INFO]  [STEP 3 — IAM ROLE]  Policy covers: DDB(eu-west-1), S3, SecretsManager, KMS, IoT, CWLogs(eu-west-1), XRay
[14:32:04] [OK]    [STEP 3 — IAM ROLE]  Role policy updated  arn=arn:aws:iam::…:role/honeywell-alexa-fulfillment-role
```
If role is new:
```
[14:32:03] [CREATE][STEP 3 — IAM ROLE]  Role not found — creating honeywell-alexa-fulfillment-role...
[14:32:04] [OK]    [STEP 3 — IAM ROLE]  Role created
[14:32:04] [INFO]  [STEP 3 — IAM ROLE]  Sleeping 10s for IAM propagation...
```

**Step 4 — Package Lambda:**
```
[14:32:14] [INFO]  [STEP 4 — PACKAGE]  Packaging alexa_fulfillment from lambdas/alexa_fulfillment/
[14:32:14] [INFO]  [STEP 4 — PACKAGE]  requirements.txt found — installing Python deps...
[14:32:14] [INFO]  [STEP 4 — PACKAGE]  pip install (manylinux wheels, python 3.12)...
[14:32:22] [OK]    [STEP 4 — PACKAGE]  Deps installed  size=18 MB
[14:32:22] [INFO]  [STEP 4 — PACKAGE]  Creating zip...
[14:32:23] [OK]    [STEP 4 — PACKAGE]  Zip ready: /tmp/deploy_build_honeywell-prod_ireland/alexa_fulfillment.zip  (19.2 MB)
```

**Step 5 — Deploy Lambda:**
```
[14:32:23] [INFO]  [STEP 5 — DEPLOY]   Deploying alexa_fulfillment to eu-west-1
[14:32:23] [INFO]  [STEP 5 — DEPLOY]   timeout=30s  memory=256MB  runtime=python3.12
[14:32:23] [INFO]  [STEP 5 — DEPLOY]   NOTE: DATA_REGION=ap-south-1 — DDB tables live in main region
[14:32:23] [INFO]  [STEP 5 — DEPLOY]   Checking if function exists in eu-west-1...
[14:32:24] [UPDATE][STEP 5 — DEPLOY]   Function exists — uploading new code...
[14:32:38] [INFO]  [STEP 5 — DEPLOY]   Waiting for code update to complete...
[14:32:41] [OK]    [STEP 5 — DEPLOY]   Code updated
[14:32:41] [INFO]  [STEP 5 — DEPLOY]   Updating configuration (role, env vars, timeout)...
[14:32:43] [INFO]  [STEP 5 — DEPLOY]   Waiting for config update to complete...
[14:32:45] [OK]    [STEP 5 — DEPLOY]   alexa_fulfillment deployed  region=eu-west-1
[14:32:45] [OK]    [STEP 5 — DEPLOY]   ARN: arn:aws:lambda:eu-west-1:123456789012:function:alexa_fulfillment
```

**Step 6 — CW Log Retention:**
```
[14:32:46] [INFO]  [STEP 6 — LOGS]  Setting log retention: /aws/lambda/alexa_fulfillment → 30 days (eu-west-1)
[14:32:46] [OK]    [STEP 6 — LOGS]  Retention set
```

**Step 7 — Post-Deploy Instructions:**
```
[14:32:46] [INFO]  [STEP 7 — POST-DEPLOY]  ── Manual steps required ──────────────────────────────
[14:32:46] [WARN]  [STEP 7 — POST-DEPLOY]  The following CANNOT be automated — complete them manually:
...exact CLI commands printed...
```

**Final Summary:**
```
[14:32:47] [OK]    [SUMMARY]  deploy_alexa_ireland.py completed  env=honeywell-prod
[14:32:47] [INFO]  [SUMMARY]  Duration: 47s
[14:32:47] [INFO]  [SUMMARY]  Lambda ARN: arn:aws:lambda:eu-west-1:123456789012:function:alexa_fulfillment
[14:32:47] [WARN]  [SUMMARY]  Remember: add Alexa Smart Home trigger + register ARN in Developer Console
```

**On ERROR:** Full Python exception traceback printed immediately. Deployment stops. Any resources already deployed before the error are left in place (not rolled back).
