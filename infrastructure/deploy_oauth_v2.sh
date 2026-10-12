#!/usr/bin/env bash
# ============================================================
# deploy_oauth_v2.sh
#
# Deploys the full Google Home OAuth 2.0 Cognito Hosted UI flow.
# Source branch: backup/google-home-oauth-flow-oct2026
#
# Steps:
#   1. Backup current google_home_oauth_authorize (code + env)
#   2. Redeploy google_home_oauth_authorize (rewritten)
#   3. Create / update google_home_oauth_cognito_callback (new Lambda)
#   4. Create DynamoDB table google_home_oauth_transactions
#   5. Add API GW routes + deploy stage
#   6. Add Cognito callback URL
#
# Usage:
#   git checkout backup/google-home-oauth-flow-oct2026
#   ./infrastructure/deploy_oauth_v2.sh
# ============================================================

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────
REGION="ap-south-1"
ACCOUNT_ID="986906626244"
API_ID="ds6nxf8ac5"
STAGE="smarthome"
COGNITO_USER_POOL_ID="ap-south-1_h1o8s7257"
COGNITO_CLIENT_ID="q7189jitfkk4ttesepkgls491"
COGNITO_DOMAIN="auth.digilux.co.in"
CALLBACK_URI="https://iot.digilux.co.in/smarthome/google-home/oauth/cognito/callback"
TRANSACTIONS_TABLE="google_home_oauth_transactions"
AUTH_CODES_TABLE="google_home_auth_codes"
GOOGLE_CLIENT_ID="75gmuum6qhfjsedrvck809oie6"
ALLOWED_REDIRECTS="https://oauth-redirect.googleusercontent.com/r/digilux-ai-pro,https://oauth-redirect-sandbox.googleusercontent.com/r/digilux-ai-pro,https://oauth-redirect.googleusercontent.com/r/digilux-ai,https://oauth-redirect-sandbox.googleusercontent.com/r/digilux-ai"
LAMBDA_ROLE="arn:aws:iam::${ACCOUNT_ID}:role/digilux-gh-authorize-role"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${SCRIPT_DIR}/rollback_backups"

# ── Helpers ──────────────────────────────────────────────────────
log()  { echo "  $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ ERROR: $*" >&2; exit 1; }
step() { echo ""; echo "[$1] $2"; }

# ── Pre-flight checks ────────────────────────────────────────────
echo "============================================================"
echo " Google Home OAuth v2 — Deploy"
echo " Region: ${REGION}  API: ${API_ID}  Stage: ${STAGE}"
echo "============================================================"

CURRENT_BRANCH=$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [[ "${CURRENT_BRANCH}" != "backup/google-home-oauth-flow-oct2026" ]]; then
  fail "Must run from branch backup/google-home-oauth-flow-oct2026 (currently on: ${CURRENT_BRANCH})"
fi

[[ -f "${REPO_ROOT}/lambdas/google_home_oauth_authorize/lambda_function.py" ]] \
  || fail "Missing lambdas/google_home_oauth_authorize/lambda_function.py"
[[ -f "${REPO_ROOT}/lambdas/google_home_oauth_cognito_callback/lambda_function.py" ]] \
  || fail "Missing lambdas/google_home_oauth_cognito_callback/lambda_function.py"

command -v aws  > /dev/null || fail "aws CLI not found"
command -v zip  > /dev/null || fail "zip not found"
command -v curl > /dev/null || fail "curl not found"
command -v python3 > /dev/null || fail "python3 not found"

mkdir -p "${BACKUP_DIR}"

# ── Step 1: Backup google_home_oauth_authorize ───────────────────
step "1/6" "Backing up current google_home_oauth_authorize..."

aws lambda get-function-configuration \
  --function-name google_home_oauth_authorize \
  --region "${REGION}" \
  --output json > "${BACKUP_DIR}/authorize_env_backup.json"
ok "Env vars saved → rollback_backups/authorize_env_backup.json"

CODE_URL=$(aws lambda get-function \
  --function-name google_home_oauth_authorize \
  --region "${REGION}" \
  --query 'Code.Location' --output text)
curl -s -o "${BACKUP_DIR}/authorize_code_backup.zip" "${CODE_URL}"
ok "Code saved → rollback_backups/authorize_code_backup.zip"

# ── Step 2: Deploy rewritten google_home_oauth_authorize ─────────
step "2/6" "Deploying rewritten google_home_oauth_authorize..."

BUILD_DIR=$(mktemp -d)
trap 'rm -rf "${BUILD_DIR}"' EXIT

cp "${REPO_ROOT}/lambdas/google_home_oauth_authorize/lambda_function.py" "${BUILD_DIR}/"
(cd "${BUILD_DIR}" && zip -q authorize.zip lambda_function.py)

aws lambda update-function-code \
  --function-name google_home_oauth_authorize \
  --zip-file "fileb://${BUILD_DIR}/authorize.zip" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
aws lambda wait function-updated \
  --function-name google_home_oauth_authorize \
  --region "${REGION}"
ok "Code updated"

aws lambda update-function-configuration \
  --function-name google_home_oauth_authorize \
  --region "${REGION}" \
  --environment "Variables={
    DATA_REGION=${REGION},
    GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},
    ALLOWED_REDIRECT_URIS=${ALLOWED_REDIRECTS},
    COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID},
    COGNITO_HOSTED_UI_DOMAIN=${COGNITO_DOMAIN},
    COGNITO_CALLBACK_URI=${CALLBACK_URI},
    GH_OAUTH_TRANSACTIONS_TABLE=${TRANSACTIONS_TABLE},
    TRANSACTION_TTL_SECONDS=600,
    APP_NAME=Digilux Smart Home,
    LOG_LEVEL=INFO
  }" \
  --no-cli-pager > /dev/null
aws lambda wait function-updated \
  --function-name google_home_oauth_authorize \
  --region "${REGION}"
ok "Env vars updated"

# ── Step 3: Create / update google_home_oauth_cognito_callback ───
step "3/6" "Setting up google_home_oauth_cognito_callback..."

BUILD_DIR_CB=$(mktemp -d)
trap 'rm -rf "${BUILD_DIR}" "${BUILD_DIR_CB}"' EXIT
cp "${REPO_ROOT}/lambdas/google_home_oauth_cognito_callback/lambda_function.py" "${BUILD_DIR_CB}/"
(cd "${BUILD_DIR_CB}" && zip -q callback.zip lambda_function.py)

CB_ENV="Variables={
  DATA_REGION=${REGION},
  COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID},
  COGNITO_HOSTED_UI_DOMAIN=${COGNITO_DOMAIN},
  COGNITO_CALLBACK_URI=${CALLBACK_URI},
  GH_OAUTH_TRANSACTIONS_TABLE=${TRANSACTIONS_TABLE},
  GH_AUTH_CODES_TABLE=${AUTH_CODES_TABLE},
  APP_NAME=Digilux Smart Home,
  AUTH_CODE_TTL_SECONDS=300,
  LOG_LEVEL=INFO
}"

if aws lambda get-function \
     --function-name google_home_oauth_cognito_callback \
     --region "${REGION}" > /dev/null 2>&1; then
  log "Lambda exists — updating code and config..."
  aws lambda update-function-code \
    --function-name google_home_oauth_cognito_callback \
    --zip-file "fileb://${BUILD_DIR_CB}/callback.zip" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
  aws lambda wait function-updated \
    --function-name google_home_oauth_cognito_callback \
    --region "${REGION}"
else
  log "Creating new Lambda..."
  aws lambda create-function \
    --function-name google_home_oauth_cognito_callback \
    --runtime python3.12 \
    --role "${LAMBDA_ROLE}" \
    --handler lambda_function.lambda_handler \
    --zip-file "fileb://${BUILD_DIR_CB}/callback.zip" \
    --timeout 30 \
    --memory-size 256 \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
  aws lambda wait function-active \
    --function-name google_home_oauth_cognito_callback \
    --region "${REGION}"
fi
ok "Lambda ready"

aws lambda update-function-configuration \
  --function-name google_home_oauth_cognito_callback \
  --region "${REGION}" \
  --environment "${CB_ENV}" \
  --no-cli-pager > /dev/null
aws lambda wait function-updated \
  --function-name google_home_oauth_cognito_callback \
  --region "${REGION}"
ok "Env vars set"

# Grant API GW permission to invoke the Lambda
aws lambda add-permission \
  --function-name google_home_oauth_cognito_callback \
  --statement-id apigw-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null 2>&1 && ok "API GW invoke permission set" \
  || log "Invoke permission already exists — skipping"

# ── Step 4: Create DynamoDB table ────────────────────────────────
step "4/6" "Creating DynamoDB table ${TRANSACTIONS_TABLE}..."

if aws dynamodb describe-table \
     --table-name "${TRANSACTIONS_TABLE}" \
     --region "${REGION}" > /dev/null 2>&1; then
  ok "Table already exists — skipping"
else
  aws dynamodb create-table \
    --table-name "${TRANSACTIONS_TABLE}" \
    --attribute-definitions AttributeName=state,AttributeType=S \
    --key-schema AttributeName=state,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
  aws dynamodb wait table-exists \
    --table-name "${TRANSACTIONS_TABLE}" \
    --region "${REGION}"
  ok "Table created"

  aws dynamodb update-time-to-live \
    --table-name "${TRANSACTIONS_TABLE}" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
  ok "TTL enabled on 'ttl' attribute"
fi

# ── Step 5: API Gateway routes ───────────────────────────────────
step "5/6" "Adding API Gateway routes..."
LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:google_home_oauth_cognito_callback"

# Find /google-home/oauth parent resource
OAUTH_RESOURCE_ID=$(aws apigateway get-resources \
  --rest-api-id "${API_ID}" \
  --region "${REGION}" \
  --query "items[?path=='/google-home/oauth'].id" \
  --output text)
[[ -n "${OAUTH_RESOURCE_ID}" ]] || fail "Could not find /google-home/oauth resource in API GW"

# --- /google-home/oauth/cognito ---
COGNITO_RES_ID=$(aws apigateway get-resources \
  --rest-api-id "${API_ID}" \
  --region "${REGION}" \
  --query "items[?path=='/google-home/oauth/cognito'].id" \
  --output text)

if [[ -z "${COGNITO_RES_ID}" ]]; then
  COGNITO_RES_ID=$(aws apigateway create-resource \
    --rest-api-id "${API_ID}" \
    --parent-id "${OAUTH_RESOURCE_ID}" \
    --path-part cognito \
    --region "${REGION}" \
    --query 'id' --output text)
  ok "Created resource /google-home/oauth/cognito  (id: ${COGNITO_RES_ID})"
else
  log "/google-home/oauth/cognito already exists (id: ${COGNITO_RES_ID})"
fi
echo "${COGNITO_RES_ID}" > "${BACKUP_DIR}/apigw_cognito_resource_id.txt"

# --- /google-home/oauth/cognito/callback ---
CB_RES_ID=$(aws apigateway get-resources \
  --rest-api-id "${API_ID}" \
  --region "${REGION}" \
  --query "items[?path=='/google-home/oauth/cognito/callback'].id" \
  --output text)

if [[ -z "${CB_RES_ID}" ]]; then
  CB_RES_ID=$(aws apigateway create-resource \
    --rest-api-id "${API_ID}" \
    --parent-id "${COGNITO_RES_ID}" \
    --path-part callback \
    --region "${REGION}" \
    --query 'id' --output text)
  ok "Created resource /google-home/oauth/cognito/callback  (id: ${CB_RES_ID})"
else
  log "/google-home/oauth/cognito/callback already exists (id: ${CB_RES_ID})"
fi
echo "${CB_RES_ID}" > "${BACKUP_DIR}/apigw_callback_resource_id.txt"

# GET method on /cognito/callback
aws apigateway put-method \
  --rest-api-id "${API_ID}" \
  --resource-id "${CB_RES_ID}" \
  --http-method GET \
  --authorization-type NONE \
  --region "${REGION}" \
  --no-cli-pager > /dev/null 2>&1 || true

aws apigateway put-integration \
  --rest-api-id "${API_ID}" \
  --resource-id "${CB_RES_ID}" \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
ok "GET /google-home/oauth/cognito/callback → google_home_oauth_cognito_callback"

# --- /google-home/oauth/consent ---
CONSENT_RES_ID=$(aws apigateway get-resources \
  --rest-api-id "${API_ID}" \
  --region "${REGION}" \
  --query "items[?path=='/google-home/oauth/consent'].id" \
  --output text)

if [[ -z "${CONSENT_RES_ID}" ]]; then
  CONSENT_RES_ID=$(aws apigateway create-resource \
    --rest-api-id "${API_ID}" \
    --parent-id "${OAUTH_RESOURCE_ID}" \
    --path-part consent \
    --region "${REGION}" \
    --query 'id' --output text)
  ok "Created resource /google-home/oauth/consent  (id: ${CONSENT_RES_ID})"
else
  log "/google-home/oauth/consent already exists (id: ${CONSENT_RES_ID})"
fi
echo "${CONSENT_RES_ID}" > "${BACKUP_DIR}/apigw_consent_resource_id.txt"

# POST method on /consent
aws apigateway put-method \
  --rest-api-id "${API_ID}" \
  --resource-id "${CONSENT_RES_ID}" \
  --http-method POST \
  --authorization-type NONE \
  --region "${REGION}" \
  --no-cli-pager > /dev/null 2>&1 || true

aws apigateway put-integration \
  --rest-api-id "${API_ID}" \
  --resource-id "${CONSENT_RES_ID}" \
  --http-method POST \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
ok "POST /google-home/oauth/consent → google_home_oauth_cognito_callback"

# Deploy API GW stage
aws apigateway create-deployment \
  --rest-api-id "${API_ID}" \
  --stage-name "${STAGE}" \
  --description "Deploy Google Home OAuth v2 (Cognito Hosted UI flow)" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
ok "Stage '${STAGE}' deployed"

# ── Step 6: Cognito callback URL ─────────────────────────────────
step "6/6" "Adding Cognito callback URL..."

# Read full client config to safely add the URL without wiping other settings
EXISTING_CALLBACKS=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "${COGNITO_USER_POOL_ID}" \
  --client-id "${COGNITO_CLIENT_ID}" \
  --region "${REGION}" \
  --query 'UserPoolClient.CallbackURLs' \
  --output json)

if echo "${EXISTING_CALLBACKS}" | grep -qF "${CALLBACK_URI}"; then
  ok "Callback URL already registered — skipping"
else
  # Save current URLs to backup
  echo "${EXISTING_CALLBACKS}" > "${BACKUP_DIR}/cognito_callbacks_backup.json"

  # Add new URL and update client (preserve all existing OAuth settings)
  UPDATED_CALLBACKS=$(python3 -c "
import json, sys
urls = json.loads(sys.stdin.read())
if '${CALLBACK_URI}' not in urls:
    urls.append('${CALLBACK_URI}')
# Output as space-separated quoted strings for AWS CLI
print(' '.join(urls))
" <<< "${EXISTING_CALLBACKS}")

  aws cognito-idp update-user-pool-client \
    --user-pool-id "${COGNITO_USER_POOL_ID}" \
    --client-id "${COGNITO_CLIENT_ID}" \
    --region "${REGION}" \
    --callback-urls ${UPDATED_CALLBACKS} \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes "aws.cognito.signin.user.admin" "email" "openid" "phone" "smarthome_server/read" "smarthome_server/write" \
    --allowed-o-auth-flows-user-pool-client \
    --no-cli-pager > /dev/null
  ok "Callback URL added: ${CALLBACK_URI}"
fi

# ── Done ─────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " ✅ Deploy complete"
echo ""
echo " Backup files saved in: infrastructure/rollback_backups/"
echo " To undo everything:   ./infrastructure/rollback_oauth_v2.sh"
echo "============================================================"
