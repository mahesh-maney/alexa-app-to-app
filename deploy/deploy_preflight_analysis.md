# deploy_preflight.py — Analysis

Interactive Python script that connects to the Honeywell QA AWS account,
discovers all existing resources, reads secret values, and produces a
partially or fully filled `env.honeywell.qa.discovered.json`.

You run this script, share the output JSON with the developer, who then
substitutes values into `env.honeywell.prod.json` before running the
deploy scripts.

**Does NOT create or modify any AWS resource — purely read-only.**

---

## Usage

```bash
python3 deploy/deploy_preflight.py
python3 deploy/deploy_preflight.py --profile honeywell-qa
python3 deploy/deploy_preflight.py --profile honeywell-qa --region eu-west-1
```

**Flags (all optional):**
- `--profile <name>` — AWS profile from `~/.aws/credentials` (default: `default`)
- `--region <region>` — primary AWS region (default: prompted interactively)
- `--output <file>` — output JSON path (default: `deploy/env.honeywell.qa.discovered.json`)

---

## Prerequisites

- Python 3.9+, `boto3` installed (`pip install boto3`)
- AWS credentials configured with **read access** to:
  - `sts:GetCallerIdentity`
  - `cognito-idp:ListUserPools`, `ListUserPoolClients`, `DescribeUserPool`
  - `dynamodb:ListTables`, `DescribeTable`
  - `secretsmanager:ListSecrets`, `GetSecretValue`
  - `kms:ListAliases`, `DescribeKey`
  - `s3:ListBuckets`
  - `iot:DescribeEndpoint`
  - `lambda:ListFunctions`, `GetFunctionConfiguration`
  - `apigateway:GetRestApis`, `GetBasePathMappings`, `GetResources`

---

## Complete Interactive Flow

### Phase 0 — Banner + Region Setup

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Digilux / Honeywell — Deployment Preflight Discovery
  This script is READ-ONLY. No AWS resources will be created.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AWS Profile [default]:  > honeywell-qa
Primary AWS Region [ap-south-1]:  > eu-west-1

[INFO] Connecting to AWS (profile=honeywell-qa, region=eu-west-1)...
[OK]   Connected — Account: 123456789012  Region: eu-west-1
```

If credentials fail:
```
[ERROR] AWS credentials failed. Check profile 'honeywell-qa' in ~/.aws/credentials
```

---

### Phase 1 — Existing Lambda Scan (Smart Shortcut)

Scans for known Lambda function names in **both** the primary region and `eu-west-1`.
If any of our Lambdas are already deployed (from a previous version), their env vars
are extracted directly — this can pre-fill most config values automatically.

**Primary region scan** — calls `lambda.get_function_configuration(FunctionName)` for:
```
google_home_start_link          google_home_app_link_complete
google_home_link_status         google_home_app_unlink
google_home_oauth_authorize     google_home_oauth_cognito_callback
google_home_oauth_token         google_home_fulfillment
google_home_proactive           alexa_app_to_app_link
alexa_complete_app_to_app       alexa_server_callback
alexa_app_to_app_unlink         alexa_link_status
alexa_skill_server_events       alexa_state_reporter
alexa_proactive_discovery
```

**eu-west-1 scan** (always, regardless of primary region):
```
alexa_fulfillment
```

Console output:
```
━━━ Phase 1 — Existing Lambda Scan ━━━

[INFO] Scanning for existing Lambdas in eu-west-1...
[FOUND] google_home_start_link — extracting env vars...
[FOUND] alexa_fulfillment (eu-west-1) — extracting env vars...
[MISS]  google_home_fulfillment — not deployed yet
...

[INFO] Lambda scan complete. Pre-filled 23 values from existing Lambdas.
```

All extracted env vars are stored as candidates.
If no Lambdas are found (fresh account): "No existing Lambdas found — will discover from AWS resources."

---

### Phase 2 — Cognito Discovery

**AWS call:** `cognito_idp.list_user_pools(MaxResults=60, region=primary_region)`

If exactly 1 pool → auto-selected, user confirms:
```
━━━ Phase 2 — Cognito User Pool ━━━

[FOUND] 1 Cognito User Pool:
  [1] eu-west-1_ABC12345  —  Name: HoneywellUsers  —  Created: 2025-01-15
      Status: Active   MFA: OFF

Use this pool? [Y/n]:  > Y
[OK]   COGNITO_USER_POOL_ID = eu-west-1_ABC12345
```

If multiple pools → numbered list, user picks:
```
[FOUND] 3 Cognito User Pools:
  [1] eu-west-1_ABC12345  —  HoneywellUsers
  [2] eu-west-1_DEF67890  —  HoneywellDevPool
  [3] eu-west-1_GHI11111  —  LegacyPool

Enter number (or 0 to skip):  > 1
[OK]   COGNITO_USER_POOL_ID = eu-west-1_ABC12345
```

**Next:** `cognito_idp.list_user_pool_clients(UserPoolId, MaxResults=60)`

If multiple clients → numbered list:
```
[FOUND] 2 App Clients for eu-west-1_ABC12345:
  [1] abc123def456  —  Name: HoneywellAppClient        —  OAuth: Yes
  [2] xyz789uvw012  —  Name: HoneywellServerClient     —  OAuth: No

Enter number (the client used by the mobile app):  > 1
[OK]   COGNITO_CLIENT_ID = abc123def456
```

**Next:** `cognito_idp.describe_user_pool(UserPoolId)` → extract `Domain`:
```
[OK]   COGNITO_HOSTED_UI_DOMAIN = auth.honeywell.example.com
       (if empty: "No custom domain configured — enter it manually or set up in Cognito first")
```

Values set: `COGNITO_USER_POOL_ID`, `COGNITO_REGION`, `COGNITO_CLIENT_ID`, `COGNITO_HOSTED_UI_DOMAIN`

---

### Phase 3 — DynamoDB Table Discovery

**AWS call:** `dynamodb.list_tables(region=primary_region)` → retrieve all table names.

Display all tables and ask user to assign each required table name:
```
━━━ Phase 3 — DynamoDB Tables ━━━

[INFO] Found 18 DynamoDB tables in eu-west-1:
  alexa_app_linking_sessions          device_state
  honeywell_alexa_lwa_tokens          user_device_details
  google_home_link_sessions           honeywell_scene_data
  google_home_auth_codes              honeywell_user_device_mapping
  google_home_tokens                  product_catalog
  google_home_oauth_transactions      firmware_versions
  ... (all listed)

Assign tables to required keys.
Press Enter to accept a suggestion, or type the exact table name, or 'skip'.

ALEXA_SESSIONS_TABLE
  Suggestion: alexa_app_linking_sessions  (matched by name)
  > [Enter]
  [OK] ALEXA_SESSIONS_TABLE = alexa_app_linking_sessions

ALEXA_TOKENS_TABLE
  Suggestion: honeywell_alexa_lwa_tokens  (matched by name)
  > [Enter]
  [OK] ALEXA_TOKENS_TABLE = honeywell_alexa_lwa_tokens

GH_SESSIONS_TABLE
  Suggestion: google_home_link_sessions  (matched by name)
  > [Enter]
  [OK] GH_SESSIONS_TABLE = google_home_link_sessions

GH_AUTH_CODES_TABLE
  Suggestion: google_home_auth_codes  (matched by name)
  > [Enter]

GH_TOKENS_TABLE
  Suggestion: google_home_tokens  (matched by name)
  > [Enter]

GH_OAUTH_TRANSACTIONS_TABLE
  Suggestion: google_home_oauth_transactions  (matched by name)
  > [Enter]

USER_DEVICE_DETAILS_TABLE
  Suggestion: user_device_details  (matched by name)
  > [Enter]

DEVICE_STATE_TABLE
  Suggestion: device_state  (matched by name)
  > [Enter]

SCENE_TABLE
  No suggestion — tables with 'scene' in name: honeywell_scene_data
  > honeywell_scene_data
  [OK] SCENE_TABLE = honeywell_scene_data

USER_DEVICE_MAPPING_TABLE
  Suggestion: honeywell_user_device_mapping  (matched by name)
  > [Enter]
```

**Suggestion logic:** fuzzy match against well-known name patterns
(`alexa_app_linking`, `lwa_tokens`, `google_home_link`, `google_home_auth`, etc.).
Always shows the full list so the user can type any name if the suggestion is wrong.

**GSI Discovery:** after SCENE_TABLE is confirmed:
```
[INFO] Reading GSIs on honeywell_scene_data...
[FOUND] GSIs: GSI_scene_data  (PK: userId)
SCENE_GSI_NAME
  Suggestion: GSI_scene_data
  > [Enter]
  [OK] SCENE_GSI_NAME = GSI_scene_data
```

---

### Phase 4 — Secrets Manager Discovery

Scans **both** the primary region and `eu-west-1` for secrets.

**AWS calls:**
- `secretsmanager.list_secrets(region=primary_region)` — paginated
- `secretsmanager.list_secrets(region=eu-west-1)` — paginated (always, even if primary IS eu-west-1)

Displays all secrets across both regions and asks user to assign:
```
━━━ Phase 4 — Secrets Manager ━━━

[INFO] Scanning secrets in eu-west-1...         found 8 secrets
[INFO] Scanning secrets in ap-south-1...        found 3 secrets

All secrets found (both regions):
  [eu-west-1]
  [1]  honeywell/alexa/lwa                  arn:aws:secretsmanager:eu-west-1:…
  [2]  honeywell/alexa/endpoint_key          arn:aws:secretsmanager:eu-west-1:…
  [3]  honeywell/google_home/service_account arn:aws:secretsmanager:eu-west-1:…
  [4]  some-other-secret                     arn:aws:secretsmanager:eu-west-1:…
  ...
  [ap-south-1]
  [9]  honeywell-google-home-oauth            arn:aws:secretsmanager:ap-south-1:…
  [10] encrypt_data_secret                    arn:aws:secretsmanager:ap-south-1:…
  ...

Assign secrets to required keys.

LWA_SECRET_ARN  (Alexa Login-with-Amazon credentials)
  Suggestion: [1] honeywell/alexa/lwa  (eu-west-1)
  > [Enter]
  [OK] LWA_SECRET_ARN    = arn:aws:secretsmanager:eu-west-1:…:honeywell/alexa/lwa-XXXXXX
  [OK] LWA_SECRET_REGION = eu-west-1  (parsed from ARN)

GOOGLE_SECRET_ARN  (Google OAuth client_id + client_secret)
  Suggestion: [9] honeywell-google-home-oauth  (ap-south-1)
  > [Enter]
  [OK] GOOGLE_SECRET_ARN    = arn:aws:secretsmanager:ap-south-1:…
  [OK] GOOGLE_SECRET_REGION = ap-south-1  (parsed from ARN)

ENDPOINT_KEY_SECRET_ARN  (Alexa/GH endpoint encryption key)
  Suggestion: [2] honeywell/alexa/endpoint_key  (eu-west-1)
  > [Enter]
  [OK] ENDPOINT_KEY_SECRET_ARN    = arn:aws:secretsmanager:eu-west-1:…
  [OK] ENDPOINT_KEY_REGION        = eu-west-1  (parsed from ARN)

GH_SA_SECRET_ARN  (Google Home service account key for proactive reports)
  Suggestion: [3] honeywell/google_home/service_account  (eu-west-1)
  > [Enter]
  [OK] GH_SA_SECRET_ARN    = arn:aws:secretsmanager:eu-west-1:…
  [OK] GH_SA_SECRET_REGION = eu-west-1  (parsed from ARN)

DEVICE_TABLE_SECRET_ARN  (device table encryption key)
  Suggestion: [10] encrypt_data_secret  (ap-south-1)
  > [Enter]
  [OK] DEVICE_TABLE_SECRET_ARN    = arn:aws:secretsmanager:ap-south-1:…
  [OK] DEVICE_TABLE_SECRET_REGION = ap-south-1  (parsed from ARN)
```

**All region values are parsed directly from the ARN** — no manual entry needed for `*_REGION` vars.

---

### Phase 5 — Read Secret Values

For secrets assigned above, the script offers to read their contents to extract
embedded config values (`client_id`, `client_secret`, etc.).

```
━━━ Phase 5 — Read Secret Values ━━━

[WARN] This phase reads the actual secret values.
       They will appear in your terminal and in the output JSON.
       Make sure your terminal session is private.

Read secret values to extract config? [Y/n]:  > Y

Reading: GOOGLE_SECRET_ARN (honeywell-google-home-oauth, ap-south-1)...
  Secret is JSON. Found keys: client_id, client_secret, project_id
  [OK] GOOGLE_CLIENT_ID = 9abc8def7ghi6jkl5mno  (from secret key 'client_id')
  [NOTE] client_secret is present — it will NOT be written to the JSON output
         (it lives in Secrets Manager, not in env vars)

Reading: LWA_SECRET_ARN (honeywell/alexa/lwa, eu-west-1)...
  Secret is JSON. Found keys: client_id, client_secret
  [OK] LWA credentials accessible — no env var extracted (used at runtime by Lambda)

Reading: ENDPOINT_KEY_SECRET_ARN...
  [OK] Endpoint key secret accessible — no env var extracted (binary key, used at runtime)
```

**What gets extracted vs what stays private:**
| Secret | Value extracted to JSON | Value kept in Secrets Manager only |
|--------|------------------------|------------------------------------|
| Google OAuth secret | `GOOGLE_CLIENT_ID` | `client_secret` (never in env vars) |
| LWA secret | Nothing | `client_id`, `client_secret` |
| Endpoint key | Nothing | Key bytes |
| GH SA key | Nothing | JSON key file |
| Device table key | Nothing | Encryption key |

---

### Phase 6 — KMS Key Discovery

**AWS call:** `kms.list_aliases(region=primary_region)`

```
━━━ Phase 6 — KMS Key ━━━

[INFO] Scanning KMS aliases in eu-west-1...
[FOUND] Aliases with 'alexa' or 'honeywell' in name:
  [1] alias/honeywell-alexa-tokens    →  KeyId: abc123
  [2] alias/honeywell-kms-master      →  KeyId: def456

KMS_KEY_ALIAS  (used to encrypt/decrypt LWA tokens)
  Suggestion: [1] alias/honeywell-alexa-tokens
  > [Enter]
  [OK] KMS_KEY_ALIAS = alias/honeywell-alexa-tokens
```

If no matching alias found:
```
  No relevant KMS aliases found.
  This alias will be created by deploy_alexa.py.
  Enter desired alias name [alias/honeywell-alexa-tokens]:  > [Enter]
  [OK] KMS_KEY_ALIAS = alias/honeywell-alexa-tokens  (will be created on deploy)
```

---

### Phase 7 — S3 Bucket Discovery

**AWS call:** `s3.list_buckets()` (global, returns all regions)

```
━━━ Phase 7 — S3 Metadata Bucket ━━━

[FOUND] Buckets containing 'metadata' or 'honeywell' or 'digilux':
  [1] honeywell-metadata-bucket         (eu-west-1)
  [2] honeywell-firmware-assets         (eu-west-1)
  [3] honeywell-device-configs          (ap-south-1)

METADATA_BUCKET  (device metadata JSON files for SYNC/QUERY responses)
  Suggestion: [1] honeywell-metadata-bucket
  > [Enter]
  [OK] METADATA_BUCKET = honeywell-metadata-bucket
```

---

### Phase 8 — IoT Endpoint Discovery

**AWS call:** `iot.describe_endpoint(endpointType='iot:Data-ATS', region=primary_region)`

No user input needed — fully automatic:
```
━━━ Phase 8 — IoT Data Endpoint ━━━

[INFO] Fetching IoT ATS endpoint for eu-west-1...
[OK]   IOT_DATA_ENDPOINT = https://abcdef1234567-ats.iot.eu-west-1.amazonaws.com
```

---

### Phase 9 — API Gateway Discovery

**AWS call:** `apigateway.get_rest_apis(region=primary_region)`

```
━━━ Phase 9 — API Gateway ━━━

[FOUND] 2 REST APIs in eu-west-1:
  [1] abc12345  —  Name: honeywell-smarthome-api   —  Created: 2025-03-10
  [2] xyz98765  —  Name: honeywell-legacy-api       —  Created: 2023-11-01

API_GATEWAY_ID:
  Suggestion: [1] abc12345  (honeywell-smarthome-api)
  > [Enter]
  [OK] API_GATEWAY_ID = abc12345
```

**Custom domain + base URL:**
```
[INFO] Checking custom domain mappings for API abc12345...
[FOUND] Custom domain: api.honeywell-smarthome.com  →  stage: smarthome
[OK]   API_BASE_URL = https://api.honeywell-smarthome.com/smarthome
[OK]   API_STAGE    = smarthome

Confirm API_BASE_URL [https://api.honeywell-smarthome.com/smarthome]:  > [Enter]
```

If no custom domain found:
```
  No custom domain mapping found.
  Constructed URL: https://abc12345.execute-api.eu-west-1.amazonaws.com/smarthome
  Enter API_BASE_URL:  > https://api.honeywell-smarthome.com/smarthome
```

---

### Phase 10 — Lambda Function Discovery

**AWS call:** `lambda.list_functions(region=primary_region)` — paginated

```
━━━ Phase 10 — Lambda Functions ━━━

[INFO] Scanning Lambda functions in eu-west-1 for CREATE_UPDATE_FN...
[FOUND] Candidates (functions with 'create' or 'update' or 'data' in name):
  [1] create_and_update_data_function
  [2] honeywell_device_data_sync
  [3] update_device_data_handler

CREATE_UPDATE_FN  (Lambda that syncs device data after Alexa linking):
  Suggestion: [1] create_and_update_data_function
  > [Enter]
  [OK] CREATE_UPDATE_FN = create_and_update_data_function
```

---

### Phase 11 — eu-west-1 Alexa Fulfillment Region Confirmation

```
━━━ Phase 11 — Alexa Fulfillment Region ━━━

[INFO] Checking for alexa_fulfillment Lambda in eu-west-1...
[FOUND] alexa_fulfillment exists in eu-west-1

ALEXA_FULFILLMENT_REGION [eu-west-1]:  > [Enter]
[OK]   ALEXA_FULFILLMENT_REGION = eu-west-1
```

If not found:
```
  alexa_fulfillment not found in eu-west-1.
  For Indian customers, Alexa routes to eu-west-1 (Ireland).
  ALEXA_FULFILLMENT_REGION [eu-west-1]:  > [Enter]
  [OK]   ALEXA_FULFILLMENT_REGION = eu-west-1  (will be deployed there)
```

---

### Phase 12 — Compute Derived Values (automatic, no input)

```
━━━ Phase 12 — Derived Values ━━━

[OK] COGNITO_REGION          = eu-west-1  (same as AWS_REGION)
[OK] ALEXA_REDIRECT_URI      = https://api.honeywell-smarthome.com/smarthome/alexa/callback
                               (derived from API_BASE_URL)
[OK] ALEXA_REDIRECT_HOSTS    = api.honeywell-smarthome.com
                               (hostname from ALEXA_REDIRECT_URI)
[NOTE] GOOGLE_REDIRECT_URI   = https://oauth-redirect.googleusercontent.com/r/{GOOGLE_AGENT_ID}
                               (will be completed after GOOGLE_AGENT_ID is entered)
[NOTE] GOOGLE_AGENT_ID must be entered manually in Phase 13
```

Values set: `COGNITO_REGION`, `ALEXA_REDIRECT_URI`, `ALEXA_REDIRECT_HOSTS`.
`GOOGLE_REDIRECT_URI` computed automatically once `GOOGLE_AGENT_ID` is provided.

---

### Phase 13 — Manual Values (Cannot Be Discovered)

```
━━━ Phase 13 — Manual Values ━━━

The following values cannot be discovered from AWS.
Enter each value, or press Enter to leave as FILL_IN.

ALEXA_SKILL_ID  (from Alexa Developer Console → Skills → Your Skill → Skill ID)
  > amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

ALEXA_SKILL_STAGE  [development / live]  default=development
  > [Enter]
  [OK] ALEXA_SKILL_STAGE = development

GOOGLE_AGENT_ID  (from Google Home Developer Console — your project/agent ID)
  > honeywell-smarthome
  [OK] GOOGLE_AGENT_ID = honeywell-smarthome
  [OK] GOOGLE_REDIRECT_URI = https://oauth-redirect.googleusercontent.com/r/honeywell-smarthome
       (computed automatically)

APP_DEEP_LINK_SCHEME  (your Android app's URI scheme, e.g. 'digilux', 'honeywell')
  > honeywell

APP_NAME  (display name shown in consent page, e.g. 'Honeywell Smart Home')
  > Honeywell Smart Home

DEVICE_MANUFACTURER  (shown in Google Home device list)
  > Honeywell

DEVICE_MODEL  (shown in Google Home device list)
  > Honeywell Smart Device

BRAND_TITLE  (consent page header — company name)
  > Honeywell Home

BRAND_SUBTITLE  (consent page sub-header)  [default: Identity Server]
  > [Enter]
  [OK] BRAND_SUBTITLE = Identity Server

TERMS_URL  (link shown on consent page)
  > https://honeywell.com/terms

PRIVACY_POLICY_URL  (link shown on consent page)
  > https://honeywell.com/privacy

Consent page text — press Enter to keep defaults.
CONSENT_PAGE_HEADING       [Authorization]:         > [Enter]
CONSENT_NOTICE_TEXT        [By clicking Allow...]:  > [Enter]
CONSENT_PERM_LABEL         [Granting permission to:]: > [Enter]
CONSENT_PERMISSIONS        [discoverdevices,controldevices]: > [Enter]
CONSENT_TRUST_TEXT         [Make sure you trust Google]: > [Enter]
CONSENT_DENY_LABEL         [DENY]:  > [Enter]
CONSENT_ALLOW_LABEL        [ALLOW]: > [Enter]
```

---

### Phase 14 — Fixed/Well-Known Values (set automatically)

These values are constants shared across all deployments and do not need input:

```
[OK] LWA_TOKEN_URL        = https://api.amazon.com/auth/o2/token
[OK] LWA_REVOKE_URL       = https://api.amazon.com/auth/o2/revoke
[OK] SKILL_ENABLEMENT_URL = https://api.amazonalexa.com/v1/users/~current/skills
[OK] ALEXA_GATEWAY_URL    = https://api.eu.amazonalexa.com/v3/events
[OK] GH_DEEP_LINK_BASE_URL = https://home.google.com/home-app/
[OK] GH_DEEP_LINK_PATH    = setup/ha_linking
[OK] LOG_LEVEL            = INFO
[OK] SESSION_TTL_SECONDS          = 1800
[OK] AUTH_CODE_TTL_SECONDS        = 900
[OK] ACCESS_TOKEN_TTL_SECONDS     = 3600
[OK] REFRESH_TOKEN_TTL_SECONDS    = 15552000
[OK] TRANSACTION_TTL_SECONDS      = 600
[OK] ALEXA_SESSION_TTL_SECONDS    = 600
[OK] GH_TOKENS_ACCESS_TOKEN_INDEX = accessToken-index
[OK] GH_SESSIONS_TABLE            = google_home_link_sessions  (if not already assigned)
[OK] GH_AUTH_CODES_TABLE          = google_home_auth_codes
[OK] GH_TOKENS_TABLE              = google_home_tokens
[OK] GH_OAUTH_TRANSACTIONS_TABLE  = google_home_oauth_transactions
[OK] ALEXA_SESSIONS_TABLE         = alexa_app_linking_sessions
[OK] DEPLOY_TARGET                = all
[OK] ENV_LABEL                    = honeywell-qa
[OK] ROLE_PREFIX                  = honeywell
```

User may override any of these if prompted:
```
ROLE_PREFIX [honeywell]:  > [Enter]
ENV_LABEL   [honeywell-qa]:  > [Enter]
```

---

### Phase 15 — Generate Output

```
━━━ Phase 15 — Output ━━━

[INFO] Writing: deploy/env.honeywell.qa.discovered.json
[OK]   File written successfully

━━━ DISCOVERY SUMMARY ━━━

KEY                              STATUS        SOURCE
───────────────────────────────────────────────────────────────────────────
AWS_REGION                       DISCOVERED    user input
ACCOUNT_ID                       DISCOVERED    sts:GetCallerIdentity
COGNITO_USER_POOL_ID             DISCOVERED    list_user_pools
COGNITO_CLIENT_ID                DISCOVERED    list_user_pool_clients
COGNITO_HOSTED_UI_DOMAIN         DISCOVERED    describe_user_pool
ALEXA_SESSIONS_TABLE             DISCOVERED    dynamodb:ListTables
ALEXA_TOKENS_TABLE               DISCOVERED    dynamodb:ListTables
GH_SESSIONS_TABLE                DISCOVERED    dynamodb:ListTables
GH_AUTH_CODES_TABLE              DISCOVERED    dynamodb:ListTables
GH_TOKENS_TABLE                  DISCOVERED    dynamodb:ListTables
GH_OAUTH_TRANSACTIONS_TABLE      DISCOVERED    dynamodb:ListTables
USER_DEVICE_DETAILS_TABLE        DISCOVERED    dynamodb:ListTables
DEVICE_STATE_TABLE               DISCOVERED    dynamodb:ListTables
SCENE_TABLE                      DISCOVERED    dynamodb:ListTables
SCENE_GSI_NAME                   DISCOVERED    dynamodb:DescribeTable
USER_DEVICE_MAPPING_TABLE        DISCOVERED    dynamodb:ListTables
LWA_SECRET_ARN                   DISCOVERED    secretsmanager:ListSecrets
LWA_SECRET_REGION                DISCOVERED    parsed from ARN
GOOGLE_SECRET_ARN                DISCOVERED    secretsmanager:ListSecrets
GOOGLE_SECRET_REGION             DISCOVERED    parsed from ARN
GOOGLE_CLIENT_ID                 DISCOVERED    secretsmanager:GetSecretValue
ENDPOINT_KEY_SECRET_ARN          DISCOVERED    secretsmanager:ListSecrets
ENDPOINT_KEY_REGION              DISCOVERED    parsed from ARN
GH_SA_SECRET_ARN                 DISCOVERED    secretsmanager:ListSecrets
GH_SA_SECRET_REGION              DISCOVERED    parsed from ARN
DEVICE_TABLE_SECRET_ARN          DISCOVERED    secretsmanager:ListSecrets
DEVICE_TABLE_SECRET_REGION       DISCOVERED    parsed from ARN
KMS_KEY_ALIAS                    DISCOVERED    kms:ListAliases
METADATA_BUCKET                  DISCOVERED    s3:ListBuckets
IOT_DATA_ENDPOINT                DISCOVERED    iot:DescribeEndpoint
API_GATEWAY_ID                   DISCOVERED    apigateway:GetRestApis
API_BASE_URL                     DISCOVERED    apigateway:GetBasePathMappings
API_STAGE                        DISCOVERED    apigateway:GetRestApis
COGNITO_REGION                   DERIVED       same as AWS_REGION
ALEXA_REDIRECT_URI               DERIVED       from API_BASE_URL
ALEXA_REDIRECT_HOSTS             DERIVED       from ALEXA_REDIRECT_URI
ALEXA_FULFILLMENT_REGION         DISCOVERED    lambda:GetFunction (eu-west-1)
CREATE_UPDATE_FN                 DISCOVERED    lambda:ListFunctions
ALEXA_SKILL_ID                   MANUAL        user input
ALEXA_SKILL_STAGE                DEFAULT       development
GOOGLE_AGENT_ID                  MANUAL        user input
GOOGLE_REDIRECT_URI              DERIVED       from GOOGLE_AGENT_ID
APP_DEEP_LINK_SCHEME             MANUAL        user input
APP_NAME                         MANUAL        user input
DEVICE_MANUFACTURER              MANUAL        user input
DEVICE_MODEL                     MANUAL        user input
BRAND_TITLE                      MANUAL        user input
BRAND_SUBTITLE                   DEFAULT       Identity Server
TERMS_URL                        MANUAL        user input
PRIVACY_POLICY_URL               MANUAL        user input
CONSENT_PAGE_HEADING             DEFAULT       Authorization
(remaining consent vars)         DEFAULT       standard values
LWA_TOKEN_URL                    CONSTANT      well-known URL
...
───────────────────────────────────────────────────────────────────────────
DISCOVERED: 35    DERIVED: 4    MANUAL: 8    DEFAULT: 10    MISSING: 0

All values resolved. Output is ready.
Share deploy/env.honeywell.qa.discovered.json with your developer.
```

If any values are still `FILL_IN`:
```
MISSING: 2
  ALEXA_SKILL_ID       — get from Alexa Developer Console
  GOOGLE_AGENT_ID      — get from Google Home Developer Console
These will appear as FILL_IN in the output JSON.
The deploy scripts will refuse to run until they are filled in.
```

---

## Output File: `env.honeywell.qa.discovered.json`

Same format as `env.digilux.qa.json` — a flat JSON object with all keys.
Any undiscovered value is `"FILL_IN"`.
Any default value is pre-filled (e.g., `"LOG_LEVEL": "INFO"`).

```json
{
  "_description": "Honeywell QA — generated by deploy_preflight.py on 2026-10-22",
  "_deploy_target": "all",
  "DEPLOY_TARGET": "all",
  "ROLE_PREFIX": "honeywell",
  "ENV_LABEL": "honeywell-qa",
  "AWS_REGION": "eu-west-1",
  "ALEXA_FULFILLMENT_REGION": "eu-west-1",
  "API_GATEWAY_ID": "abc12345",
  "API_STAGE": "smarthome",
  "API_BASE_URL": "https://api.honeywell-smarthome.com/smarthome",
  "COGNITO_USER_POOL_ID": "eu-west-1_ABC12345",
  "COGNITO_REGION": "eu-west-1",
  "COGNITO_CLIENT_ID": "abc123def456",
  "COGNITO_HOSTED_UI_DOMAIN": "auth.honeywell-smarthome.com",
  "ALEXA_SESSIONS_TABLE": "alexa_app_linking_sessions",
  "ALEXA_TOKENS_TABLE": "honeywell_alexa_lwa_tokens",
  ...
  "ALEXA_SKILL_ID": "FILL_IN",
  "GOOGLE_AGENT_ID": "FILL_IN",
  ...
}
```

---

## Logging During Execution

Same format as the deploy scripts:
```
[HH:MM:SS] [LEVEL]  [PHASE X — NAME]  message
```

| Level | Meaning |
|-------|---------|
| `INFO` | Operation in progress |
| `OK` | Value successfully discovered or confirmed |
| `FOUND` | Resource(s) found in AWS |
| `MISS` | Resource not found (non-fatal for Lambda scan) |
| `DERIVED` | Value computed from another value |
| `DEFAULT` | Standard constant applied |
| `MANUAL` | Value entered by user |
| `WARN` | Secret values visible in terminal |
| `ERROR` | Fatal — AWS call failed, exits |

---

## What This Script Does NOT Do

- Does not create any AWS resource
- Does not modify any existing resource
- Does not write any file other than the output JSON
- Does not store or transmit secret values anywhere other than the output JSON
- Does not call the Google or Alexa APIs — only AWS APIs
