#!/bin/bash
# deploy_all.sh — Zero-touch deployment for Digilux voice integrations
#
# Deploys Alexa and/or Google Home Lambdas, wires API Gateway routes,
# deploys the stage, and runs smoke tests — no manual steps required.
#
# Usage:
#   ./deploy_all.sh --alexa              # deploy Alexa only
#   ./deploy_all.sh --google             # deploy Google Home only
#   ./deploy_all.sh --all                # deploy both
#   ./deploy_all.sh --alexa --dry-run    # validate prerequisites only
#
# Required env vars — Alexa:
#   LWA_SECRET_ARN    — Secrets Manager ARN for LWA client_id + client_secret
#   KMS_KEY_ARN       — KMS key ARN for token encryption (auto-detected if not set)
#   ALEXA_SKILL_ID    — Alexa skill ID (amzn1.ask.skill....)
#
# Required env vars — Google Home:
#   GOOGLE_AGENT_ID       — Google Home agent/project ID
#   GOOGLE_CLIENT_ID      — Google OAuth client ID
#   GOOGLE_REDIRECT_URI   — OAuth redirect URI registered in Google Console
#   GOOGLE_SECRET_ARN     — Secrets Manager ARN for Google client_id + client_secret
#
# Optional:
#   DRY_RUN=1             — validate and package only, do not deploy
#   LOG_LEVEL=DEBUG       — Lambda log verbosity

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────────────────────────
DEPLOY_ALEXA=0
DEPLOY_GOOGLE=0
DRY_RUN=${DRY_RUN:-0}

for arg in "$@"; do
  case "$arg" in
    --alexa)   DEPLOY_ALEXA=1 ;;
    --google)  DEPLOY_GOOGLE=1 ;;
    --all)     DEPLOY_ALEXA=1; DEPLOY_GOOGLE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    *) echo "Unknown argument: $arg"; echo "Usage: $0 --alexa|--google|--all [--dry-run]"; exit 1 ;;
  esac
done

if [ "$DEPLOY_ALEXA" -eq 0 ] && [ "$DEPLOY_GOOGLE" -eq 0 ]; then
  echo "ERROR: Specify --alexa, --google, or --all"
  echo "Usage: $0 --alexa|--google|--all [--dry-run]"
  exit 1
fi

# ── Constants ─────────────────────────────────────────────────────────────────
REGION="ap-south-1"
API_ID="ds6nxf8ac5"               # iot.digilux.co.in/smarthome → this API
API_STAGE="smarthome"             # confirmed live stage
AUTHORIZER_ID="fp1yfy"           # Cognito USER_POOLS authorizer
COGNITO_USER_POOL_ID="ap-south-1_h1o8s7257"
COGNITO_REGION="ap-south-1"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-q7189jitfkk4ttesepkgls491}"
OAUTH_BASE_URL="https://iot.digilux.co.in/smarthome"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="${SCRIPT_DIR}/../lambdas"
BUILD_DIR="/tmp/digilux_lambda_build"

PASS=0; FAIL=0
FAILED_CHECKS=()

_pass()    { echo "  ✓ $1"; PASS=$((PASS+1)); }
_fail()    { echo "  ✗ $1"; FAIL=$((FAIL+1)); FAILED_CHECKS+=("$1"); }
_section() { echo ""; echo "━━━ $1 ━━━"; }
_info()    { echo "  → $1"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
_section "PREFLIGHT"

# AWS credentials
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  && _pass "AWS credentials valid (account: $ACCOUNT_ID)" \
  || { _fail "AWS credentials not configured"; exit 1; }

# AWS CLI tools
for tool in aws jq python3 zip; do
  command -v "$tool" &>/dev/null && _pass "$tool available" || { _fail "$tool not found — install it first"; exit 1; }
done

# Confirm API exists
aws apigateway get-rest-api --rest-api-id "$API_ID" --region "$REGION" \
  --query 'id' --output text &>/dev/null \
  && _pass "API Gateway $API_ID reachable" \
  || { _fail "API Gateway $API_ID not found in $REGION"; exit 1; }

# Alexa prerequisite check
if [ "$DEPLOY_ALEXA" -eq 1 ]; then
  _section "ALEXA PREREQUISITES"
  [ -n "${LWA_SECRET_ARN:-}" ]  && _pass "LWA_SECRET_ARN set"   || _fail "LWA_SECRET_ARN not set — export LWA_SECRET_ARN=arn:aws:secretsmanager:..."
  [ -n "${ALEXA_SKILL_ID:-}" ]  && _pass "ALEXA_SKILL_ID set"   || _fail "ALEXA_SKILL_ID not set — export ALEXA_SKILL_ID=amzn1.ask.skill...."

  # Auto-detect KMS key if not set
  if [ -z "${KMS_KEY_ARN:-}" ]; then
    KMS_KEY_ARN=$(aws kms list-aliases --region "$REGION" \
      --query "Aliases[?AliasName=='alias/digilux-alexa-tokens'].TargetKeyId" \
      --output text 2>/dev/null)
    if [ -n "$KMS_KEY_ARN" ] && [ "$KMS_KEY_ARN" != "None" ]; then
      KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ARN}"
      _pass "KMS key auto-detected: $KMS_KEY_ARN"
    else
      _fail "KMS key alias/digilux-alexa-tokens not found and KMS_KEY_ARN not set"
    fi
  else
    _pass "KMS_KEY_ARN set"
  fi

  # Validate LWA secret is accessible
  aws secretsmanager describe-secret --secret-id "$LWA_SECRET_ARN" --region "$REGION" \
    --query 'Name' --output text &>/dev/null \
    && _pass "LWA secret accessible in Secrets Manager" \
    || _fail "LWA secret not accessible: $LWA_SECRET_ARN"
fi

# Google Home prerequisite check
if [ "$DEPLOY_GOOGLE" -eq 1 ]; then
  _section "GOOGLE HOME PREREQUISITES"
  [ -n "${GOOGLE_AGENT_ID:-}" ]     && _pass "GOOGLE_AGENT_ID set"     || _fail "GOOGLE_AGENT_ID not set"
  [ -n "${GOOGLE_CLIENT_ID:-}" ]    && _pass "GOOGLE_CLIENT_ID set"    || _fail "GOOGLE_CLIENT_ID not set"
  [ -n "${GOOGLE_REDIRECT_URI:-}" ] && _pass "GOOGLE_REDIRECT_URI set" || _fail "GOOGLE_REDIRECT_URI not set"
  [ -n "${GOOGLE_SECRET_ARN:-}" ]   && _pass "GOOGLE_SECRET_ARN set"   || _fail "GOOGLE_SECRET_ARN not set"

  if [ -n "${GOOGLE_SECRET_ARN:-}" ]; then
    aws secretsmanager describe-secret --secret-id "$GOOGLE_SECRET_ARN" --region "$REGION" \
      --query 'Name' --output text &>/dev/null \
      && _pass "Google secret accessible in Secrets Manager" \
      || _fail "Google secret not accessible: $GOOGLE_SECRET_ARN"
  fi
fi

# Fail fast if any prerequisite failed
if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo "  ✗ $FAIL prerequisite(s) failed — fix above before deploying:"
  for c in "${FAILED_CHECKS[@]}"; do echo "    - $c"; done
  exit 1
fi

[ "$DRY_RUN" -eq 1 ] && { echo ""; echo "  DRY RUN — prerequisites OK. Exiting without deploying."; exit 0; }

# ── Shared helpers ────────────────────────────────────────────────────────────

package_lambda() {
  local NAME="$1" SRC_DIR="$2"
  local OUT="${BUILD_DIR}/${NAME}.zip"
  mkdir -p "$BUILD_DIR"
  rm -f "$OUT"
  # Package Lambda + any local dependencies (exclude __pycache__ and tests)
  (cd "$SRC_DIR" && zip -qr "$OUT" . -x "__pycache__/*" "*.pyc" "tests/*")
  _info "Packaged ${NAME} → $(du -sh "$OUT" | cut -f1)"
  echo "$OUT"
}

deploy_lambda() {
  local NAME="$1" ZIP="$2" ROLE_ARN="$3" ENV_VARS="$4" TIMEOUT="${5:-15}"
  if aws lambda get-function --function-name "$NAME" --region "$REGION" &>/dev/null; then
    # Update existing
    aws lambda update-function-code \
      --function-name "$NAME" --zip-file "fileb://${ZIP}" \
      --region "$REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-updated --function-name "$NAME" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$NAME" \
      --role "$ROLE_ARN" \
      --timeout "$TIMEOUT" \
      --environment "Variables={${ENV_VARS}}" \
      --tracing-config Mode=Active \
      --region "$REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-updated --function-name "$NAME" --region "$REGION"
    _pass "Lambda $NAME updated"
  else
    # Create new
    aws lambda create-function \
      --function-name "$NAME" \
      --runtime python3.12 \
      --role "$ROLE_ARN" \
      --handler lambda_function.lambda_handler \
      --zip-file "fileb://${ZIP}" \
      --timeout "$TIMEOUT" \
      --environment "Variables={${ENV_VARS}}" \
      --tracing-config Mode=Active \
      --region "$REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-active --function-name "$NAME" --region "$REGION"
    _pass "Lambda $NAME created"
  fi

  # Set log retention
  aws logs put-retention-policy \
    --log-group-name "/aws/lambda/${NAME}" \
    --retention-in-days 30 \
    --region "$REGION" 2>/dev/null || true
}

create_or_update_role() {
  local ROLE="$1" POLICY_JSON="$2"
  local TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  if aws iam get-role --role-name "$ROLE" &>/dev/null; then
    aws iam put-role-policy --role-name "$ROLE" \
      --policy-name "${ROLE}-policy" \
      --policy-document "$POLICY_JSON" > /dev/null
    _info "IAM role $ROLE updated"
  else
    aws iam create-role --role-name "$ROLE" \
      --assume-role-policy-document "$TRUST" \
      --output text --query 'Role.RoleName' > /dev/null
    aws iam put-role-policy --role-name "$ROLE" \
      --policy-name "${ROLE}-policy" \
      --policy-document "$POLICY_JSON" > /dev/null
    _info "IAM role $ROLE created"
    sleep 8   # IAM propagation
  fi
  aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text
}

get_or_create_resource() {
  local PARENT_ID="$1" PATH_PART="$2"
  local EXISTING
  EXISTING=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
    --query "items[?parentId=='${PARENT_ID}' && pathPart=='${PATH_PART}'].id" \
    --output text 2>/dev/null)
  if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
    echo "$EXISTING"
  else
    aws apigateway create-resource \
      --rest-api-id "$API_ID" --parent-id "$PARENT_ID" \
      --path-part "$PATH_PART" --region "$REGION" \
      --query 'id' --output text
  fi
}

wire_method() {
  local HTTP_METHOD="$1" RESOURCE_ID="$2" LAMBDA_NAME="$3" AUTH="${4:-COGNITO_USER_POOLS}"
  local LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
  local URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations"

  # Create method (idempotent)
  aws apigateway put-method \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --authorization-type "$AUTH" \
    $( [ "$AUTH" = "COGNITO_USER_POOLS" ] && echo "--authorizer-id $AUTHORIZER_ID" ) \
    --region "$REGION" &>/dev/null || true

  # Create integration
  aws apigateway put-integration \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" --type AWS_PROXY \
    --integration-http-method POST --uri "$URI" \
    --region "$REGION" &>/dev/null || true

  # Lambda permission (idempotent)
  local SID="${LAMBDA_NAME}-apigw-${HTTP_METHOD,,}-${RESOURCE_ID}"
  aws lambda remove-permission --function-name "$LAMBDA_NAME" \
    --statement-id "$SID" --region "$REGION" &>/dev/null || true
  aws lambda add-permission --function-name "$LAMBDA_NAME" \
    --statement-id "$SID" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/*" \
    --region "$REGION" &>/dev/null || true

  # CORS OPTIONS method
  aws apigateway put-method \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --authorization-type NONE \
    --region "$REGION" &>/dev/null || true
  aws apigateway put-integration \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --type MOCK \
    --request-templates '{"application/json":"{\"statusCode\":200}"}' \
    --region "$REGION" &>/dev/null || true
  aws apigateway put-method-response \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{"method.response.header.Access-Control-Allow-Headers":false,"method.response.header.Access-Control-Allow-Methods":false,"method.response.header.Access-Control-Allow-Origin":false}' \
    --region "$REGION" &>/dev/null || true
  aws apigateway put-integration-response \
    --rest-api-id "$API_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{"method.response.header.Access-Control-Allow-Headers":"'"'"'Content-Type,Authorization'"'"'","method.response.header.Access-Control-Allow-Methods":"'"'"'OPTIONS,GET,POST,DELETE'"'"'","method.response.header.Access-Control-Allow-Origin":"'"'"'*'"'"'"}' \
    --region "$REGION" &>/dev/null || true
}

deploy_stage() {
  aws apigateway create-deployment \
    --rest-api-id "$API_ID" \
    --stage-name "$API_STAGE" \
    --description "Deployed by deploy_all.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --region "$REGION" --output text --query 'id' > /dev/null
  _pass "API Gateway stage '$API_STAGE' deployed"
}

# Shared policy fragments
LOGS_STMT='"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:'"${REGION}"':'"${ACCOUNT_ID}"':log-group:/aws/lambda/*"'
XRAY_STMT='"Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords"],"Resource":"*"'

# ── Root API resources (shared) ───────────────────────────────────────────────
ROOT_ID=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[?path==`/`].id' --output text)
V1_ID=$(get_or_create_resource "$ROOT_ID" "api")
V1_ID=$(get_or_create_resource "$V1_ID" "v1")

# ══════════════════════════════════════════════════════════════════════════════
# ALEXA DEPLOYMENT
# ══════════════════════════════════════════════════════════════════════════════
if [ "$DEPLOY_ALEXA" -eq 1 ]; then

_section "ALEXA — DynamoDB TABLES"

for TABLE_DEF in \
  "alexa_app_linking_sessions:state:S" \
  "digilux_honeywell_alexa_lwa_tokens:userId:S"; do
  TABLE=$(echo "$TABLE_DEF" | cut -d: -f1)
  PK=$(echo "$TABLE_DEF" | cut -d: -f2)
  TYPE=$(echo "$TABLE_DEF" | cut -d: -f3)
  if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" &>/dev/null; then
    _pass "Table $TABLE already exists"
  else
    aws dynamodb create-table --table-name "$TABLE" \
      --attribute-definitions "AttributeName=${PK},AttributeType=${TYPE}" \
      --key-schema "AttributeName=${PK},KeyType=HASH" \
      --billing-mode PAY_PER_REQUEST \
      --region "$REGION" --output text --query 'TableDescription.TableName' > /dev/null
    aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
    aws dynamodb update-time-to-live --table-name "$TABLE" --region "$REGION" \
      --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null
    _pass "Table $TABLE created"
  fi
done

_section "ALEXA — IAM ROLES"

ROLE_START_ARN=$(create_or_update_role "digilux-alexa-start-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Sessions\",${LOGS_STMT/log-group:\/aws\/lambda\/*//aws\/lambda\/alexa_*}},
    {\"Sid\":\"DDB\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/alexa_app_linking_sessions\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_COMPLETE_ARN=$(create_or_update_role "digilux-alexa-complete-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/alexa_app_linking_sessions\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_alexa_lwa_tokens\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\",\"kms:GenerateDataKey\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_CALLBACK_ARN=$(create_or_update_role "digilux-alexa-callback-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_UNLINK_ARN=$(create_or_update_role "digilux-alexa-unlink-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_alexa_lwa_tokens\"},
    {\"Sid\":\"DeviceMapping\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_user_device_mapping\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_STATUS_ARN=$(create_or_update_role "digilux-alexa-status-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_alexa_lwa_tokens\"},
    {\"Sid\":\"DeviceMapping\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_user_device_mapping\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

_section "ALEXA — PACKAGE + DEPLOY LAMBDAS"

BASE_URL="https://iot.digilux.co.in/smarthome"
REDIRECT_URI="${BASE_URL}/alexa/callback"
ALLOWED_REDIRECT_HOSTS="iot.digilux.co.in"

ALEXA_COMMON="DATA_REGION=${REGION},LOG_LEVEL=${LOG_LEVEL},COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},COGNITO_REGION=${COGNITO_REGION},COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}"

ZIP=$(package_lambda "alexa_start_app_to_app" "${LAMBDA_DIR}/alexa_start_app_to_app")
deploy_lambda "alexa_start_app_to_app" "$ZIP" "$ROLE_START_ARN" \
  "${ALEXA_COMMON},SESSION_TABLE=alexa_app_linking_sessions,REDIRECT_URI=${REDIRECT_URI},ALLOWED_REDIRECT_HOSTS=${ALLOWED_REDIRECT_HOSTS},KMS_KEY_ARN=${KMS_KEY_ARN}" 15

ZIP=$(package_lambda "alexa_complete_app_to_app" "${LAMBDA_DIR}/alexa_complete_app_to_app")
deploy_lambda "alexa_complete_app_to_app" "$ZIP" "$ROLE_COMPLETE_ARN" \
  "${ALEXA_COMMON},SESSION_TABLE=alexa_app_linking_sessions,LWA_TOKENS_TABLE=digilux_honeywell_alexa_lwa_tokens,LWA_SECRET_ARN=${LWA_SECRET_ARN},KMS_KEY_ARN=${KMS_KEY_ARN},ALEXA_SKILL_ID=${ALEXA_SKILL_ID:-}" 30

ZIP=$(package_lambda "alexa_callback" "${LAMBDA_DIR}/alexa_callback")
deploy_lambda "alexa_callback" "$ZIP" "$ROLE_CALLBACK_ARN" \
  "${ALEXA_COMMON},APP_DEEP_LINK_SCHEME=digilux,APP_DEEP_LINK_HOST=alexa" 10

ZIP=$(package_lambda "alexa_unlink" "${LAMBDA_DIR}/alexa_unlink")
deploy_lambda "alexa_unlink" "$ZIP" "$ROLE_UNLINK_ARN" \
  "${ALEXA_COMMON},LWA_TOKENS_TABLE=digilux_honeywell_alexa_lwa_tokens,USER_DEVICE_MAPPING_TABLE=digilux_honeywell_user_device_mapping,LWA_SECRET_ARN=${LWA_SECRET_ARN},KMS_KEY_ARN=${KMS_KEY_ARN},ALEXA_SKILL_ID=${ALEXA_SKILL_ID:-}" 15

ZIP=$(package_lambda "alexa_link_status" "${LAMBDA_DIR}/alexa_link_status")
deploy_lambda "alexa_link_status" "$ZIP" "$ROLE_STATUS_ARN" \
  "${ALEXA_COMMON},LWA_TOKENS_TABLE=digilux_honeywell_alexa_lwa_tokens,USER_DEVICE_MAPPING_TABLE=digilux_honeywell_user_device_mapping,LWA_SECRET_ARN=${LWA_SECRET_ARN},KMS_KEY_ARN=${KMS_KEY_ARN}" 15

_section "ALEXA — API GATEWAY ROUTES"

ALEXA_ID=$(get_or_create_resource "$V1_ID" "alexa")
STATUS_ID=$(get_or_create_resource "$ALEXA_ID" "status")
START_ID=$(get_or_create_resource "$ALEXA_ID" "startAppToApp")
COMPLETE_ID=$(get_or_create_resource "$ALEXA_ID" "completeAppToApp")
UNLINK_ID=$(get_or_create_resource "$ALEXA_ID" "unlink")
ROOT_ALEXA_ID=$(get_or_create_resource "$ROOT_ID" "alexa")
CALLBACK_ID=$(get_or_create_resource "$ROOT_ALEXA_ID" "callback")
SKILL_EVENT_ID=$(get_or_create_resource "$ROOT_ALEXA_ID" "skill-event")

wire_method GET    "$STATUS_ID"     "alexa_link_status"      "COGNITO_USER_POOLS"
wire_method POST   "$START_ID"      "alexa_start_app_to_app"  "COGNITO_USER_POOLS"
wire_method POST   "$COMPLETE_ID"   "alexa_complete_app_to_app" "COGNITO_USER_POOLS"
wire_method DELETE "$UNLINK_ID"     "alexa_unlink"           "COGNITO_USER_POOLS"
wire_method GET    "$CALLBACK_ID"   "alexa_callback"         "NONE"
wire_method POST   "$SKILL_EVENT_ID" "alexa_skill_events"    "NONE"

_pass "Alexa API Gateway routes wired"

fi  # end DEPLOY_ALEXA

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE HOME DEPLOYMENT
# ══════════════════════════════════════════════════════════════════════════════
if [ "$DEPLOY_GOOGLE" -eq 1 ]; then

_section "GOOGLE HOME — DynamoDB TABLES"

for TABLE_DEF in \
  "google_home_link_sessions:state:S" \
  "google_home_auth_codes:code:S" \
  "google_home_tokens:userId:S"; do
  TABLE=$(echo "$TABLE_DEF" | cut -d: -f1)
  PK=$(echo "$TABLE_DEF" | cut -d: -f2)
  TYPE=$(echo "$TABLE_DEF" | cut -d: -f3)
  if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" &>/dev/null; then
    _pass "Table $TABLE already exists"
  else
    aws dynamodb create-table --table-name "$TABLE" \
      --attribute-definitions "AttributeName=${PK},AttributeType=${TYPE}" \
      --key-schema "AttributeName=${PK},KeyType=HASH" \
      --billing-mode PAY_PER_REQUEST \
      --region "$REGION" --output text --query 'TableDescription.TableName' > /dev/null
    aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
    aws dynamodb update-time-to-live --table-name "$TABLE" --region "$REGION" \
      --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null
    _pass "Table $TABLE created"
  fi
done

# Add GSIs to tokens table (accessToken-index, refreshToken-index) if missing
for GSI in accessToken refreshToken; do
  EXISTS=$(aws dynamodb describe-table --table-name google_home_tokens --region "$REGION" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='${GSI}-index'].IndexName" \
    --output text 2>/dev/null)
  if [ -z "$EXISTS" ] || [ "$EXISTS" = "None" ]; then
    aws dynamodb update-table --table-name google_home_tokens --region "$REGION" \
      --attribute-definitions "AttributeName=${GSI},AttributeType=S" \
      --global-secondary-index-updates "[{\"Create\":{\"IndexName\":\"${GSI}-index\",\"KeySchema\":[{\"AttributeName\":\"${GSI}\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"KEYS_ONLY\"},\"BillingMode\":\"PAY_PER_REQUEST\"}}]" \
      > /dev/null 2>&1 && _info "GSI ${GSI}-index added to google_home_tokens" || true
  fi
done

_section "GOOGLE HOME — IAM ROLES"

GH_DDB_TABLES="arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_*"

ROLE_GH_START_ARN=$(create_or_update_role "digilux-gh-start-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_link_sessions\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_COMPLETE_ARN=$(create_or_update_role "digilux-gh-complete-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"DDB\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_link_sessions\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_STATUS_ARN=$(create_or_update_role "digilux-gh-status-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_UNLINK_ARN=$(create_or_update_role "digilux-gh-unlink-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${GOOGLE_SECRET_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_AUTHORIZE_ARN=$(create_or_update_role "digilux-gh-authorize-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_link_sessions\"},
    {\"Sid\":\"AuthCodes\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_auth_codes\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_TOKEN_ARN=$(create_or_update_role "digilux-gh-token-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"AuthCodes\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_auth_codes\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${GOOGLE_SECRET_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_FULFILLMENT_ARN=$(create_or_update_role "digilux-gh-fulfillment-role" "{
  \"Version\":\"2012-10-17\",
  \"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:Query\"],\"Resource\":[\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens\",\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/google_home_tokens/index/*\",\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_user_device_mapping\",\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/digilux_honeywell_user_device_mapping/index/*\"]},
    {\"Sid\":\"Logs\",${LOGS_STMT}},
    {\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

_section "GOOGLE HOME — PACKAGE + DEPLOY LAMBDAS"

GH_COMMON="DATA_REGION=${REGION},LOG_LEVEL=${LOG_LEVEL}"
GH_TABLES="GH_SESSIONS_TABLE=google_home_link_sessions,GH_AUTH_CODES_TABLE=google_home_auth_codes,GH_TOKENS_TABLE=google_home_tokens"
ALLOWED_REDIRECT_URIS="https://oauth-redirect.googleusercontent.com/r/${GOOGLE_AGENT_ID},https://oauth-redirect-sandbox.googleusercontent.com/r/${GOOGLE_AGENT_ID}"

ZIP=$(package_lambda "google_home_start" "${LAMBDA_DIR}/google_home_start")
deploy_lambda "google_home_start" "$ZIP" "$ROLE_GH_START_ARN" \
  "${GH_COMMON},GH_SESSIONS_TABLE=google_home_link_sessions,GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID},OAUTH_BASE_URL=${OAUTH_BASE_URL},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI}" 15

ZIP=$(package_lambda "google_home_complete" "${LAMBDA_DIR}/google_home_complete")
deploy_lambda "google_home_complete" "$ZIP" "$ROLE_GH_COMPLETE_ARN" \
  "${GH_COMMON},GH_SESSIONS_TABLE=google_home_link_sessions,GH_TOKENS_TABLE=google_home_tokens" 15

ZIP=$(package_lambda "google_home_status" "${LAMBDA_DIR}/google_home_status")
deploy_lambda "google_home_status" "$ZIP" "$ROLE_GH_STATUS_ARN" \
  "${GH_COMMON},GH_TOKENS_TABLE=google_home_tokens,GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID}" 15

ZIP=$(package_lambda "google_home_unlink" "${LAMBDA_DIR}/google_home_unlink")
deploy_lambda "google_home_unlink" "$ZIP" "$ROLE_GH_UNLINK_ARN" \
  "${GH_COMMON},GH_TOKENS_TABLE=google_home_tokens,GOOGLE_CLIENT_SECRET_ARN=${GOOGLE_SECRET_ARN}" 15

ZIP=$(package_lambda "google_home_oauth_authorize" "${LAMBDA_DIR}/google_home_oauth_authorize")
deploy_lambda "google_home_oauth_authorize" "$ZIP" "$ROLE_GH_AUTHORIZE_ARN" \
  "${GH_COMMON},GH_SESSIONS_TABLE=google_home_link_sessions,GH_AUTH_CODES_TABLE=google_home_auth_codes,GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID},COGNITO_REGION=${COGNITO_REGION},ALLOWED_REDIRECT_URIS=${ALLOWED_REDIRECT_URIS}" 15

ZIP=$(package_lambda "google_home_oauth_token" "${LAMBDA_DIR}/google_home_oauth_token")
deploy_lambda "google_home_oauth_token" "$ZIP" "$ROLE_GH_TOKEN_ARN" \
  "${GH_COMMON},GH_AUTH_CODES_TABLE=google_home_auth_codes,GH_TOKENS_TABLE=google_home_tokens,GOOGLE_CLIENT_SECRET_ARN=${GOOGLE_SECRET_ARN},GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID}" 15

ZIP=$(package_lambda "google_home_fulfillment" "${LAMBDA_DIR}/google_home_fulfillment")
deploy_lambda "google_home_fulfillment" "$ZIP" "$ROLE_GH_FULFILLMENT_ARN" \
  "${GH_COMMON},GH_TOKENS_TABLE=google_home_tokens,USER_DEVICE_MAPPING_TABLE=digilux_honeywell_user_device_mapping" 15

_section "GOOGLE HOME — API GATEWAY ROUTES"

VOICE_ID=$(get_or_create_resource "$V1_ID" "voice")
GH_ID=$(get_or_create_resource "$VOICE_ID" "google-home")
LINKING_ID=$(get_or_create_resource "$GH_ID" "account-linking")
DEEP_LINK_ID=$(get_or_create_resource "$LINKING_ID" "deep-link")
GH_START_ID=$(get_or_create_resource "$DEEP_LINK_ID" "start")
GH_COMPLETE_ID=$(get_or_create_resource "$DEEP_LINK_ID" "complete")

wire_method POST   "$GH_START_ID"    "google_home_start"    "COGNITO_USER_POOLS"
wire_method POST   "$GH_COMPLETE_ID" "google_home_complete"  "COGNITO_USER_POOLS"
wire_method GET    "$LINKING_ID"     "google_home_status"    "COGNITO_USER_POOLS"
wire_method DELETE "$LINKING_ID"     "google_home_unlink"    "COGNITO_USER_POOLS"

# OAuth + fulfillment routes (called by Google — no Cognito auth)
ROOT_GH_ID=$(get_or_create_resource "$ROOT_ID" "google-home")
OAUTH_ID=$(get_or_create_resource "$ROOT_GH_ID" "oauth")
AUTHORIZE_ID=$(get_or_create_resource "$OAUTH_ID" "authorize")
TOKEN_ID=$(get_or_create_resource "$OAUTH_ID" "token")
FULFILLMENT_ID=$(get_or_create_resource "$ROOT_GH_ID" "fulfillment")

wire_method GET    "$AUTHORIZE_ID"   "google_home_oauth_authorize" "NONE"
wire_method POST   "$AUTHORIZE_ID"   "google_home_oauth_authorize" "NONE"
wire_method POST   "$TOKEN_ID"       "google_home_oauth_token"     "NONE"
wire_method POST   "$FULFILLMENT_ID" "google_home_fulfillment"     "NONE"

_pass "Google Home API Gateway routes wired"

fi  # end DEPLOY_GOOGLE

# ── Deploy API stage ──────────────────────────────────────────────────────────
_section "DEPLOY API STAGE"
deploy_stage

# ── Smoke tests ───────────────────────────────────────────────────────────────
_section "SMOKE TESTS"
BASE="https://iot.digilux.co.in/smarthome"
sleep 5  # brief pause for Lambda cold-start readiness

smoke() {
  local LABEL="$1" METHOD="$2" URL="$3" EXPECTED="$4" BODY="${5:-}"
  local CODE
  if [ -n "$BODY" ]; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X "$METHOD" "$URL" \
      -H "Content-Type: application/json" -d "$BODY")
  else
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X "$METHOD" "$URL")
  fi
  [ "$CODE" = "$EXPECTED" ] && _pass "$LABEL (HTTP $CODE)" || _fail "$LABEL — expected $EXPECTED got $CODE"
}

if [ "$DEPLOY_ALEXA" -eq 1 ]; then
  smoke "Alexa start — no auth → 401"    POST   "${BASE}/api/v1/alexa/startAppToApp"   "401"
  smoke "Alexa complete — no auth → 401" POST   "${BASE}/api/v1/alexa/completeAppToApp" "401"
  smoke "Alexa status — no auth → 401"   GET    "${BASE}/api/v1/alexa/status"           "401"
  smoke "Alexa unlink — no auth → 401"   DELETE "${BASE}/api/v1/alexa/unlink"           "401"
  smoke "Alexa callback — no params"     GET    "${BASE}/alexa/callback"               "200"
fi

if [ "$DEPLOY_GOOGLE" -eq 1 ]; then
  smoke "GH start — no auth → 401"    POST   "${BASE}/api/v1/voice/google-home/account-linking/deep-link/start"    "401"
  smoke "GH complete — no auth → 401" POST   "${BASE}/api/v1/voice/google-home/account-linking/deep-link/complete" "401"
  smoke "GH status — no auth → 401"   GET    "${BASE}/api/v1/voice/google-home/account-linking"                    "401"
  smoke "GH unlink — no auth → 401"   DELETE "${BASE}/api/v1/voice/google-home/account-linking"                    "401"
  smoke "GH oauth authorize — GET"     GET    "${BASE}/google-home/oauth/authorize"                                "400"  # 400 = missing client_id — Lambda is reachable
  smoke "GH oauth token — POST"        POST   "${BASE}/google-home/oauth/token"                                    "400"  # 400 = missing params — Lambda is reachable
fi

# ── Results ───────────────────────────────────────────────────────────────────
_section "RESULTS"
TOTAL=$((PASS+FAIL))
echo ""
echo "  Total: $TOTAL   ✓ Passed: $PASS   ✗ Failed: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo "  Failed:"
  for c in "${FAILED_CHECKS[@]}"; do echo "    - $c"; done
  echo ""
  echo "  OVERALL: FAIL"
  exit 1
fi

echo "  OVERALL: PASS"
echo ""
echo "  Live endpoints:"
[ "$DEPLOY_ALEXA" -eq 1 ] && cat <<EOF
    POST   ${BASE}/api/v1/alexa/startAppToApp
    POST   ${BASE}/api/v1/alexa/completeAppToApp
    GET    ${BASE}/api/v1/alexa/status
    DELETE ${BASE}/api/v1/alexa/unlink
    GET    ${BASE}/alexa/callback
EOF
[ "$DEPLOY_GOOGLE" -eq 1 ] && cat <<EOF
    POST   ${BASE}/api/v1/voice/google-home/account-linking/deep-link/start
    POST   ${BASE}/api/v1/voice/google-home/account-linking/deep-link/complete
    GET    ${BASE}/api/v1/voice/google-home/account-linking
    DELETE ${BASE}/api/v1/voice/google-home/account-linking
    GET/POST ${BASE}/google-home/oauth/authorize
    POST   ${BASE}/google-home/oauth/token
    POST   ${BASE}/google-home/fulfillment
EOF
