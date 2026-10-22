# deploy.sh — Usage Guide

Complete guide for deploying the Alexa and Google Home voice integrations
to any AWS account using the config-driven deploy script.

---

## 1. Dependencies

Install all of the following before running the script. The script checks
for each one at startup and exits immediately if any are missing.

| Tool | Minimum version | Install |
|---|---|---|
| `aws` CLI | v2.x | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html |
| `jq` | 1.6+ | `brew install jq` / `apt install jq` |
| `python3` | 3.9+ | `brew install python3` / system python3 |
| `zip` | any | pre-installed on macOS; `apt install zip` on Linux |
| `curl` | any | pre-installed on macOS/Linux |

Quick check — run this to confirm all are available:
```bash
for t in aws jq python3 zip curl; do
  command -v "$t" && echo "$t OK" || echo "MISSING: $t"
done
```

---

## 2. AWS Credentials

The script uses the standard AWS CLI credential chain. Set up credentials
**for the target AWS account** using one of these methods:

### Option A — Named profile (recommended for multi-account)
```bash
aws configure --profile honeywell-prod
# Enter: Access Key ID, Secret Access Key, Region (must match AWS_REGION in config)

# Then export the profile before running deploy:
export AWS_PROFILE=honeywell-prod
```

### Option B — Environment variables
```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-south-1   # must match config
```

### Option C — IAM role / instance profile (CI/CD)
If running in a CI pipeline (GitHub Actions, CodeBuild, etc.) attach an
IAM role with the permissions listed in section 4 below.

### Verify credentials point to the right account
```bash
aws sts get-caller-identity
# Should show the expected Account ID and correct region
```

---

## 3. Required IAM Permissions for the Deploying Principal

The AWS user or role that runs deploy.sh needs these IAM permissions
on the **target account**:

```
lambda:CreateFunction, lambda:UpdateFunctionCode,
lambda:UpdateFunctionConfiguration, lambda:GetFunction,
lambda:AddPermission, lambda:RemovePermission,
lambda:WaitForFunctionActive

iam:CreateRole, iam:GetRole, iam:PutRolePolicy

apigateway:GET*, apigateway:POST*, apigateway:PUT*,
apigateway:DELETE*, apigateway:CreateDeployment

dynamodb:CreateTable, dynamodb:DescribeTable,
dynamodb:UpdateTable, dynamodb:UpdateTimeToLive

kms:CreateKey, kms:CreateAlias, kms:ListAliases,
kms:DescribeKey

secretsmanager:DescribeSecret

logs:PutRetentionPolicy, logs:CreateLogGroup

sts:GetCallerIdentity
```

> For a quick-start you can use `AdministratorAccess` on a fresh account,
> then lock it down once deployment is verified.

---

## 4. Prepare Secrets in AWS Secrets Manager

These must exist **before** running the deploy script. The script verifies
they are reachable during preflight and will fail if they are not.

### Alexa (LWA secret)

Store in the region where the Alexa skill's LWA app lives (often `eu-west-1`):

```bash
aws secretsmanager create-secret \
  --name "honeywell/alexa/lwa" \
  --region eu-west-1 \
  --secret-string '{
    "client_id":     "amzn1.application-oa2-client.XXXX",
    "client_secret": "XXXX"
  }'
```

Copy the resulting ARN into `LWA_SECRET_ARN` in your env config file.

### Google Home (OAuth client secret)

Store in the same region as your `AWS_REGION`:

```bash
aws secretsmanager create-secret \
  --name "honeywell/google-home/oauth" \
  --region ap-south-1 \
  --secret-string '{
    "client_id":     "YOUR_COGNITO_APP_CLIENT_ID",
    "client_secret": "YOUR_GOOGLE_OAUTH_CLIENT_SECRET"
  }'
```

Copy the resulting ARN into `GOOGLE_SECRET_ARN`.

> **Important**: the `client_secret` stored here must EXACTLY match what is
> entered in the Google Home Developer Console under Account Linking.
> See `google_console_checklist.md` for details.

---

## 5. Prepare the Environment Config File

Start from the template for a new account:

```bash
cp deploy/env.honeywell.prod.json deploy/env.honeywell.prod.json
```

Open the file and replace every `FILL_IN` value. The script **hard-stops**
if any `FILL_IN` remains — it will not deploy partially configured environments.

Key fields to fill:

| Field | Where to get it |
|---|---|
| `AWS_REGION` | Target region (e.g. `ap-south-1`) |
| `API_GATEWAY_ID` | Leave `""` to create a new API Gateway; or paste an existing ID |
| `API_BASE_URL` | The invoke URL of the API Gateway stage, e.g. `https://abc123.execute-api.ap-south-1.amazonaws.com/smarthome` |
| `COGNITO_USER_POOL_ID` | From Cognito console |
| `COGNITO_CLIENT_ID` | Cognito App Client ID (also used as Google OAuth client_id) |
| `LWA_SECRET_ARN` | From step 4 above |
| `GOOGLE_SECRET_ARN` | From step 4 above |
| `ALEXA_SKILL_ID` | From Alexa Developer Console |
| `ALEXA_REDIRECT_URI` | Must match the Allowed Return URL in the Alexa skill |
| `GOOGLE_AGENT_ID` | Google Home project ID / slug |
| `GOOGLE_REDIRECT_URI` | `https://oauth-redirect.googleusercontent.com/r/<GOOGLE_AGENT_ID>` |
| `APP_DEEP_LINK_SCHEME` | Android deep link scheme (e.g. `honeywell`) |
| `APP_NAME` | Displayed in Google OAuth consent screen |
| `DEVICE_MANUFACTURER` | Used in Google Home SYNC response |

Fields with sensible defaults (no change needed for standard deployments):

| Field | Default | Notes |
|---|---|---|
| `API_STAGE` | `smarthome` | API Gateway stage name |
| `LOG_LEVEL` | `INFO` | Lambda log verbosity |
| `SESSION_TTL_SECONDS` | `1800` | Alexa session / GH link session TTL |
| `AUTH_CODE_TTL_SECONDS` | `900` | Google OAuth auth code TTL |
| `ACCESS_TOKEN_TTL_SECONDS` | `3600` | Google access token TTL |
| `REFRESH_TOKEN_TTL_SECONDS` | `15552000` | ~180 days |

---

## 6. Running the Script

Always run from the **repository root**:

```bash
cd /path/to/app-to-app
chmod +x deploy/deploy.sh
```

### Validate config without deploying (dry run)
```bash
./deploy/deploy.sh --config deploy/env.honeywell.prod.json --dry-run
```
Checks all FILL_IN values are replaced and all CLI tools + AWS credentials
are working. Exits without creating any AWS resources. Run this first.

### Deploy Google Home only
```bash
./deploy/deploy.sh --config deploy/env.honeywell.prod.json --google
```

### Deploy Alexa only
```bash
./deploy/deploy.sh --config deploy/env.honeywell.prod.json --alexa
```

### Deploy everything (Alexa + Google Home)
```bash
./deploy/deploy.sh --config deploy/env.honeywell.prod.json --all
```
Or omit the flag if `DEPLOY_TARGET` in the config file is already set to `"all"`.

### Digilux QA (reference)
```bash
./deploy/deploy.sh --config deploy/env.digilux.qa.json --google
```

---

## 7. What the Script Does (Step by Step)

| Step | What happens |
|---|---|
| 1 — Config validation | Checks no FILL_IN remains; validates required keys per deploy target |
| 2 — Preflight | Verifies aws, jq, python3, zip, curl are installed; validates AWS credentials; checks Secrets Manager ARNs are reachable |
| 3 — API Gateway | Uses existing API GW ID or creates a new REST API; creates Cognito authorizer if it doesn't exist |
| 4a/4b — DynamoDB | Creates all required tables (idempotent); enables TTL; creates GSIs on the tokens table |
| 5a — KMS (Alexa only) | Creates KMS key + alias for LWA token encryption if it doesn't exist |
| 6a/5b — IAM roles | Creates per-Lambda IAM roles with least-privilege inline policies; waits for IAM propagation |
| 7a/6b — Lambda deploy | Packages each Lambda as a zip from `lambdas/`; creates or updates the function; sets env vars from config |
| 8a/7b — API routes | Creates API GW resources and methods; wires Lambda integrations; adds CORS OPTIONS responses |
| 9 — Stage deploy | Creates a new deployment to the configured stage |
| 10 — Smoke tests | Hits each endpoint without auth and verifies 401 (Cognito-protected routes) or non-5xx (public routes) |
| 11 — Console checklist | Prints the Google Console values with actual deployment URLs (Google deploy only) |

The script is **fully idempotent** — safe to run multiple times. Existing
resources are updated, not duplicated.

---

## 8. First Deployment vs Re-deployment

### First deployment on a new account
1. Leave `API_GATEWAY_ID` as `""` in the config — the script creates a new API GW and prints its ID
2. After the first run, copy the printed API Gateway ID into `API_GATEWAY_ID` in the config file
3. Set `API_BASE_URL` to the invoke URL: `https://<API_GW_ID>.execute-api.<REGION>.amazonaws.com/<STAGE>`
4. Re-run with the updated config — subsequent runs will use the existing API GW

### Re-deployment / code update
Just run the same command again. Lambda code and config are updated in place.
API GW routes and DynamoDB tables are left untouched if already correct.

---

## 9. After Deployment

1. Note the endpoint URLs printed in the final summary
2. Follow `google_console_checklist.md` to configure the Google Home Developer Console
3. Follow the Alexa Developer Console to configure the skill's Account Linking URLs
4. Run an end-to-end link test on a real Android device (not emulator — GMS required)
5. Check CloudWatch log groups `/aws/lambda/<function-name>` for `CONFIG_OK` on cold start

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR: Config file not found` | Wrong path to config | Check relative vs absolute path; run from repo root |
| `FILL_IN` error at startup | Config not fully filled | Open the env JSON and replace all `FILL_IN` values |
| `AWS credentials not configured` | Wrong profile or no credentials | Run `aws sts get-caller-identity` manually to debug |
| `LWA secret not accessible` | Secret ARN wrong, wrong region, or missing permissions | Verify ARN in Secrets Manager console; check IAM permissions |
| `API Gateway not found` | Wrong `API_GATEWAY_ID` or wrong region | Check `AWS_REGION` matches where the API GW lives |
| Lambda `AccessDeniedException` at runtime | IAM role missing a permission | Check CloudWatch logs for the specific action denied; update the role policy |
| Smoke test `403` instead of `401` | Cognito authorizer not attached to the method | Re-run deploy; check API GW method settings in console |
| `invalid_client` from Google token endpoint | Client secret mismatch between Secrets Manager and Google Console | Re-read the secret, re-enter it in Google Console exactly |
| Google token endpoint never called (24–48h) | Google project-level backoff triggered | Wait out the backoff; do not retry token endpoint during this window |

---

## 11. Directory Layout

```
deploy/
  deploy.sh                  # Main deployment script
  env.digilux.qa.json        # Digilux QA config (fully populated)
  env.honeywell.prod.json    # Honeywell prod template (FILL_IN → replace)
  google_console_checklist.md  # Step-by-step Google Console setup guide
  DEPLOY_GUIDE.md            # This file

lambdas/
  alexa_start_app_to_app/
  alexa_complete_app_to_app/
  alexa_server_callback/
  alexa_unlink/
  alexa_link_status/
  google_home_start/
  google_home_complete/
  google_home_status/
  google_home_unlink/
  google_home_oauth_authorize/
  google_home_oauth_token/
  google_home_fulfillment/
```

Lambda packages are built into `/tmp/deploy_<ENV_LABEL>_build/` during the run
and can be discarded afterwards.
