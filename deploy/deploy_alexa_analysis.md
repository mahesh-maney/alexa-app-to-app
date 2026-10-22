# deploy_alexa.py — Analysis

Python script that deploys **all Alexa resources** for one environment,
including `alexa_fulfillment` in Ireland (`ALEXA_FULFILLMENT_REGION` = eu-west-1).
Nothing in this script touches Google Home. Fully idempotent — safe to re-run.

---

## Usage

```bash
python3 deploy/deploy_alexa.py --config deploy/env.digilux.qa.json
python3 deploy/deploy_alexa.py --config deploy/env.honeywell.prod.json
python3 deploy/deploy_alexa.py --config deploy/env.honeywell.prod.json --dry-run
```

**Flags:**
- `--config <file>` — path to env JSON (required)
- `--dry-run` — validate config + AWS credentials, print plan, exit without deploying

---

## Prerequisites

- Python 3.9+, `boto3`, `pip`
- AWS credentials that can deploy to both `AWS_REGION` **and** `ALEXA_FULFILLMENT_REGION`
- API Gateway ID in `API_GATEWAY_ID` (or blank to auto-create — same API GW as GH)
- KMS key alias `KMS_KEY_ALIAS` will be created if it does not exist

---

## Config Keys Consumed

| Key | Example (Digilux QA) | Purpose |
|-----|----------------------|---------|
| `AWS_REGION` | `ap-south-1` | All Alexa Lambdas (except fulfillment) + DDB + API GW |
| `ALEXA_FULFILLMENT_REGION` | `eu-west-1` | Region for `alexa_fulfillment` Lambda |
| `ROLE_PREFIX` | `digilux` | IAM role name prefix |
| `ENV_LABEL` | `digilux-qa` | Display label |
| `API_GATEWAY_ID` | `ds6nxf8ac5` | API GW ID |
| `API_STAGE` | `smarthome` | Stage name |
| `API_BASE_URL` | `https://iot.digilux.co.in/smarthome` | Smoke test base URL |
| `COGNITO_USER_POOL_ID` | `ap-south-1_h1o8s7257` | Cognito authorizer |
| `COGNITO_REGION` | `ap-south-1` | Cognito pool region |
| `COGNITO_CLIENT_ID` | `q7189jitfkk4ttesepkgls491` | Lambda env var |
| `ALEXA_SESSIONS_TABLE` | `alexa_app_linking_sessions` | DDB: session table |
| `ALEXA_TOKENS_TABLE` | `digilux_honeywell_alexa_lwa_tokens` | DDB: LWA token table |
| `USER_DEVICE_MAPPING_TABLE` | `digilux_honeywell_user_device_mapping` | Device mapping (skill-evt, complete, unlink) |
| `USER_DEVICE_DETAILS_TABLE` | `user_device_details` | State reporter + proactive + fulfillment |
| `DEVICE_STATE_TABLE` | `device_state` | State reporter + fulfillment |
| `SCENE_TABLE` | `digilux_scene_data` | Proactive + fulfillment |
| `SCENE_GSI_NAME` | `GSI_scene` | Scene table GSI |
| `METADATA_BUCKET` | `digilux-metadata-bucket` | S3 metadata |
| `ENDPOINT_KEY_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | Endpoint encryption key |
| `ENDPOINT_KEY_REGION` | `eu-west-1` | Region of endpoint key |
| `LWA_SECRET_ARN` | `arn:aws:secretsmanager:eu-west-1:…` | LWA client credentials |
| `LWA_SECRET_REGION` | `eu-west-1` | LWA secret region |
| `KMS_KEY_ALIAS` | `alias/digilux-alexa-tokens` | KMS key alias |
| `ALEXA_SKILL_ID` | `amzn1.ask.skill.fc2dcda9-…` | Skill ID |
| `ALEXA_SKILL_STAGE` | `development` | Skill stage |
| `ALEXA_REDIRECT_URI` | `https://iot.digilux.co.in/smarthome/alexa/callback` | OAuth redirect URI |
| `ALEXA_REDIRECT_HOSTS` | `iot.digilux.co.in` | Allowed redirect host |
| `APP_DEEP_LINK_SCHEME` | `digilux` | App deep link scheme (sent as `APP_SCHEME`) |
| `APP_NAME` | `Digilux Smart Home` | Display name |
| `DEVICE_MANUFACTURER` | `Digilux` | Used by fulfillment |
| `LOG_LEVEL` | `INFO` | Lambda log level |
| `ALEXA_SESSION_TTL_SECONDS` | `600` | Session TTL (10 min) |
| `CREATE_UPDATE_FN` | `create_and_update_data_function` | Lambda name for device sync |
| `LWA_TOKEN_URL` | `https://api.amazon.com/auth/o2/token` | LWA token endpoint |
| `LWA_REVOKE_URL` | `https://api.amazon.com/auth/o2/revoke` | LWA revoke endpoint |
| `SKILL_ENABLEMENT_URL` | `https://api.amazonalexa.com/v1/users/~current/skills` | Skill enablement API |
| `ALEXA_GATEWAY_URL` | `https://api.eu.amazonalexa.com/v3/events` | Proactive events gateway |
| `IOT_DATA_ENDPOINT` | `https://a2yxnt6tjmcgb1-ats.iot…` | MQTT endpoint |
| `DEVICE_TABLE_SECRET_ARN` | `arn:aws:secretsmanager:ap-south-1:…` | Device table key |
| `DEVICE_TABLE_SECRET_REGION` | `ap-south-1` | Region |

---

## Step-by-Step Execution

### Step 1 — Load + Validate Config

1. Parse `--config` argument. Load JSON file.
2. Abort if any value is literally `"FILL_IN"`.
3. Check required keys are non-empty:
   - Always: `AWS_REGION`, `ROLE_PREFIX`, `API_STAGE`, `API_BASE_URL`, `ALEXA_FULFILLMENT_REGION`
   - Alexa-specific: `LWA_SECRET_ARN`, `LWA_SECRET_REGION`, `ALEXA_SKILL_ID`, `ALEXA_REDIRECT_URI`, `ALEXA_REDIRECT_HOSTS`, `ALEXA_SESSIONS_TABLE`, `ALEXA_TOKENS_TABLE`, `USER_DEVICE_MAPPING_TABLE`, `USER_DEVICE_DETAILS_TABLE`, `METADATA_BUCKET`, `ENDPOINT_KEY_SECRET_ARN`

**Idempotency:** validation-only, no AWS calls.

---

### Step 2 — Preflight Checks

1. Call `sts.get_caller_identity()` → confirm credentials valid, capture `ACCOUNT_ID`.
2. Call `secretsmanager.describe_secret(SecretId=LWA_SECRET_ARN, region=LWA_SECRET_REGION)` → confirm LWA secret accessible.
3. Confirm `ALEXA_FULFILLMENT_REGION` is reachable: `sts.get_caller_identity()` using a regional endpoint for `ALEXA_FULFILLMENT_REGION`.

**Idempotency:** read-only AWS calls.

---

### Step 3 — DynamoDB Tables (2 tables)

For each table — **check before create:**
```
describe_table(TableName) → if ResourceNotFoundException → create
```

| Table | Partition Key | TTL attribute | Notes |
|-------|--------------|---------------|-------|
| `ALEXA_SESSIONS_TABLE` | `state` (S) | `ttl` | App-to-App linking sessions |
| `ALEXA_TOKENS_TABLE` | `userId` (S) | — | LWA tokens, KMS-encrypted |

Steps per table:
- If not exists → `create_table` (PAY_PER_REQUEST) → `wait_until_table_exists`
- Enable TTL on `ttl` attribute (only on sessions table)
- If already exists → print "already exists", skip

**Idempotency:** describe → skip if exists.

---

### Step 4 — KMS Key

1. `kms.list_aliases()` → search for alias `KMS_KEY_ALIAS` (e.g., `alias/digilux-alexa-tokens`)
2. If alias exists → `describe_key(KeyId=alias)` → capture `KMS_KEY_ARN`
3. If alias not found:
   - `kms.create_key(Description=...)` → capture `KMS_KEY_ID`
   - `kms.create_alias(AliasName=KMS_KEY_ALIAS, TargetKeyId=KMS_KEY_ID)`
   - `KMS_KEY_ARN = arn:aws:kms:{AWS_REGION}:{ACCOUNT_ID}:key/{KMS_KEY_ID}`

**Idempotency:** list before create. Never re-creates an existing key.

---

### Step 5 — API Gateway

1. Verify `API_GATEWAY_ID` reachable (or create if blank) — same logic as deploy_google.py.
2. Look up or create `cognito_authorizer` — same logic.
3. Capture `ROOT_ID`, get/create `api` → `v1` resource nodes.

**Idempotency:** get before create.

---

### Step 6 — IAM Roles (9 roles)

All roles: trust policy = `lambda.amazonaws.com` → `sts:AssumeRole`.
Strategy: `get_role` → if missing `create_role` + `sleep(10)`. Always `put_role_policy`.

**Important:** Roles 7 and 8 (state_reporter, proactive) and Role 9 (fulfillment) reference device/scene/S3 ARNs that are built from config values in `AWS_REGION` — this script defines them correctly in both Alexa-only and combined deployments.

#### Role: `{ROLE_PREFIX}-alexa-start-role`
Permissions:
- `dynamodb:PutItem`, `dynamodb:GetItem` on `ALEXA_SESSIONS_TABLE`
- `kms:GenerateDataKey`, `kms:Decrypt` on `KMS_KEY_ARN`
- CloudWatch Logs (`AWS_REGION`), X-Ray

#### Role: `{ROLE_PREFIX}-alexa-complete-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:UpdateItem` on `ALEXA_SESSIONS_TABLE`
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:UpdateItem` on `ALEXA_TOKENS_TABLE`
- `secretsmanager:GetSecretValue` on `LWA_SECRET_ARN`
- `kms:Decrypt`, `kms:GenerateDataKey` on `KMS_KEY_ARN`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-callback-role`
Permissions:
- CloudWatch Logs, X-Ray only

#### Role: `{ROLE_PREFIX}-alexa-unlink-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:DeleteItem`, `dynamodb:UpdateItem` on `ALEXA_TOKENS_TABLE`
- `secretsmanager:GetSecretValue` on `LWA_SECRET_ARN`
- `kms:Decrypt` on `KMS_KEY_ARN`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-status-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` on `ALEXA_TOKENS_TABLE`
- `secretsmanager:GetSecretValue` on `LWA_SECRET_ARN`
- `kms:Decrypt` on `KMS_KEY_ARN`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-skill-evt-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` on `ALEXA_TOKENS_TABLE`
- `dynamodb:Query`, `dynamodb:GetItem` on `USER_DEVICE_MAPPING_TABLE` table + `index/*`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-state-reporter-role`
Permissions:
- `dynamodb:GetItem` on `ALEXA_TOKENS_TABLE`
- `dynamodb:Query`, `dynamodb:GetItem` on `USER_DEVICE_DETAILS_TABLE` + `index/*`
- `s3:GetObject`, `s3:ListBucket` on `METADATA_BUCKET`
- `secretsmanager:GetSecretValue` on `ENDPOINT_KEY_SECRET_ARN`, `LWA_SECRET_ARN`
- `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:DescribeStream`, `dynamodb:ListStreams` on `DEVICE_STATE_TABLE/stream/*`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-proactive-role`
Permissions:
- `dynamodb:GetItem` on `ALEXA_TOKENS_TABLE`
- `dynamodb:Query`, `dynamodb:GetItem` on `USER_DEVICE_DETAILS_TABLE` + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `SCENE_TABLE` + `index/*`
- `s3:GetObject`, `s3:ListBucket` on `METADATA_BUCKET`
- `secretsmanager:GetSecretValue` on `ENDPOINT_KEY_SECRET_ARN`, `LWA_SECRET_ARN`
- `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:DescribeStream`, `dynamodb:ListStreams` on `USER_DEVICE_DETAILS_TABLE/stream/*`
- CloudWatch Logs, X-Ray

#### Role: `{ROLE_PREFIX}-alexa-fulfillment-role`
Permissions:
- `dynamodb:GetItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` on `ALEXA_TOKENS_TABLE`
  - Note: ARN uses `AWS_REGION` even though the Lambda lives in `ALEXA_FULFILLMENT_REGION`
- `dynamodb:Query`, `dynamodb:GetItem` on `USER_DEVICE_DETAILS_TABLE` + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `DEVICE_STATE_TABLE` + `index/*`
- `dynamodb:Query`, `dynamodb:GetItem` on `SCENE_TABLE` + `index/*`
- `s3:GetObject`, `s3:ListBucket` on `METADATA_BUCKET`
- `secretsmanager:GetSecretValue` on `LWA_SECRET_ARN`, `ENDPOINT_KEY_SECRET_ARN`, `DEVICE_TABLE_SECRET_ARN`
- `kms:Decrypt` on `KMS_KEY_ARN`
- `iot:Publish` on `arn:aws:iot:{AWS_REGION}:{ACCOUNT_ID}:topic/iot/device/*`
- CloudWatch Logs in **`ALEXA_FULFILLMENT_REGION`** (not `AWS_REGION`)
- X-Ray

**Idempotency:** `get_role` → if not exists `create_role`. `put_role_policy` always overwrites.

---

### Step 7 — Package Lambda Zips

Same packaging logic as deploy_google.py.
Build dir: `/tmp/deploy_build_{ENV_LABEL}/`

| Lambda | Source directory | Has requirements.txt? |
|--------|------------------|-----------------------|
| `alexa_app_to_app_link` | `lambdas/alexa_app_to_app_link/` | No |
| `alexa_complete_app_to_app` | `lambdas/alexa_complete_app_to_app/` | No |
| `alexa_server_callback` | `lambdas/alexa_server_callback/` | No |
| `alexa_app_to_app_unlink` | `lambdas/alexa_app_to_app_unlink/` | No |
| `alexa_link_status` | `lambdas/alexa_link_status/` | No |
| `alexa_skill_server_events` | `lambdas/alexa_skill_server_events/` | No |
| `alexa_state_reporter` | `lambdas/alexa_state_reporter/` | No |
| `alexa_proactive_discovery` | `lambdas/alexa_proactive_discovery/` | No |
| `alexa_fulfillment` | `lambdas/alexa_fulfillment/` | Yes |

---

### Step 8 — Deploy Lambdas

**8 Lambdas to `AWS_REGION`:**

| Lambda | Timeout | Memory | Env vars (key ones) |
|--------|---------|--------|---------------------|
| `alexa_app_to_app_link` | 15s | 256 MB | DATA_REGION, LOG_LEVEL, COGNITO_*, SESSION_TABLE, REDIRECT_URI, ALLOWED_REDIRECT_HOSTS, KMS_KEY_ARN, ALEXA_SESSION_TTL_SECONDS |
| `alexa_complete_app_to_app` | 30s | 256 MB | + SESSION_TABLE, LWA_TOKENS_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, KMS_KEY_ARN, ALEXA_SKILL_ID, ALEXA_SKILL_STAGE, REDIRECT_URI, USER_DEVICE_MAPPING_TABLE, CREATE_UPDATE_FN, LWA_TOKEN_URL, LWA_REVOKE_URL |
| `alexa_server_callback` | 10s | 256 MB | DATA_REGION, LOG_LEVEL, COGNITO_*, APP_SCHEME={APP_DEEP_LINK_SCHEME} |
| `alexa_app_to_app_unlink` | 15s | 256 MB | + LWA_TOKENS_TABLE, USER_DEVICE_MAPPING_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, KMS_KEY_ARN, ALEXA_SKILL_ID, CREATE_UPDATE_FN, LWA_REVOKE_URL, SKILL_ENABLEMENT_URL |
| `alexa_link_status` | 15s | 256 MB | + LWA_TOKENS_TABLE, USER_DEVICE_MAPPING_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, KMS_KEY_ARN |
| `alexa_skill_server_events` | 15s | **128 MB** | + LWA_TOKENS_TABLE, USER_DEVICE_MAPPING_TABLE, ALEXA_SKILL_ID |
| `alexa_state_reporter` | 30s | 256 MB | DATA_REGION, LOG_LEVEL, LWA_TOKENS_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, USER_DEVICE_DETAILS_TABLE, METADATA_BUCKET, ENDPOINT_KEY_SECRET_ARN, ENDPOINT_KEY_REGION, ALEXA_GATEWAY_URL |
| `alexa_proactive_discovery` | 30s | 256 MB | + LWA_TOKENS_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, USER_DEVICE_DETAILS_TABLE, SCENE_TABLE, METADATA_BUCKET, ENDPOINT_KEY_SECRET_ARN, ENDPOINT_KEY_REGION, ALEXA_GATEWAY_URL |

**1 Lambda to `ALEXA_FULFILLMENT_REGION` (eu-west-1):**

| Lambda | Timeout | Memory | Env vars |
|--------|---------|--------|----------|
| `alexa_fulfillment` | 30s | 256 MB | DATA_REGION={AWS_REGION}, LOG_LEVEL, LWA_TOKENS_TABLE, LWA_SECRET_ARN, LWA_SECRET_REGION, KMS_KEY_ARN, COGNITO_USER_POOL_ID, COGNITO_REGION, USER_DEVICE_DETAILS_TABLE, DEVICE_STATE_TABLE, SCENE_TABLE, METADATA_BUCKET, ENDPOINT_KEY_SECRET_ARN, ENDPOINT_KEY_REGION, DEVICE_TABLE_SECRET_ARN, DEVICE_TABLE_SECRET_REGION, ALEXA_GATEWAY_URL, IOT_DATA_ENDPOINT |

Note: `alexa_fulfillment` is deployed with `region=ALEXA_FULFILLMENT_REGION` but its `DATA_REGION` env var points to `AWS_REGION` (where DDB tables live).

**Idempotency per Lambda:**
```
lambda.get_function(FunctionName, region) → exists → update_code + wait + update_config + wait
                                          → not exists → create_function + wait
```

---

### Step 9 — API Gateway Resource Tree + Method Wiring

Note: `alexa_fulfillment` is **NOT** wired to API GW — Alexa calls it directly via its Smart Home trigger.

**Resource tree:**
```
/ (ROOT)
├── api/
│   └── v1/
│       └── alexa/
│           ├── status/            ← GET (link_status)
│           ├── startAppToApp/     ← POST (app_to_app_link)
│           ├── completeAppToApp/  ← POST (complete_app_to_app)
│           └── unlink/            ← DELETE (app_to_app_unlink)
└── alexa/
    ├── callback/                  ← GET (server_callback)
    └── skill-event/               ← POST (skill_server_events)
```

| Route | Method | Lambda | Auth |
|-------|--------|--------|------|
| `/api/v1/alexa/status` | GET | `alexa_link_status` | COGNITO_USER_POOLS |
| `/api/v1/alexa/startAppToApp` | POST | `alexa_app_to_app_link` | COGNITO_USER_POOLS |
| `/api/v1/alexa/completeAppToApp` | POST | `alexa_complete_app_to_app` | COGNITO_USER_POOLS |
| `/api/v1/alexa/unlink` | DELETE | `alexa_app_to_app_unlink` | COGNITO_USER_POOLS |
| `/alexa/callback` | GET | `alexa_server_callback` | NONE |
| `/alexa/skill-event` | POST | `alexa_skill_server_events` | NONE |

Method wiring: same idempotent put_method + put_integration + CORS pattern as deploy_google.py.

---

### Step 10 — Deploy API Gateway Stage

`apigateway.create_deployment(restApiId, stageName=API_STAGE)` — creates new deployment.

---

### Step 11 — Smoke Tests

| Test | Method | URL | Expected HTTP |
|------|--------|-----|---------------|
| Alexa start — no auth | POST | `{API_BASE_URL}/api/v1/alexa/startAppToApp` | 401 |
| Alexa complete — no auth | POST | `{API_BASE_URL}/api/v1/alexa/completeAppToApp` | 401 |
| Alexa status — no auth | GET | `{API_BASE_URL}/api/v1/alexa/status` | 401 |
| Alexa unlink — no auth | DELETE | `{API_BASE_URL}/api/v1/alexa/unlink` | 401 |
| Alexa callback — reachable | GET | `{API_BASE_URL}/alexa/callback` | 200 |

---

### Step 12 — Manual Post-Deploy Steps (printed to stdout)

```
1. DynamoDB stream triggers (cannot be automated):
   - Enable stream on {DEVICE_STATE_TABLE} → create event source mapping for alexa_state_reporter
   - Enable stream on {USER_DEVICE_DETAILS_TABLE} → create event source mapping for alexa_proactive_discovery
   (exact CLI commands printed)

2. Alexa fulfillment Smart Home trigger (cannot be automated):
   aws lambda add-permission \
     --function-name alexa_fulfillment \
     --statement-id alexa-smart-home \
     --action lambda:InvokeFunction \
     --principal alexa-connectedhome.amazon.com \
     --event-source-token {ALEXA_SKILL_ID} \
     --region {ALEXA_FULFILLMENT_REGION}

3. Register Lambda ARN in Alexa Developer Console:
   arn:aws:lambda:{ALEXA_FULFILLMENT_REGION}:{ACCOUNT_ID}:function:alexa_fulfillment
```

---

## What Gets Created / Updated (Summary)

| Resource type | Count | Notes |
|---|---|---|
| DynamoDB tables | 2 | sessions, tokens |
| KMS key + alias | 1 | `alias/{ROLE_PREFIX}-alexa-tokens` |
| IAM roles | 9 | start, complete, callback, unlink, status, skill-evt, state-reporter, proactive, fulfillment |
| Lambda functions in `AWS_REGION` | 8 | all Alexa account-linking Lambdas |
| Lambda functions in `ALEXA_FULFILLMENT_REGION` | 1 | `alexa_fulfillment` in eu-west-1 |
| API GW resources | ~10 | `/api/v1/alexa/…` + `/alexa/callback` + `/alexa/skill-event` |
| API GW methods | 6 routes + OPTIONS | — |
| API GW deployment | 1 | stage: `API_STAGE` |
| CloudWatch log retention | 9 | 30 days (8 in `AWS_REGION`, 1 in `ALEXA_FULFILLMENT_REGION`) |

---

## Rollback Considerations

- **Lambdas**: previous code still in Lambda version history — can roll back manually
- **DDB tables**: never deleted or modified — fully additive
- **KMS key**: never deleted — safe to re-run
- **IAM roles**: `put_role_policy` overwrites existing policy
- **This script never deletes anything**
