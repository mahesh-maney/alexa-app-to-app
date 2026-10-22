# deploy_google.py — Analysis

Python script that deploys **all Google Home resources** for one environment.
Nothing in this script touches Alexa. Fully idempotent — safe to re-run.

---

## Usage

```bash
python3 deploy/deploy_google.py --config deploy/env.digilux.qa.json
python3 deploy/deploy_google.py --config deploy/env.honeywell.prod.json
python3 deploy/deploy_google.py --config deploy/env.honeywell.prod.json --dry-run
```

**Flags:**
- `--config <file>` — path to env JSON (required)
- `--dry-run` — validate config + AWS credentials, print plan, exit without deploying

---

## Prerequisites

- Python 3.9+, `boto3`, `pip`, `zip` (system)
- AWS credentials configured (`aws configure` or `AWS_PROFILE`)
- API Gateway ID must already exist in `API_GATEWAY_ID` (or left blank to auto-create)

---

## Config Keys Consumed

| Key | Example (Digilux QA) | Purpose |
|-----|----------------------|---------|
| `AWS_REGION` | `ap-south-1` | All GH Lambda deployments + DDB + API GW |
| `ROLE_PREFIX` | `digilux` | IAM role name prefix |
| `ENV_LABEL` | `digilux-qa` | Display label only |
| `API_GATEWAY_ID` | `ds6nxf8ac5` | API GW to wire routes into |
| `API_STAGE` | `smarthome` | Stage name for deployment |
| `API_BASE_URL` | `https://iot.digilux.co.in/smarthome` | Smoke tests + console checklist |
| `COGNITO_USER_POOL_ID` | `ap-south-1_h1o8s7257` | Cognito authorizer |
| `COGNITO_REGION` | `ap-south-1` | Cognito pool region |
| `COGNITO_CLIENT_ID` | `q7189jitfkk4ttesepkgls491` | OAuth authorize + consent Lambdas |
| `COGNITO_HOSTED_UI_DOMAIN` | `auth.digilux.co.in` | OAuth authorize Lambda |
| `GH_SESSIONS_TABLE` | `google_home_link_sessions` | DDB table: app-flow sessions |
| `GH_AUTH_CODES_TABLE` | `google_home_auth_codes` | DDB table: short-lived auth codes |
| `GH_TOKENS_TABLE` | `google_home_tokens` | DDB table: linked user tokens |
| `GH_OAUTH_TRANSACTIONS_TABLE` | `google_home_oauth_transactions` | DDB table: OAuth state |
| `GH_TOKENS_ACCESS_TOKEN_INDEX` | `accessToken-index` | GSI name on tokens table |
| `GOOGLE_SECRET_ARN` | `arn:aws:secretsmanager:ap-south-1:…` | GH OAuth client secret |
| `GOOGLE_SECRET_REGION` | `ap-south-1` | Region of GH secret |
| `GOOGLE_AGENT_ID` | `digilux-ai-pro` | GH project/agent ID |
| `GOOGLE_CLIENT_ID` | `75gmuum6qhfjsedrvck809oie6` | GH OAuth client ID |
| `GOOGLE_REDIRECT_URI` | `https://oauth-redirect.googleusercontent.com/r/…` | GH token redirect |
| `GOOGLE_SA_KEY_SECRET_ARN` | `` (empty = optional) | SA key secret for unlink HomeGraph call |
| `GH_SA_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | SA key for proactive reports |
| `GH_SA_SECRET_REGION` | `eu-west-1` | Region of SA key secret |
| `USER_DEVICE_DETAILS_TABLE` | `user_device_details` | Device list for fulfillment + proactive |
| `DEVICE_STATE_TABLE` | `device_state` | Device states for fulfillment |
| `SCENE_TABLE` | `digilux_scene_data` | Scenes for fulfillment |
| `SCENE_GSI_NAME` | `GSI_scene` | GSI on scene table |
| `METADATA_BUCKET` | `digilux-metadata-bucket` | S3 bucket for device metadata |
| `ENDPOINT_KEY_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | Endpoint encryption key |
| `ENDPOINT_KEY_REGION` | `eu-west-1` | Region of endpoint key secret |
| `DEVICE_MANUFACTURER` | `Digilux` | Sent to Google in SYNC response |
| `DEVICE_MODEL` | `Digilux Smart Device` | Sent to Google in SYNC response |
| `IOT_DATA_ENDPOINT` | `https://a2yxnt6tjmcgb1-ats.iot…` | MQTT endpoint for EXECUTE |
| `DEVICE_TABLE_SECRET_ARN` | `arn:aws:secretsmanager:ap-south-1:…` | Device table encrypt key |
| `DEVICE_TABLE_SECRET_REGION` | `ap-south-1` | Region |
| `APP_NAME` | `Digilux Smart Home` | Consent page vendor name |
| `BRAND_TITLE` | `Digilux Automation Pvt Ltd` | Consent page header |
| `BRAND_SUBTITLE` | `Identity Server` | Consent page sub-header |
| `TERMS_URL` | `https://digilux.co.in/terms.jsp` | Consent page T&C link |
| `PRIVACY_POLICY_URL` | `https://digilux.co.in/privacy-policy.jsp` | Consent page privacy link |
| `CONSENT_PAGE_HEADING` | `Authorization` | Consent `<h1>` |
| `CONSENT_NOTICE_TEXT` | `By clicking Allow…` | Notice box text |
| `CONSENT_PERM_LABEL` | `Granting permission to:` | Permissions label |
| `CONSENT_PERMISSIONS` | `discoverdevices,controldevices` | Comma-separated permission items |
| `CONSENT_TRUST_TEXT` | `Make sure you trust Google` | Trust paragraph |
| `CONSENT_DENY_LABEL` | `DENY` | Deny button |
| `CONSENT_ALLOW_LABEL` | `ALLOW` | Allow button |
| `LOG_LEVEL` | `INFO` | Lambda log level |
| `SESSION_TTL_SECONDS` | `1800` | Link session TTL |
| `AUTH_CODE_TTL_SECONDS` | `900` | Auth code TTL |
| `ACCESS_TOKEN_TTL_SECONDS` | `3600` | GH access token TTL |
| `REFRESH_TOKEN_TTL_SECONDS` | `15552000` | GH refresh token TTL |
| `TRANSACTION_TTL_SECONDS` | `600` | OAuth transaction TTL |
| `GH_DEEP_LINK_BASE_URL` | `https://home.google.com/home-app/` | Deep link for app flow |
| `GH_DEEP_LINK_PATH` | `setup/ha_linking` | Deep link path |

---

## Step-by-Step Execution

### Step 1 — Load + Validate Config

1. Parse `--config` argument. Load JSON file.
2. Abort if any value is literally `"FILL_IN"`.
3. Check required keys are non-empty:
   - Always: `AWS_REGION`, `ROLE_PREFIX`, `API_STAGE`, `API_BASE_URL`
   - GH-specific: `GOOGLE_SECRET_ARN`, `GOOGLE_AGENT_ID`, `GOOGLE_CLIENT_ID`, `COGNITO_HOSTED_UI_DOMAIN`, `GH_OAUTH_TRANSACTIONS_TABLE`, `APP_NAME`, `DEVICE_MANUFACTURER`
4. Compute derived values:
   - `COGNITO_CALLBACK_URI = {API_BASE_URL}/google-home/oauth/cognito/callback`
   - `ALLOWED_REDIRECT_URIS = "https://oauth-redirect.googleusercontent.com/r/{GOOGLE_AGENT_ID},https://oauth-redirect-sandbox.googleusercontent.com/r/{GOOGLE_AGENT_ID}"`

**Idempotency:** validation-only, no AWS calls.

---

### Step 2 — Preflight Checks

1. Verify `boto3` is importable.
2. Call `sts.get_caller_identity()` → confirm AWS credentials valid, capture `ACCOUNT_ID`.
3. Call `secretsmanager.describe_secret(SecretId=GOOGLE_SECRET_ARN, region=GOOGLE_SECRET_REGION)` → confirm secret is accessible.
4. If `GOOGLE_SA_KEY_SECRET_ARN` is non-empty, verify it too.
5. Print warning: *"Verify GOOGLE_SECRET_ARN client_secret EXACTLY matches Google Console — a mismatch causes 24–48h backoff."*

**Idempotency:** read-only AWS calls.

---

### Step 3 — DynamoDB Tables (4 tables + 2 GSIs)

For each table — **check before create:**
```
describe_table(TableName) → if ResourceNotFoundException → create
```

| Table | Partition Key | TTL attribute | Notes |
|-------|--------------|---------------|-------|
| `GH_SESSIONS_TABLE` | `state` (S) | `ttl` | App-flow CSRF sessions |
| `GH_AUTH_CODES_TABLE` | `code` (S) | `ttl` | Short-lived auth codes |
| `GH_TOKENS_TABLE` | `userId` (S) | — | Permanent linked user tokens |
| `GH_OAUTH_TRANSACTIONS_TABLE` | `state` (S) | `ttl` | OAuth flow state (600s) |

For each table:
- If table does not exist → `create_table` (PAY_PER_REQUEST billing) → `wait_until_table_exists`
- If table already exists → skip creation, print "already exists"
- After creation → `update_time_to_live` (enable TTL on `ttl` attribute) — idempotent if already enabled

**GSIs on `GH_TOKENS_TABLE`** (check before add):
- `accessToken-index` (PK: `accessToken`, ProjectionType: ALL)
- `refreshToken-index` (PK: `refreshToken`, ProjectionType: ALL)

Check: `describe_table` → inspect `Table.GlobalSecondaryIndexes[].IndexName`.
If GSI already exists → skip. If missing → `update_table` to add.

**Idempotency:** describe → skip if exists. All operations safe to re-run.

---

### Step 4 — API Gateway

1. If `API_GATEWAY_ID` is non-empty:
   - Call `apigateway.get_rest_api(restApiId)` → confirm it exists in `AWS_REGION`. Abort on error.
2. If `API_GATEWAY_ID` is empty:
   - `create_rest_api` (REGIONAL) → capture new ID, print warning to save it in config.
3. Look up Cognito authorizer named `cognito_authorizer`:
   - `get_authorizers` → filter by `name == "cognito_authorizer"`
   - If not found → `create_authorizer` (COGNITO_USER_POOLS, identity source: `Authorization` header)
   - Capture `AUTHORIZER_ID`
4. Get root resource `/` → capture `ROOT_ID`.

**Idempotency:** get before create on both API and authorizer.

---

### Step 5 — IAM Roles (9 roles)

For each role — **get before create:**
```
iam.get_role(RoleName) → if NoSuchEntityException → create_role first
iam.put_role_policy(...)   ← always called (overwrites if exists)
```

Trust policy (same for all): `lambda.amazonaws.com` can `sts:AssumeRole`.

After first-time creation: `time.sleep(10)` to allow IAM propagation before Lambda deploy.

#### Role: `{ROLE_PREFIX}-gh-start-role`
Permissions:
- `dynamodb:PutItem` on `google_home_link_sessions`
- CloudWatch Logs (create group/stream/put) on `/aws/lambda/*` in `AWS_REGION`
- X-Ray (PutTraceSegments, PutTelemetryRecords)

#### Role: `{ROLE_PREFIX}-gh-complete-role`
Permissions:
- `dynamodb:GetItem` on `google_home_link_sessions`
- `dynamodb:GetItem` on `google_home_tokens`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-status-role`
Permissions:
- `dynamodb:GetItem` on `google_home_tokens`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-unlink-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:DeleteItem` on `google_home_tokens`
- `secretsmanager:GetSecretValue` on `GOOGLE_SECRET_ARN`
  - If `GOOGLE_SA_KEY_SECRET_ARN` is non-empty → also include it
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-authorize-role`
Permissions:
- `dynamodb:PutItem` on `google_home_oauth_transactions`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-callback-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` on `google_home_oauth_transactions`
- `dynamodb:PutItem` on `google_home_auth_codes`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-token-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:DeleteItem` on `google_home_auth_codes`
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem` on `google_home_tokens`
- `secretsmanager:GetSecretValue` on `GOOGLE_SECRET_ARN`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-fulfillment-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:DeleteItem` on `google_home_tokens` table + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `user_device_details` table + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `device_state` table + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `scene_table` table + `index/*`
- `s3:GetObject`, `s3:ListBucket` on `metadata_bucket` + `metadata_bucket/*`
- `secretsmanager:GetSecretValue` on `ENDPOINT_KEY_SECRET_ARN`
- `iot:Publish` on `arn:aws:iot:{AWS_REGION}:{ACCOUNT_ID}:topic/iot/device/*`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-gh-proactive-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:Query` on `google_home_tokens` table + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `user_device_details` table + `index/*`
- `s3:GetObject`, `s3:ListBucket` on `metadata_bucket`
- `secretsmanager:GetSecretValue` on `ENDPOINT_KEY_SECRET_ARN`, `GH_SA_SECRET_ARN`
- `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:DescribeStream`, `dynamodb:ListStreams` on `user_device_details/stream/*`
- CloudWatch Logs, X-Ray

**Idempotency:** `get_role` → if not exists `create_role`. `put_role_policy` always overwrites (safe).

---

### Step 6 — Package Lambda Zips

For each Lambda source directory:

1. If `requirements.txt` exists in source dir:
   - `pip install -r requirements.txt -t /tmp/deploy_build/{name}_deps/ --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: --implementation cp`
   - Fallback: re-run without platform flags (local build)
   - Add all files from deps dir into zip first
2. Use Python `zipfile.ZipFile` to add all `.py` files + any asset files (e.g., `brand-logo.png`) from source dir
3. Exclude: `__pycache__/`, `*.pyc`, `tests/`, `requirements.txt`
4. Write zip to `/tmp/deploy_build_{ENV_LABEL}/{name}.zip`

Build dir is wiped and recreated fresh each run.

| Lambda function name | Source directory | Has requirements.txt? |
|---------------------|------------------|-----------------------|
| `google_home_start_link` | `lambdas/google_home_start_link/` | No |
| `google_home_app_link_complete` | `lambdas/google_home_app_link_complete/` | No |
| `google_home_link_status` | `lambdas/google_home_link_status/` | No |
| `google_home_app_unlink` | `lambdas/google_home_app_unlink/` | Yes (cryptography) |
| `google_home_oauth_authorize` | `lambdas/google_home_oauth_authorize/` | No |
| `google_home_oauth_cognito_callback` | `lambdas/google_home_oauth_consent_screen/` | No |
| `google_home_oauth_token` | `lambdas/google_home_oauth_token/` | No |
| `google_home_fulfillment` | `lambdas/google_home_fulfillment/` | Yes (pycryptodome) |
| `google_home_proactive` | `lambdas/google_home_proactive/` | No |

---

### Step 7 — Deploy Lambdas (all 9 to `AWS_REGION`)

For each Lambda — **get before update/create:**
```
lambda.get_function(FunctionName) → if exists → update; if not → create
```

**If Lambda exists:**
1. `update_function_code(ZipFile=zip_bytes)`
2. `lambda.get_waiter('function_updated').wait(FunctionName)`
3. `update_function_configuration(role, timeout, memory, environment)`
4. `lambda.get_waiter('function_updated').wait(FunctionName)`

**If Lambda does not exist:**
1. `create_function(runtime=python3.12, handler=lambda_function.lambda_handler, ...)`
2. `lambda.get_waiter('function_active').wait(FunctionName)`

After deploy:
- `logs.put_retention_policy(logGroupName=/aws/lambda/{name}, retentionInDays=30)` — idempotent

| Lambda | Timeout | Memory | Region |
|--------|---------|--------|--------|
| `google_home_start_link` | 15s | 256 MB | `AWS_REGION` |
| `google_home_app_link_complete` | 15s | 256 MB | `AWS_REGION` |
| `google_home_link_status` | 15s | 256 MB | `AWS_REGION` |
| `google_home_app_unlink` | 30s | 256 MB | `AWS_REGION` |
| `google_home_oauth_authorize` | 15s | 256 MB | `AWS_REGION` |
| `google_home_oauth_cognito_callback` | 15s | 256 MB | `AWS_REGION` |
| `google_home_oauth_token` | 15s | 256 MB | `AWS_REGION` |
| `google_home_fulfillment` | 15s | 256 MB | `AWS_REGION` |
| `google_home_proactive` | 60s | 256 MB | `AWS_REGION` |

**Environment variables per Lambda:**

`google_home_start_link`:
`DATA_REGION, LOG_LEVEL, GH_SESSIONS_TABLE, GOOGLE_AGENT_ID, GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI, GOOGLE_SCOPE=profile, OAUTH_BASE_URL={API_BASE_URL}, SESSION_TTL_SECONDS, GH_DEEP_LINK_BASE_URL, GH_DEEP_LINK_PATH`

`google_home_app_link_complete`:
`DATA_REGION, LOG_LEVEL, GH_SESSIONS_TABLE, GH_TOKENS_TABLE`

`google_home_link_status`:
`DATA_REGION, LOG_LEVEL, GH_TOKENS_TABLE, GOOGLE_AGENT_ID`

`google_home_app_unlink`:
`DATA_REGION, LOG_LEVEL, GH_TOKENS_TABLE, GOOGLE_SECRET_REGION, GOOGLE_SA_KEY_SECRET_ARN`

`google_home_oauth_authorize`:
`DATA_REGION, LOG_LEVEL, GOOGLE_CLIENT_ID, ALLOWED_REDIRECT_URIS (computed), COGNITO_CLIENT_ID, COGNITO_HOSTED_UI_DOMAIN, COGNITO_CALLBACK_URI (computed), GH_OAUTH_TRANSACTIONS_TABLE, TRANSACTION_TTL_SECONDS, APP_NAME`

`google_home_oauth_cognito_callback`:
`DATA_REGION, LOG_LEVEL, COGNITO_CLIENT_ID, COGNITO_HOSTED_UI_DOMAIN, COGNITO_CALLBACK_URI (computed), GH_OAUTH_TRANSACTIONS_TABLE, GH_AUTH_CODES_TABLE, AUTH_CODE_TTL_SECONDS, APP_NAME, BRAND_TITLE, BRAND_SUBTITLE, TERMS_URL, PRIVACY_POLICY_URL, CONSENT_PAGE_HEADING, CONSENT_NOTICE_TEXT, CONSENT_PERM_LABEL, CONSENT_PERMISSIONS, CONSENT_TRUST_TEXT, CONSENT_DENY_LABEL, CONSENT_ALLOW_LABEL`

`google_home_oauth_token`:
`DATA_REGION, LOG_LEVEL, GH_AUTH_CODES_TABLE, GH_TOKENS_TABLE, GOOGLE_CLIENT_SECRET_ARN={GOOGLE_SECRET_ARN}, GOOGLE_SECRET_REGION, GOOGLE_AGENT_ID, ACCESS_TOKEN_TTL_SECONDS, REFRESH_TOKEN_TTL_SECONDS`

`google_home_fulfillment`:
`DATA_REGION, LOG_LEVEL, GH_TOKENS_TABLE, GH_TOKENS_ACCESS_TOKEN_INDEX, USER_DEVICE_DETAILS_TABLE, DEVICE_STATE_TABLE, SCENE_TABLE, SCENE_GSI_NAME, METADATA_BUCKET, ENDPOINT_KEY_SECRET_ARN, ENDPOINT_KEY_REGION, DEVICE_MANUFACTURER, DEVICE_MODEL`

`google_home_proactive`:
`DATA_REGION, LOG_LEVEL, GH_TOKENS_TABLE, USER_DEVICE_DETAILS_TABLE, METADATA_BUCKET, ENDPOINT_KEY_SECRET_ARN, ENDPOINT_KEY_REGION, GH_SA_SECRET_ARN, GH_SA_SECRET_REGION`

---

### Step 8 — API Gateway Resource Tree + Method Wiring

**Resource tree created** (get_or_create for each node — idempotent):

```
/ (ROOT)
├── api/
│   └── v1/
│       └── voice/
│           └── google-home/
│               └── account-linking/          ← GET (status), DELETE (unlink)
│                   └── deep-link/
│                       ├── start/            ← POST (start_link)
│                       └── complete/         ← POST (app_link_complete)
└── google-home/
    ├── oauth/
    │   ├── authorize/                        ← GET + POST (oauth_authorize)
    │   ├── token/                            ← POST (oauth_token)
    │   ├── cognito/
    │   │   └── callback/                     ← GET (cognito_callback)
    │   └── consent/                          ← POST (consent)
    └── fulfillment/                          ← POST (fulfillment)
```

**For each resource node:** `get_resources` → filter `parentId + pathPart` → if found return id, if not `create_resource`.

**For each method wiring (idempotent — put overwrites):**
1. `put_method` (authorization type + authorizer ID for Cognito routes, NONE for public)
2. `put_integration` (AWS_PROXY, POST to Lambda ARN)
3. `remove_permission` on Lambda (ignore NoSuchResource) → `add_permission` (API GW invoke)
4. `put_method` OPTIONS → `put_integration` MOCK (CORS)
5. `put_method_response` + `put_integration_response` for CORS headers

| Route | Method | Lambda | Auth |
|-------|--------|--------|------|
| `/api/v1/voice/google-home/account-linking/deep-link/start` | POST | `google_home_start_link` | COGNITO_USER_POOLS |
| `/api/v1/voice/google-home/account-linking/deep-link/complete` | POST | `google_home_app_link_complete` | COGNITO_USER_POOLS |
| `/api/v1/voice/google-home/account-linking` | GET | `google_home_link_status` | COGNITO_USER_POOLS |
| `/api/v1/voice/google-home/account-linking` | DELETE | `google_home_app_unlink` | COGNITO_USER_POOLS |
| `/google-home/oauth/authorize` | GET | `google_home_oauth_authorize` | NONE |
| `/google-home/oauth/authorize` | POST | `google_home_oauth_authorize` | NONE |
| `/google-home/oauth/cognito/callback` | GET | `google_home_oauth_cognito_callback` | NONE |
| `/google-home/oauth/consent` | POST | `google_home_oauth_cognito_callback` | NONE |
| `/google-home/oauth/token` | POST | `google_home_oauth_token` | NONE |
| `/google-home/fulfillment` | POST | `google_home_fulfillment` | NONE |

---

### Step 9 — Deploy API Gateway Stage

`apigateway.create_deployment(restApiId, stageName=API_STAGE)` — always creates a new deployment (this is intentional; the stage is updated to point to the latest deployment).

---

### Step 10 — Smoke Tests

`time.sleep(5)` to allow cold-start readiness, then HTTP checks via `urllib.request`:

| Test | Method | URL | Expected HTTP |
|------|--------|-----|---------------|
| GH start — no auth | POST | `{API_BASE_URL}/api/v1/voice/google-home/account-linking/deep-link/start` | 401 |
| GH complete — no auth | POST | `{API_BASE_URL}/api/v1/voice/google-home/account-linking/deep-link/complete` | 401 |
| GH status — no auth | GET | `{API_BASE_URL}/api/v1/voice/google-home/account-linking` | 401 |
| GH unlink — no auth | DELETE | `{API_BASE_URL}/api/v1/voice/google-home/account-linking` | 401 |
| GH authorize — reachable | GET | `{API_BASE_URL}/google-home/oauth/authorize` | 400 |
| GH cognito callback — reachable | GET | `{API_BASE_URL}/google-home/oauth/cognito/callback` | 400 |
| GH token — reachable | POST | `{API_BASE_URL}/google-home/oauth/token` | 400 |

---

### Step 11 — Google Console Checklist (printed to stdout)

Prints exact values to configure in Google Home Developer Console:
- Fulfillment URL
- Client ID
- Client Secret ARN (to retrieve manually)
- Authorization URL
- Token URL
- Cognito callback URL (to add to Cognito app client allowed callback URLs)

---

### Step 12 — Manual Post-Deploy Steps (printed to stdout)

```
1. Enable DynamoDB stream on {USER_DEVICE_DETAILS_TABLE} (NEW_AND_OLD_IMAGES)
   then create event source mapping for google_home_proactive

2. google_home_app_unlink: if GOOGLE_SA_KEY_SECRET_ARN was blank,
   set it manually:
   aws lambda update-function-configuration \
     --function-name google_home_app_unlink \
     --environment "Variables={GOOGLE_SA_KEY_SECRET_ARN=<arn>,...}" \
     --region {AWS_REGION}
```

---

## What Gets Created / Updated (Summary)

| Resource type | Count | Names |
|---|---|---|
| DynamoDB tables | 4 | sessions, auth_codes, tokens, oauth_transactions |
| DynamoDB GSIs | 2 | accessToken-index, refreshToken-index on tokens table |
| IAM roles | 9 | gh-start, gh-complete, gh-status, gh-unlink, gh-authorize, gh-callback, gh-token, gh-fulfillment, gh-proactive |
| Lambda functions | 9 | all deployed to `AWS_REGION` |
| API GW resources | ~14 | full path tree |
| API GW methods | 10 routes + OPTIONS for each | — |
| API GW deployment | 1 | stage: `API_STAGE` |
| CloudWatch log retention | 9 | 30 days each |

---

## Rollback Considerations

- **Lambdas**: previous version still in Lambda's version history — can roll back via `update_function_code` with the old zip
- **DDB tables**: idempotent creates — no data is deleted or modified by this script
- **IAM roles**: `put_role_policy` overwrites — old policy is replaced but role itself persists
- **API GW**: methods are overwritten with `put_method`/`put_integration` — no routes are deleted

**This script never deletes anything.** All operations are additive or update-in-place.

---

## Logging Specification

### Format

Every log line is printed to stdout with a consistent prefix:

```
[HH:MM:SS] [LEVEL]  [STEP X — STEP NAME]  message
```

Examples:
```
[14:32:01] [INFO]   [STEP 1 — CONFIG]      Loaded config: deploy/env.honeywell.prod.json
[14:32:01] [INFO]   [STEP 1 — CONFIG]      ENV_LABEL=honeywell-prod  AWS_REGION=eu-west-1
[14:32:01] [OK]     [STEP 1 — CONFIG]      No FILL_IN placeholders found
[14:32:02] [INFO]   [STEP 2 — PREFLIGHT]   Checking AWS credentials...
[14:32:02] [OK]     [STEP 2 — PREFLIGHT]   AWS credentials valid  account=123456789012  region=eu-west-1
[14:32:03] [INFO]   [STEP 3 — DDB]         Checking table: google_home_link_sessions
[14:32:03] [SKIP]   [STEP 3 — DDB]         Table google_home_link_sessions already exists — skipping create
[14:32:03] [INFO]   [STEP 3 — DDB]         Checking table: google_home_auth_codes
[14:32:04] [CREATE] [STEP 3 — DDB]         Table google_home_auth_codes not found — creating...
[14:32:06] [OK]     [STEP 3 — DDB]         Table google_home_auth_codes created  pk=code  billing=PAY_PER_REQUEST
[14:32:06] [INFO]   [STEP 3 — DDB]         Enabling TTL on google_home_auth_codes (attribute: ttl)
[14:32:07] [OK]     [STEP 3 — DDB]         TTL enabled on google_home_auth_codes
[14:32:07] [INFO]   [STEP 3 — DDB]         Checking GSI: accessToken-index on google_home_tokens
[14:32:07] [SKIP]   [STEP 3 — DDB]         GSI accessToken-index already exists — skipping
```

### Log Levels

| Level | Meaning |
|-------|---------|
| `INFO` | Normal progress — a check is starting or an operation is in progress |
| `OK` | Step succeeded or resource already in desired state |
| `SKIP` | Resource already exists and is correct — no action taken (idempotency) |
| `CREATE` | Resource did not exist — creating it now |
| `UPDATE` | Resource exists — updating it |
| `WARN` | Non-fatal issue — deployment continues but attention may be needed |
| `ERROR` | Fatal error — deployment stops, full exception printed |

### Per-Step Log Detail

**Step 1 — Config:**
- Config file path
- Key values: ENV_LABEL, AWS_REGION, ROLE_PREFIX, API_BASE_URL
- Computed values: COGNITO_CALLBACK_URI, ALLOWED_REDIRECT_URIS
- List of all required keys checked with PASS/FAIL per key

**Step 2 — Preflight:**
- AWS account ID and region confirmed
- Each secret ARN accessibility check (name printed on success)
- Critical warning about Google client_secret mismatch risk (printed in yellow/WARN)

**Step 3 — DDB Tables:**
- For each table: "Checking…" → "already exists / creating…"
- For each create: "Waiting for table to become ACTIVE…" → "Table ACTIVE"
- For each GSI: "Checking GSI {name}…" → "exists / adding…"
- Final count: "DDB: 4 tables OK, 2 GSIs OK"

**Step 4 — API Gateway:**
- API GW ID being used
- Cognito authorizer ID (found or created)
- ROOT_ID captured

**Step 5 — IAM Roles:**
- For each role: "Checking role {name}…" → "exists — updating policy / not found — creating role + policy"
- After first-time create: "Sleeping 10s for IAM propagation…"
- Role ARN printed after each create/update

**Step 6 — Package Lambdas:**
- For each Lambda: "Packaging {name} from {source_dir}…"
- If requirements.txt: "Installing dependencies (pip)…" → "Dependencies installed: {size}"
- "Zip created: /tmp/…/{name}.zip  ({size} KB)"
- Total package time per Lambda

**Step 7 — Deploy Lambdas:**
- For each Lambda:
  - "Deploying {name} → {region}  timeout={t}s  memory={m}MB"
  - "Function exists — uploading new code…" or "Function not found — creating…"
  - "Waiting for update to complete…" (shown while waiter is polling)
  - "Updating configuration (env vars, role, timeout)…"
  - "Waiting for configuration update…"
  - "Lambda {name} deployed  region={region}  arn={arn}"
- At the end of all Lambdas: "Lambdas deployed: 9/9"

**Step 8 — API GW Routes:**
- For each resource node: "Resource {path} — exists ({id}) / creating under {parent_id}…"
- For each method: "Wiring {METHOD} {path} → {lambda_name}  auth={auth_type}"
- "Lambda invoke permission granted"
- "CORS OPTIONS added"
- Final count: "Routes wired: 10 methods on 8 resources"

**Step 9 — API GW Stage Deploy:**
- "Deploying stage '{API_STAGE}' on API {API_GATEWAY_ID}…"
- "Stage deployed  deployment_id={id}"
- "Sleeping 5s for cold-start readiness…"

**Step 10 — Smoke Tests:**
- For each test: "{METHOD} {url} → HTTP {actual}  expected={expected}  [PASS / FAIL]"
- Final count: "Smoke tests: 7 passed, 0 failed"

**Step 11 — Console Checklist:**
- Full checklist block printed with exact URLs and values for this deployment

**Final Summary:**
```
[14:35:22] [INFO]   [SUMMARY]  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[14:35:22] [OK]     [SUMMARY]  deploy_google.py completed  env=honeywell-prod
[14:35:22] [INFO]   [SUMMARY]  Duration: 3m 21s
[14:35:22] [INFO]   [SUMMARY]  Resources:
[14:35:22] [INFO]   [SUMMARY]    DDB tables  : 4 (2 created, 2 already existed)
[14:35:22] [INFO]   [SUMMARY]    IAM roles   : 9 (3 created, 6 updated)
[14:35:22] [INFO]   [SUMMARY]    Lambdas     : 9 (9 deployed to ap-south-1)
[14:35:22] [INFO]   [SUMMARY]    API GW      : 10 routes wired, stage deployed
[14:35:22] [INFO]   [SUMMARY]    Smoke tests : 7/7 passed
[14:35:22] [INFO]   [SUMMARY]  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**On any ERROR:**
```
[14:33:45] [ERROR]  [STEP 7 — LAMBDA DEPLOY]  Failed to deploy google_home_fulfillment
[14:33:45] [ERROR]  [STEP 7 — LAMBDA DEPLOY]  Exception: An error occurred (AccessDeniedException) ...
[14:33:45] [ERROR]  [STEP 7 — LAMBDA DEPLOY]  Traceback (most recent call last): ...
[14:33:45] [ERROR]  [SUMMARY]  Deployment FAILED at step 7.  Resources deployed before failure are left in place.
```

Full Python exception traceback is always printed on error so the exact AWS API error message is visible.
