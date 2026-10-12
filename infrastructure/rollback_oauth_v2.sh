#!/usr/bin/env bash
# ============================================================
# rollback_oauth_v2.sh
#
# Rolls back the Google Home OAuth v2 deployment.
# Restores the environment to the state before deploy_oauth_v2.sh ran.
#
# What this undoes:
#   1. Restores google_home_oauth_authorize (code + env vars)
#   2. Deletes google_home_oauth_cognito_callback Lambda
#   3. Deletes DynamoDB table google_home_oauth_transactions
#   4. Removes API GW routes + redeploys stage
#   5. Removes Cognito callback URL
#
# Requires: deploy_oauth_v2.sh must have run first (uses its backups)
#
# Usage:
#   ./infrastructure/rollback_oauth_v2.sh
# ============================================================

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────
REGION="ap-south-1"
API_ID="ds6nxf8ac5"
STAGE="smarthome"
COGNITO_USER_POOL_ID="ap-south-1_h1o8s7257"
COGNITO_CLIENT_ID="q7189jitfkk4ttesepkgls491"
CALLBACK_URI="https://iot.digilux.co.in/smarthome/google-home/oauth/cognito/callback"
TRANSACTIONS_TABLE="google_home_oauth_transactions"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${SCRIPT_DIR}/rollback_backups"

# ── Helpers ──────────────────────────────────────────────────────
log()  { echo "  $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
fail() { echo "  ✗ ERROR: $*" >&2; exit 1; }
step() { echo ""; echo "[$1] $2"; }

# ── Pre-flight checks ────────────────────────────────────────────
echo "============================================================"
echo " Google Home OAuth v2 — Rollback"
echo " Region: ${REGION}  API: ${API_ID}  Stage: ${STAGE}"
echo "============================================================"

[[ -d "${BACKUP_DIR}" ]] \
  || fail "Backup directory not found: ${BACKUP_DIR}  (run deploy_oauth_v2.sh first)"
[[ -f "${BACKUP_DIR}/authorize_code_backup.zip" ]] \
  || fail "Missing authorize_code_backup.zip — cannot restore Lambda code"
[[ -f "${BACKUP_DIR}/authorize_env_backup.json" ]] \
  || fail "Missing authorize_env_backup.json — cannot restore env vars"

command -v aws     > /dev/null || fail "aws CLI not found"
command -v python3 > /dev/null || fail "python3 not found"

echo ""
echo "  ⚠  This will restore the previous OAuth flow (login form version)."
read -r -p "  Continue? [y/N] " CONFIRM
[[ "${CONFIRM}" =~ ^[yY]$ ]] || { echo "  Aborted."; exit 0; }

# ── Step 1: Restore google_home_oauth_authorize ──────────────────
step "1/5" "Restoring google_home_oauth_authorize..."

aws lambda update-function-code \
  --function-name google_home_oauth_authorize \
  --zip-file "fileb://${BACKUP_DIR}/authorize_code_backup.zip" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
aws lambda wait function-updated \
  --function-name google_home_oauth_authorize \
  --region "${REGION}"
ok "Code restored from backup"

# Rebuild env vars object from the saved config JSON
ENV_JSON=$(python3 -c "
import json, sys
cfg = json.load(open('${BACKUP_DIR}/authorize_env_backup.json'))
env = cfg.get('Environment', {}).get('Variables', {})
pairs = ','.join(f'{k}={v}' for k, v in env.items())
print('Variables={' + pairs + '}')
")

aws lambda update-function-configuration \
  --function-name google_home_oauth_authorize \
  --region "${REGION}" \
  --environment "${ENV_JSON}" \
  --no-cli-pager > /dev/null
aws lambda wait function-updated \
  --function-name google_home_oauth_authorize \
  --region "${REGION}"
ok "Env vars restored"

# ── Step 2: Delete google_home_oauth_cognito_callback ────────────
step "2/5" "Removing google_home_oauth_cognito_callback Lambda..."

if aws lambda get-function \
     --function-name google_home_oauth_cognito_callback \
     --region "${REGION}" > /dev/null 2>&1; then
  aws lambda delete-function \
    --function-name google_home_oauth_cognito_callback \
    --region "${REGION}" \
    --no-cli-pager
  ok "Lambda deleted"
else
  warn "Lambda not found — already removed or never deployed"
fi

# ── Step 3: Delete DynamoDB table ────────────────────────────────
step "3/5" "Removing DynamoDB table ${TRANSACTIONS_TABLE}..."

if aws dynamodb describe-table \
     --table-name "${TRANSACTIONS_TABLE}" \
     --region "${REGION}" > /dev/null 2>&1; then
  aws dynamodb delete-table \
    --table-name "${TRANSACTIONS_TABLE}" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null
  aws dynamodb wait table-not-exists \
    --table-name "${TRANSACTIONS_TABLE}" \
    --region "${REGION}"
  ok "Table deleted"
else
  warn "Table not found — already removed or never created"
fi

# ── Step 4: Remove API Gateway routes ────────────────────────────
step "4/5" "Removing API Gateway routes..."

# Delete /google-home/oauth/cognito/callback (GET method + resource)
CB_RES_ID=""
if [[ -f "${BACKUP_DIR}/apigw_callback_resource_id.txt" ]]; then
  CB_RES_ID=$(cat "${BACKUP_DIR}/apigw_callback_resource_id.txt")
else
  CB_RES_ID=$(aws apigateway get-resources \
    --rest-api-id "${API_ID}" \
    --region "${REGION}" \
    --query "items[?path=='/google-home/oauth/cognito/callback'].id" \
    --output text)
fi

if [[ -n "${CB_RES_ID}" ]]; then
  aws apigateway delete-resource \
    --rest-api-id "${API_ID}" \
    --resource-id "${CB_RES_ID}" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null 2>&1 \
    && ok "Deleted /google-home/oauth/cognito/callback resource" \
    || warn "/google-home/oauth/cognito/callback not found — skipping"
fi

# Deleting the /callback resource also orphans /cognito — delete that too
COGNITO_RES_ID=""
if [[ -f "${BACKUP_DIR}/apigw_cognito_resource_id.txt" ]]; then
  COGNITO_RES_ID=$(cat "${BACKUP_DIR}/apigw_cognito_resource_id.txt")
else
  COGNITO_RES_ID=$(aws apigateway get-resources \
    --rest-api-id "${API_ID}" \
    --region "${REGION}" \
    --query "items[?path=='/google-home/oauth/cognito'].id" \
    --output text)
fi

if [[ -n "${COGNITO_RES_ID}" ]]; then
  aws apigateway delete-resource \
    --rest-api-id "${API_ID}" \
    --resource-id "${COGNITO_RES_ID}" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null 2>&1 \
    && ok "Deleted /google-home/oauth/cognito resource" \
    || warn "/google-home/oauth/cognito not found — skipping"
fi

# Delete /google-home/oauth/consent (POST method + resource)
CONSENT_RES_ID=""
if [[ -f "${BACKUP_DIR}/apigw_consent_resource_id.txt" ]]; then
  CONSENT_RES_ID=$(cat "${BACKUP_DIR}/apigw_consent_resource_id.txt")
else
  CONSENT_RES_ID=$(aws apigateway get-resources \
    --rest-api-id "${API_ID}" \
    --region "${REGION}" \
    --query "items[?path=='/google-home/oauth/consent'].id" \
    --output text)
fi

if [[ -n "${CONSENT_RES_ID}" ]]; then
  aws apigateway delete-resource \
    --rest-api-id "${API_ID}" \
    --resource-id "${CONSENT_RES_ID}" \
    --region "${REGION}" \
    --no-cli-pager > /dev/null 2>&1 \
    && ok "Deleted /google-home/oauth/consent resource" \
    || warn "/google-home/oauth/consent not found — skipping"
fi

# Redeploy stage to publish the route removals
aws apigateway create-deployment \
  --rest-api-id "${API_ID}" \
  --stage-name "${STAGE}" \
  --description "Rollback: remove Google Home OAuth v2 routes" \
  --region "${REGION}" \
  --no-cli-pager > /dev/null
ok "Stage '${STAGE}' redeployed"

# ── Step 5: Remove Cognito callback URL ──────────────────────────
step "5/5" "Removing Cognito callback URL..."

CURRENT_CALLBACKS=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "${COGNITO_USER_POOL_ID}" \
  --client-id "${COGNITO_CLIENT_ID}" \
  --region "${REGION}" \
  --query 'UserPoolClient.CallbackURLs' \
  --output json)

if ! echo "${CURRENT_CALLBACKS}" | grep -qF "${CALLBACK_URI}"; then
  warn "Callback URL not present — already removed"
else
  if [[ -f "${BACKUP_DIR}/cognito_callbacks_backup.json" ]]; then
    # Restore exact original list from backup
    RESTORED_CALLBACKS=$(python3 -c "
import json
urls = json.load(open('${BACKUP_DIR}/cognito_callbacks_backup.json'))
print(' '.join(urls))
")
  else
    # No backup — remove the URL from current list
    RESTORED_CALLBACKS=$(python3 -c "
import json, sys
urls = json.loads(sys.stdin.read())
urls = [u for u in urls if u != '${CALLBACK_URI}']
print(' '.join(urls))
" <<< "${CURRENT_CALLBACKS}")
  fi

  aws cognito-idp update-user-pool-client \
    --user-pool-id "${COGNITO_USER_POOL_ID}" \
    --client-id "${COGNITO_CLIENT_ID}" \
    --region "${REGION}" \
    --callback-urls ${RESTORED_CALLBACKS} \
    --allowed-o-auth-flows code \
    --allowed-o-auth-scopes "aws.cognito.signin.user.admin" "email" "openid" "phone" "smarthome_server/read" "smarthome_server/write" \
    --allowed-o-auth-flows-user-pool-client \
    --no-cli-pager > /dev/null
  ok "Callback URL removed: ${CALLBACK_URI}"
fi

# ── Done ─────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo " ✅ Rollback complete"
echo ""
echo " google_home_oauth_authorize  — restored to pre-deploy code + env"
echo " google_home_oauth_cognito_callback — deleted"
echo " google_home_oauth_transactions     — deleted"
echo " API GW routes                      — removed, stage redeployed"
echo " Cognito callback URL               — removed"
echo "============================================================"
