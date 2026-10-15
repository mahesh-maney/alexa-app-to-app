#!/bin/bash
# deploy.sh — Config-driven deployment for Digilux / Honeywell voice integrations
#
# Deploys Alexa and/or Google Home Lambdas, wires API Gateway routes,
# creates DynamoDB tables and IAM roles — fully idempotent, no manual steps.
#
# Usage:
#   ./deploy/deploy.sh --config deploy/env.digilux.qa.json
#   ./deploy/deploy.sh --config deploy/env.honeywell.prod.json
#   ./deploy/deploy.sh --config deploy/env.honeywell.prod.json --dry-run
#   ./deploy/deploy.sh --config deploy/env.honeywell.prod.json --alexa
#   ./deploy/deploy.sh --config deploy/env.honeywell.prod.json --google
#
# Flags:
#   --config <file>   Path to JSON environment config file (required)
#   --alexa           Deploy Alexa only (overrides DEPLOY_TARGET in config)
#   --google          Deploy Google Home only (overrides DEPLOY_TARGET in config)
#   --all             Deploy both (overrides DEPLOY_TARGET in config)
#   --dry-run         Validate config and prerequisites, do not deploy
#
# Prerequisites:
#   aws, jq, python3, zip, curl

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0; FAILED_CHECKS=()

_pass()    { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); }
_fail()    { echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); FAILED_CHECKS+=("$1"); }
_warn()    { echo -e "  ${YELLOW}!${NC} $1"; }
_info()    { echo -e "  ${CYAN}→${NC} $1"; }
_section() { echo ""; echo -e "${BOLD}━━━ $1 ━━━${NC}"; }
_banner()  { echo ""; echo -e "${BOLD}${CYAN}$1${NC}"; echo ""; }

# ── Argument parsing ───────────────────────────────────────────────────────────
CONFIG_FILE=""
OVERRIDE_TARGET=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)   CONFIG_FILE="$2"; shift 2 ;;
    --alexa)    OVERRIDE_TARGET="alexa"; shift ;;
    --google)   OVERRIDE_TARGET="google"; shift ;;
    --all)      OVERRIDE_TARGET="all"; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 --config <env-file.json> [--alexa|--google|--all] [--dry-run]"
      exit 1
      ;;
  esac
done

if [ -z "$CONFIG_FILE" ]; then
  echo -e "${RED}ERROR:${NC} --config <file> is required"
  echo "Usage: $0 --config deploy/env.digilux.qa.json"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo -e "${RED}ERROR:${NC} Config file not found: $CONFIG_FILE"
  exit 1
fi

# ── Load config ────────────────────────────────────────────────────────────────
cfg() { jq -r ".$1 // empty" "$CONFIG_FILE"; }

ENV_LABEL=$(cfg ENV_LABEL)
DEPLOY_TARGET="${OVERRIDE_TARGET:-$(cfg DEPLOY_TARGET)}"
ROLE_PREFIX=$(cfg ROLE_PREFIX)

AWS_REGION=$(cfg AWS_REGION)
API_GATEWAY_ID=$(cfg API_GATEWAY_ID)
API_STAGE=$(cfg API_STAGE)
API_BASE_URL=$(cfg API_BASE_URL)

COGNITO_USER_POOL_ID=$(cfg COGNITO_USER_POOL_ID)
COGNITO_REGION=$(cfg COGNITO_REGION)
COGNITO_CLIENT_ID=$(cfg COGNITO_CLIENT_ID)

ALEXA_SESSIONS_TABLE=$(cfg ALEXA_SESSIONS_TABLE)
ALEXA_TOKENS_TABLE=$(cfg ALEXA_TOKENS_TABLE)
GH_SESSIONS_TABLE=$(cfg GH_SESSIONS_TABLE)
GH_AUTH_CODES_TABLE=$(cfg GH_AUTH_CODES_TABLE)
GH_TOKENS_TABLE=$(cfg GH_TOKENS_TABLE)
GH_OAUTH_TRANSACTIONS_TABLE=$(cfg GH_OAUTH_TRANSACTIONS_TABLE)
USER_DEVICE_MAPPING_TABLE=$(cfg USER_DEVICE_MAPPING_TABLE)

LWA_SECRET_ARN=$(cfg LWA_SECRET_ARN)
LWA_SECRET_REGION=$(cfg LWA_SECRET_REGION)
KMS_KEY_ALIAS=$(cfg KMS_KEY_ALIAS)

ALEXA_SKILL_ID=$(cfg ALEXA_SKILL_ID)
ALEXA_SKILL_STAGE=$(cfg ALEXA_SKILL_STAGE)
ALEXA_REDIRECT_URI=$(cfg ALEXA_REDIRECT_URI)
ALEXA_REDIRECT_HOSTS=$(cfg ALEXA_REDIRECT_HOSTS)

GOOGLE_SECRET_ARN=$(cfg GOOGLE_SECRET_ARN)
GOOGLE_SECRET_REGION=$(cfg GOOGLE_SECRET_REGION)
GOOGLE_AGENT_ID=$(cfg GOOGLE_AGENT_ID)
GOOGLE_CLIENT_ID=$(cfg GOOGLE_CLIENT_ID)
GOOGLE_REDIRECT_URI=$(cfg GOOGLE_REDIRECT_URI)
COGNITO_HOSTED_UI_DOMAIN=$(cfg COGNITO_HOSTED_UI_DOMAIN)
COGNITO_CALLBACK_URI=$(cfg COGNITO_CALLBACK_URI)
TRANSACTION_TTL_SECONDS=$(cfg TRANSACTION_TTL_SECONDS)

APP_DEEP_LINK_SCHEME=$(cfg APP_DEEP_LINK_SCHEME)
APP_NAME=$(cfg APP_NAME)
DEVICE_MANUFACTURER=$(cfg DEVICE_MANUFACTURER)

LOG_LEVEL=$(cfg LOG_LEVEL)
SESSION_TTL_SECONDS=$(cfg SESSION_TTL_SECONDS)
AUTH_CODE_TTL_SECONDS=$(cfg AUTH_CODE_TTL_SECONDS)
ACCESS_TOKEN_TTL_SECONDS=$(cfg ACCESS_TOKEN_TTL_SECONDS)
REFRESH_TOKEN_TTL_SECONDS=$(cfg REFRESH_TOKEN_TTL_SECONDS)
GH_TOKENS_ACCESS_TOKEN_INDEX=$(cfg GH_TOKENS_ACCESS_TOKEN_INDEX)
GH_DEEP_LINK_BASE_URL=$(cfg GH_DEEP_LINK_BASE_URL)
GH_DEEP_LINK_PATH=$(cfg GH_DEEP_LINK_PATH)
GOOGLE_SA_KEY_SECRET_ARN=$(cfg GOOGLE_SA_KEY_SECRET_ARN)

DEPLOY_ALEXA=0; DEPLOY_GOOGLE=0
case "$DEPLOY_TARGET" in
  alexa)  DEPLOY_ALEXA=1 ;;
  google) DEPLOY_GOOGLE=1 ;;
  all)    DEPLOY_ALEXA=1; DEPLOY_GOOGLE=1 ;;
  *)
    echo -e "${RED}ERROR:${NC} DEPLOY_TARGET must be 'alexa', 'google', or 'all' — got '$DEPLOY_TARGET'"
    exit 1
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAMBDA_DIR="${REPO_ROOT}/lambdas"
BUILD_DIR="/tmp/deploy_${ENV_LABEL}_build"

_banner "DEPLOY — ${ENV_LABEL}  [target: ${DEPLOY_TARGET}]"
_info "Config:    $CONFIG_FILE"
_info "Region:    $AWS_REGION"
_info "API Base:  $API_BASE_URL"

# ── Step 1: Validate config — no FILL_IN values ───────────────────────────────
_section "STEP 1 — CONFIG VALIDATION"

FILL_IN_KEYS=$(jq -r 'to_entries[] | select(.value == "FILL_IN") | .key' "$CONFIG_FILE" 2>/dev/null || true)
if [ -n "$FILL_IN_KEYS" ]; then
  echo -e "  ${RED}ERROR: The following values are still set to FILL_IN in $CONFIG_FILE:${NC}"
  while IFS= read -r key; do
    echo -e "    ${RED}✗${NC} $key"
  done <<< "$FILL_IN_KEYS"
  echo ""
  echo "  Fill in all FILL_IN values before running deploy."
  exit 1
fi
_pass "No FILL_IN placeholders — all values provided"

# Validate deploy-target specific required values
validate_required() {
  local KEY="$1" VAL="$2"
  if [ -z "$VAL" ]; then
    _fail "Required config key missing or empty: $KEY"
  else
    _pass "$KEY set"
  fi
}

validate_required "AWS_REGION"     "$AWS_REGION"
validate_required "API_BASE_URL"   "$API_BASE_URL"
validate_required "API_STAGE"      "$API_STAGE"
validate_required "ROLE_PREFIX"    "$ROLE_PREFIX"
validate_required "COGNITO_USER_POOL_ID" "$COGNITO_USER_POOL_ID"
validate_required "COGNITO_REGION"       "$COGNITO_REGION"
validate_required "COGNITO_CLIENT_ID"    "$COGNITO_CLIENT_ID"

if [ "$DEPLOY_ALEXA" -eq 1 ]; then
  validate_required "LWA_SECRET_ARN"      "$LWA_SECRET_ARN"
  validate_required "LWA_SECRET_REGION"   "$LWA_SECRET_REGION"
  validate_required "ALEXA_SKILL_ID"      "$ALEXA_SKILL_ID"
  validate_required "ALEXA_REDIRECT_URI"  "$ALEXA_REDIRECT_URI"
  validate_required "ALEXA_REDIRECT_HOSTS" "$ALEXA_REDIRECT_HOSTS"
  validate_required "ALEXA_SESSIONS_TABLE" "$ALEXA_SESSIONS_TABLE"
  validate_required "ALEXA_TOKENS_TABLE"   "$ALEXA_TOKENS_TABLE"
fi

if [ "$DEPLOY_GOOGLE" -eq 1 ]; then
  validate_required "GOOGLE_SECRET_ARN"    "$GOOGLE_SECRET_ARN"
  validate_required "GOOGLE_SECRET_REGION" "$GOOGLE_SECRET_REGION"
  validate_required "GOOGLE_AGENT_ID"      "$GOOGLE_AGENT_ID"
  validate_required "GOOGLE_CLIENT_ID"     "$GOOGLE_CLIENT_ID"
  validate_required "GOOGLE_REDIRECT_URI"  "$GOOGLE_REDIRECT_URI"
  validate_required "GH_SESSIONS_TABLE"    "$GH_SESSIONS_TABLE"
  validate_required "GH_AUTH_CODES_TABLE"  "$GH_AUTH_CODES_TABLE"
  validate_required "GH_TOKENS_TABLE"      "$GH_TOKENS_TABLE"
  validate_required "USER_DEVICE_MAPPING_TABLE" "$USER_DEVICE_MAPPING_TABLE"
  validate_required "APP_NAME"             "$APP_NAME"
  validate_required "DEVICE_MANUFACTURER"  "$DEVICE_MANUFACTURER"
fi

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo -e "  ${RED}Config validation failed — fix the above before deploying.${NC}"
  exit 1
fi

# ── Step 2: Preflight — tools + AWS credentials ───────────────────────────────
_section "STEP 2 — PREFLIGHT CHECKS"

for tool in aws jq python3 zip curl; do
  command -v "$tool" &>/dev/null \
    && _pass "$tool available" \
    || { _fail "$tool not found — install it and re-run"; exit 1; }
done

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  && _pass "AWS credentials valid (account: $ACCOUNT_ID, region: $AWS_REGION)" \
  || { _fail "AWS credentials not configured — run 'aws configure' or set AWS_PROFILE"; exit 1; }

if [ "$DEPLOY_ALEXA" -eq 1 ]; then
  aws secretsmanager describe-secret --secret-id "$LWA_SECRET_ARN" \
    --region "$LWA_SECRET_REGION" --query 'Name' --output text &>/dev/null \
    && _pass "LWA secret accessible" \
    || _fail "LWA secret not accessible: $LWA_SECRET_ARN (region: $LWA_SECRET_REGION)"
fi

if [ "$DEPLOY_GOOGLE" -eq 1 ]; then
  aws secretsmanager describe-secret --secret-id "$GOOGLE_SECRET_ARN" \
    --region "$GOOGLE_SECRET_REGION" --query 'Name' --output text &>/dev/null \
    && _pass "Google OAuth secret accessible" \
    || _fail "Google secret not accessible: $GOOGLE_SECRET_ARN (region: $GOOGLE_SECRET_REGION)"

  _warn "CRITICAL: Verify client_secret in Secrets Manager EXACTLY matches Google Console"
  _warn "A mismatch causes 24-48h Google backoff. Confirm before testing end-to-end."
fi

if [ "$FAIL" -gt 0 ]; then
  echo ""
  echo -e "  ${RED}Preflight failed — fix the above before deploying.${NC}"
  exit 1
fi

[ "$DRY_RUN" -eq 1 ] && {
  echo ""
  echo -e "  ${GREEN}DRY RUN — config and prerequisites OK. Exiting without deploying.${NC}"
  exit 0
}

# ── Shared helpers ─────────────────────────────────────────────────────────────

package_lambda() {
  local NAME="$1" SRC_DIR="$2"
  local OUT="${BUILD_DIR}/${NAME}.zip"
  mkdir -p "$BUILD_DIR"
  rm -f "$OUT"
  # Install Python requirements if present (needed for lambdas with native deps,
  # e.g. google_home_unlink uses 'cryptography' for Home Graph JWT signing).
  if [ -f "${SRC_DIR}/requirements.txt" ]; then
    local DEPS_DIR="${BUILD_DIR}/${NAME}_deps"
    rm -rf "$DEPS_DIR" && mkdir -p "$DEPS_DIR"
    # Prefer Linux/Lambda-compatible binary wheels; fall back to local build.
    pip install -q -r "${SRC_DIR}/requirements.txt" -t "$DEPS_DIR" \
        --platform manylinux2014_x86_64 --python-version 3.12 \
        --only-binary=:all: --implementation cp 2>/dev/null \
      || pip install -q -r "${SRC_DIR}/requirements.txt" -t "$DEPS_DIR" 2>/dev/null
    (cd "$DEPS_DIR" && zip -qr "$OUT" . \
        -x "*.pyc" "__pycache__/*" "*.dist-info/*" "*.egg-info/*")
    echo -e "  ${CYAN}→${NC} Packaged ${NAME} deps ($(du -sh "$DEPS_DIR" | cut -f1))" >&2
  fi
  (cd "$SRC_DIR" && zip -qr "$OUT" . -x "__pycache__/*" "*.pyc" "tests/*" "requirements.txt")
  echo -e "  ${CYAN}→${NC} Packaged ${NAME} ($(du -sh "$OUT" | cut -f1))" >&2
  echo "$OUT"
}

deploy_lambda() {
  local NAME="$1" ZIP="$2" ROLE_ARN="$3" ENV_JSON="$4" TIMEOUT="${5:-15}"
  if aws lambda get-function --function-name "$NAME" --region "$AWS_REGION" &>/dev/null; then
    aws lambda update-function-code \
      --function-name "$NAME" --zip-file "fileb://${ZIP}" \
      --region "$AWS_REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-updated --function-name "$NAME" --region "$AWS_REGION"
    aws lambda update-function-configuration \
      --function-name "$NAME" \
      --role "$ROLE_ARN" \
      --timeout "$TIMEOUT" \
      --memory-size 256 \
      --environment "$ENV_JSON" \
      --tracing-config Mode=Active \
      --region "$AWS_REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-updated --function-name "$NAME" --region "$AWS_REGION"
    _pass "Lambda $NAME updated"
  else
    aws lambda create-function \
      --function-name "$NAME" \
      --runtime python3.12 \
      --role "$ROLE_ARN" \
      --handler lambda_function.lambda_handler \
      --zip-file "fileb://${ZIP}" \
      --timeout "$TIMEOUT" \
      --memory-size 256 \
      --environment "$ENV_JSON" \
      --tracing-config Mode=Active \
      --region "$AWS_REGION" --output text --query 'FunctionName' > /dev/null
    aws lambda wait function-active --function-name "$NAME" --region "$AWS_REGION"
    _pass "Lambda $NAME created"
  fi
  aws logs put-retention-policy \
    --log-group-name "/aws/lambda/${NAME}" \
    --retention-in-days 30 \
    --region "$AWS_REGION" 2>/dev/null || true
}

env_json() {
  # Build {"Variables": {...}} JSON from key=value pairs
  # Usage: env_json "KEY1=val1" "KEY2=val2" ...
  local vars=()
  for pair in "$@"; do
    local k="${pair%%=*}"
    local v="${pair#*=}"
    vars+=("$(jq -n --arg k "$k" --arg v "$v" '{($k): $v}')")
  done
  local merged
  merged=$(echo "${vars[@]}" | jq -s 'add')
  echo "{\"Variables\": $merged}"
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
    sleep 10  # IAM propagation
    _info "IAM role $ROLE created"
  fi
  aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text
}

ensure_ddb_table() {
  local TABLE="$1" PK="$2" PK_TYPE="${3:-S}"
  if aws dynamodb describe-table --table-name "$TABLE" --region "$AWS_REGION" &>/dev/null; then
    _pass "DynamoDB table $TABLE already exists"
  else
    aws dynamodb create-table --table-name "$TABLE" \
      --attribute-definitions "AttributeName=${PK},AttributeType=${PK_TYPE}" \
      --key-schema "AttributeName=${PK},KeyType=HASH" \
      --billing-mode PAY_PER_REQUEST \
      --region "$AWS_REGION" --output text --query 'TableDescription.TableName' > /dev/null
    aws dynamodb wait table-exists --table-name "$TABLE" --region "$AWS_REGION"
    aws dynamodb update-time-to-live --table-name "$TABLE" --region "$AWS_REGION" \
      --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null 2>&1 || true
    _pass "DynamoDB table $TABLE created"
  fi
}

ensure_ddb_gsi() {
  local TABLE="$1" GSI_NAME="$2" GSI_KEY="$3"
  local EXISTS
  EXISTS=$(aws dynamodb describe-table --table-name "$TABLE" --region "$AWS_REGION" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='${GSI_NAME}'].IndexName" \
    --output text 2>/dev/null || true)
  if [ -z "$EXISTS" ] || [ "$EXISTS" = "None" ]; then
    aws dynamodb update-table --table-name "$TABLE" --region "$AWS_REGION" \
      --attribute-definitions "AttributeName=${GSI_KEY},AttributeType=S" \
      --global-secondary-index-updates \
        "[{\"Create\":{\"IndexName\":\"${GSI_NAME}\",\"KeySchema\":[{\"AttributeName\":\"${GSI_KEY}\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"KEYS_ONLY\"},\"BillingMode\":\"PAY_PER_REQUEST\"}}]" \
      > /dev/null 2>&1 \
      && _info "GSI $GSI_NAME added to $TABLE" || true
  fi
}

get_or_create_resource() {
  local PARENT_ID="$1" PATH_PART="$2"
  local EXISTING
  EXISTING=$(aws apigateway get-resources --rest-api-id "$API_GATEWAY_ID" --region "$AWS_REGION" \
    --query "items[?parentId=='${PARENT_ID}' && pathPart=='${PATH_PART}'].id" \
    --output text 2>/dev/null || true)
  if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
    echo "$EXISTING"
  else
    aws apigateway create-resource \
      --rest-api-id "$API_GATEWAY_ID" --parent-id "$PARENT_ID" \
      --path-part "$PATH_PART" --region "$AWS_REGION" \
      --query 'id' --output text
  fi
}

wire_method() {
  local HTTP_METHOD="$1" RESOURCE_ID="$2" LAMBDA_NAME="$3" AUTH="${4:-COGNITO_USER_POOLS}"
  local LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
  local URI="arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations"

  local AUTH_ARGS="--authorization-type $AUTH"
  [ "$AUTH" = "COGNITO_USER_POOLS" ] && AUTH_ARGS="$AUTH_ARGS --authorizer-id $AUTHORIZER_ID"

  aws apigateway put-method \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    $AUTH_ARGS \
    --region "$AWS_REGION" &>/dev/null || true

  aws apigateway put-integration \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" --type AWS_PROXY \
    --integration-http-method POST --uri "$URI" \
    --region "$AWS_REGION" &>/dev/null || true

  # Lambda invoke permission
  local SID="${LAMBDA_NAME}-apigw-${HTTP_METHOD,,}-${RESOURCE_ID}"
  aws lambda remove-permission --function-name "$LAMBDA_NAME" \
    --statement-id "$SID" --region "$AWS_REGION" &>/dev/null || true
  aws lambda add-permission --function-name "$LAMBDA_NAME" \
    --statement-id "$SID" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_GATEWAY_ID}/*/${HTTP_METHOD}/*" \
    --region "$AWS_REGION" &>/dev/null || true

  # CORS OPTIONS
  aws apigateway put-method \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --authorization-type NONE \
    --region "$AWS_REGION" &>/dev/null || true
  aws apigateway put-integration \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --type MOCK \
    --request-templates '{"application/json":"{\"statusCode\":200}"}' \
    --region "$AWS_REGION" &>/dev/null || true
  aws apigateway put-method-response \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters \
      '{"method.response.header.Access-Control-Allow-Headers":false,"method.response.header.Access-Control-Allow-Methods":false,"method.response.header.Access-Control-Allow-Origin":false}' \
    --region "$AWS_REGION" &>/dev/null || true
  aws apigateway put-integration-response \
    --rest-api-id "$API_GATEWAY_ID" --resource-id "$RESOURCE_ID" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters \
      '{"method.response.header.Access-Control-Allow-Headers":"'"'"'Content-Type,Authorization'"'"'","method.response.header.Access-Control-Allow-Methods":"'"'"'OPTIONS,GET,POST,DELETE'"'"'","method.response.header.Access-Control-Allow-Origin":"'"'"'*'"'"'"}' \
    --region "$AWS_REGION" &>/dev/null || true
}

smoke() {
  local LABEL="$1" METHOD="$2" URL="$3" EXPECTED="$4" BODY="${5:-}"
  local CODE
  if [ -n "$BODY" ]; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X "$METHOD" "$URL" \
      -H "Content-Type: application/json" -d "$BODY")
  else
    CODE=$(curl -s -o /dev/null -w "%{http_code}" -X "$METHOD" "$URL")
  fi
  [ "$CODE" = "$EXPECTED" ] \
    && _pass "$LABEL (HTTP $CODE)" \
    || _fail "$LABEL — expected HTTP $EXPECTED, got $CODE"
}

# Shared IAM policy fragments
LOGS_STMT="\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogGroup\",\"logs:CreateLogStream\",\"logs:PutLogEvents\"],\"Resource\":\"arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/*\""
XRAY_STMT="\"Effect\":\"Allow\",\"Action\":[\"xray:PutTraceSegments\",\"xray:PutTelemetryRecords\"],\"Resource\":\"*\""

# ── Step 3: API Gateway ────────────────────────────────────────────────────────
_section "STEP 3 — API GATEWAY"

if [ -z "$API_GATEWAY_ID" ]; then
  _info "API_GATEWAY_ID not set — creating new REST API"
  API_GATEWAY_ID=$(aws apigateway create-rest-api \
    --name "${ROLE_PREFIX}-smarthome-api" \
    --description "Smart Home voice integration API (${ENV_LABEL})" \
    --endpoint-configuration types=REGIONAL \
    --region "$AWS_REGION" \
    --query 'id' --output text)
  _pass "API Gateway created: $API_GATEWAY_ID"
  _warn "Add this to your config: \"API_GATEWAY_ID\": \"$API_GATEWAY_ID\""
else
  aws apigateway get-rest-api --rest-api-id "$API_GATEWAY_ID" --region "$AWS_REGION" \
    --query 'id' --output text &>/dev/null \
    && _pass "API Gateway $API_GATEWAY_ID reachable" \
    || { _fail "API Gateway $API_GATEWAY_ID not found in $AWS_REGION"; exit 1; }
fi

# Cognito authorizer — get or create
AUTHORIZER_ID=$(aws apigateway get-authorizers --rest-api-id "$API_GATEWAY_ID" \
  --region "$AWS_REGION" \
  --query "items[?name=='cognito_authorizer'].id" --output text 2>/dev/null || true)

if [ -z "$AUTHORIZER_ID" ] || [ "$AUTHORIZER_ID" = "None" ]; then
  COGNITO_POOL_ARN="arn:aws:cognito-idp:${COGNITO_REGION}:${ACCOUNT_ID}:userpool/${COGNITO_USER_POOL_ID}"
  AUTHORIZER_ID=$(aws apigateway create-authorizer \
    --rest-api-id "$API_GATEWAY_ID" \
    --name "cognito_authorizer" \
    --type COGNITO_USER_POOLS \
    --provider-arns "$COGNITO_POOL_ARN" \
    --identity-source "method.request.header.Authorization" \
    --region "$AWS_REGION" \
    --query 'id' --output text)
  _pass "Cognito authorizer created: $AUTHORIZER_ID"
else
  _pass "Cognito authorizer exists: $AUTHORIZER_ID"
fi

ROOT_ID=$(aws apigateway get-resources --rest-api-id "$API_GATEWAY_ID" \
  --region "$AWS_REGION" --query 'items[?path==`/`].id' --output text)

API_ID=$(get_or_create_resource "$ROOT_ID" "api")
V1_ID=$(get_or_create_resource "$API_ID" "v1")

# ══════════════════════════════════════════════════════════════════════════════
# ALEXA DEPLOYMENT
# ══════════════════════════════════════════════════════════════════════════════
if [ "$DEPLOY_ALEXA" -eq 1 ]; then

# ── Step 4a: DynamoDB ─────────────────────────────────────────────────────────
_section "STEP 4a — ALEXA DynamoDB TABLES"

ensure_ddb_table "$ALEXA_SESSIONS_TABLE" "state"
ensure_ddb_table "$ALEXA_TOKENS_TABLE"   "userId"

# KMS key
_section "STEP 5a — ALEXA KMS KEY"
KMS_KEY_ARN=$(aws kms list-aliases --region "$AWS_REGION" \
  --query "Aliases[?AliasName=='${KMS_KEY_ALIAS}'].TargetKeyId" \
  --output text 2>/dev/null || true)

if [ -n "$KMS_KEY_ARN" ] && [ "$KMS_KEY_ARN" != "None" ]; then
  KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ARN}"
  _pass "KMS key found: $KMS_KEY_ARN"
else
  _info "KMS key alias '$KMS_KEY_ALIAS' not found — creating"
  KMS_KEY_ID=$(aws kms create-key \
    --description "Encryption key for Alexa LWA tokens (${ENV_LABEL})" \
    --region "$AWS_REGION" \
    --query 'KeyMetadata.KeyId' --output text)
  aws kms create-alias \
    --alias-name "$KMS_KEY_ALIAS" \
    --target-key-id "$KMS_KEY_ID" \
    --region "$AWS_REGION"
  KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}"
  _pass "KMS key created: $KMS_KEY_ARN"
fi

# ── Step 6a: IAM Roles ────────────────────────────────────────────────────────
_section "STEP 6a — ALEXA IAM ROLES"

DDB_SESSIONS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ALEXA_SESSIONS_TABLE}"
DDB_TOKENS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ALEXA_TOKENS_TABLE}"

ROLE_START_ARN=$(create_or_update_role "${ROLE_PREFIX}-alexa-start-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\"],\"Resource\":\"${DDB_SESSIONS_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:GenerateDataKey\",\"kms:Decrypt\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_COMPLETE_ARN=$(create_or_update_role "${ROLE_PREFIX}-alexa-complete-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"${DDB_SESSIONS_ARN}\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"${DDB_TOKENS_ARN}\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\",\"kms:GenerateDataKey\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_CALLBACK_ARN=$(create_or_update_role "${ROLE_PREFIX}-alexa-callback-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_UNLINK_ARN=$(create_or_update_role "${ROLE_PREFIX}-alexa-unlink-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"${DDB_TOKENS_ARN}\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_STATUS_ARN=$(create_or_update_role "${ROLE_PREFIX}-alexa-status-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"${DDB_TOKENS_ARN}\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${LWA_SECRET_ARN}\"},
    {\"Sid\":\"Kms\",\"Effect\":\"Allow\",\"Action\":[\"kms:Decrypt\"],\"Resource\":\"${KMS_KEY_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

# ── Step 7a: Package + Deploy Lambdas ─────────────────────────────────────────
_section "STEP 7a — ALEXA LAMBDA DEPLOY"

ALEXA_COMMON=(
  "DATA_REGION=${AWS_REGION}"
  "LOG_LEVEL=${LOG_LEVEL}"
  "COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID}"
  "COGNITO_REGION=${COGNITO_REGION}"
  "COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}"
)

ZIP=$(package_lambda "alexa_start_app_to_app" "${LAMBDA_DIR}/alexa_start_app_to_app")
deploy_lambda "alexa_start_app_to_app" "$ZIP" "$ROLE_START_ARN" \
  "$(env_json "${ALEXA_COMMON[@]}" \
    "SESSION_TABLE=${ALEXA_SESSIONS_TABLE}" \
    "REDIRECT_URI=${ALEXA_REDIRECT_URI}" \
    "ALLOWED_REDIRECT_HOSTS=${ALEXA_REDIRECT_HOSTS}" \
    "KMS_KEY_ARN=${KMS_KEY_ARN}" \
    "SESSION_TTL_SECONDS=${SESSION_TTL_SECONDS}")" 15

ZIP=$(package_lambda "alexa_complete_app_to_app" "${LAMBDA_DIR}/alexa_complete_app_to_app")
deploy_lambda "alexa_complete_app_to_app" "$ZIP" "$ROLE_COMPLETE_ARN" \
  "$(env_json "${ALEXA_COMMON[@]}" \
    "SESSION_TABLE=${ALEXA_SESSIONS_TABLE}" \
    "LWA_TOKENS_TABLE=${ALEXA_TOKENS_TABLE}" \
    "LWA_SECRET_ARN=${LWA_SECRET_ARN}" \
    "LWA_SECRET_REGION=${LWA_SECRET_REGION}" \
    "KMS_KEY_ARN=${KMS_KEY_ARN}" \
    "ALEXA_SKILL_ID=${ALEXA_SKILL_ID}" \
    "ALEXA_SKILL_STAGE=${ALEXA_SKILL_STAGE}" \
    "REDIRECT_URI=${ALEXA_REDIRECT_URI}")" 30

ZIP=$(package_lambda "alexa_callback" "${LAMBDA_DIR}/alexa_callback")
deploy_lambda "alexa_callback" "$ZIP" "$ROLE_CALLBACK_ARN" \
  "$(env_json "${ALEXA_COMMON[@]}" \
    "APP_DEEP_LINK_SCHEME=${APP_DEEP_LINK_SCHEME}")" 10

ZIP=$(package_lambda "alexa_unlink" "${LAMBDA_DIR}/alexa_unlink")
deploy_lambda "alexa_unlink" "$ZIP" "$ROLE_UNLINK_ARN" \
  "$(env_json "${ALEXA_COMMON[@]}" \
    "LWA_TOKENS_TABLE=${ALEXA_TOKENS_TABLE}" \
    "USER_DEVICE_MAPPING_TABLE=${USER_DEVICE_MAPPING_TABLE}" \
    "LWA_SECRET_ARN=${LWA_SECRET_ARN}" \
    "LWA_SECRET_REGION=${LWA_SECRET_REGION}" \
    "KMS_KEY_ARN=${KMS_KEY_ARN}" \
    "ALEXA_SKILL_ID=${ALEXA_SKILL_ID}")" 15

ZIP=$(package_lambda "alexa_link_status" "${LAMBDA_DIR}/alexa_link_status")
deploy_lambda "alexa_link_status" "$ZIP" "$ROLE_STATUS_ARN" \
  "$(env_json "${ALEXA_COMMON[@]}" \
    "LWA_TOKENS_TABLE=${ALEXA_TOKENS_TABLE}" \
    "USER_DEVICE_MAPPING_TABLE=${USER_DEVICE_MAPPING_TABLE}" \
    "LWA_SECRET_ARN=${LWA_SECRET_ARN}" \
    "LWA_SECRET_REGION=${LWA_SECRET_REGION}" \
    "KMS_KEY_ARN=${KMS_KEY_ARN}")" 15

# ── Step 8a: API Gateway Routes ───────────────────────────────────────────────
_section "STEP 8a — ALEXA API GATEWAY ROUTES"

ALEXA_ID=$(get_or_create_resource "$V1_ID"    "alexa")
STATUS_ID=$(get_or_create_resource "$ALEXA_ID" "status")
START_ID=$(get_or_create_resource  "$ALEXA_ID" "startAppToApp")
COMPLETE_ID=$(get_or_create_resource "$ALEXA_ID" "completeAppToApp")
UNLINK_ID=$(get_or_create_resource "$ALEXA_ID"  "unlink")
ROOT_ALEXA_ID=$(get_or_create_resource "$ROOT_ID"      "alexa")
CALLBACK_ID=$(get_or_create_resource   "$ROOT_ALEXA_ID" "callback")
SKILL_EVT_ID=$(get_or_create_resource  "$ROOT_ALEXA_ID" "skill-event")

wire_method GET    "$STATUS_ID"     "alexa_link_status"        "COGNITO_USER_POOLS"
wire_method POST   "$START_ID"      "alexa_start_app_to_app"   "COGNITO_USER_POOLS"
wire_method POST   "$COMPLETE_ID"   "alexa_complete_app_to_app" "COGNITO_USER_POOLS"
wire_method DELETE "$UNLINK_ID"     "alexa_unlink"             "COGNITO_USER_POOLS"
wire_method GET    "$CALLBACK_ID"   "alexa_callback"           "NONE"
wire_method POST   "$SKILL_EVT_ID"  "alexa_skill_events"       "NONE"

_pass "Alexa API Gateway routes wired"

fi  # end DEPLOY_ALEXA

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE HOME DEPLOYMENT
# ══════════════════════════════════════════════════════════════════════════════
if [ "$DEPLOY_GOOGLE" -eq 1 ]; then

# ── Step 4b: DynamoDB ─────────────────────────────────────────────────────────
_section "STEP 4b — GOOGLE HOME DynamoDB TABLES"

ensure_ddb_table "$GH_SESSIONS_TABLE"              "state"
ensure_ddb_table "$GH_AUTH_CODES_TABLE"            "code"
ensure_ddb_table "$GH_TOKENS_TABLE"                "userId"
ensure_ddb_table "$GH_OAUTH_TRANSACTIONS_TABLE"    "state"

ensure_ddb_gsi "$GH_TOKENS_TABLE" "${GH_TOKENS_ACCESS_TOKEN_INDEX}" "accessToken"
ensure_ddb_gsi "$GH_TOKENS_TABLE" "refreshToken-index"              "refreshToken"

# ── Step 5b: IAM Roles ────────────────────────────────────────────────────────
_section "STEP 5b — GOOGLE HOME IAM ROLES"

DDB_GH_SESSIONS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${GH_SESSIONS_TABLE}"
DDB_GH_CODES_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${GH_AUTH_CODES_TABLE}"
DDB_GH_TOKENS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}"
DDB_GH_TRANSACTIONS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${GH_OAUTH_TRANSACTIONS_TABLE}"
DDB_DEVICE_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${USER_DEVICE_MAPPING_TABLE}"

ROLE_GH_START_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-start-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\"],\"Resource\":\"${DDB_GH_SESSIONS_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_COMPLETE_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-complete-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Sessions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"${DDB_GH_SESSIONS_ARN}\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"${DDB_GH_TOKENS_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_STATUS_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-status-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\"],\"Resource\":\"${DDB_GH_TOKENS_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

# Build the Secrets Manager resource list for the unlink role.
# Includes the SA key secret when GOOGLE_SA_KEY_SECRET_ARN is configured.
if [ -n "${GOOGLE_SA_KEY_SECRET_ARN:-}" ]; then
  _GH_UNLINK_SECRET_RESOURCE="[\"${GOOGLE_SECRET_ARN}\",\"${GOOGLE_SA_KEY_SECRET_ARN}\"]"
else
  _GH_UNLINK_SECRET_RESOURCE="\"${GOOGLE_SECRET_ARN}\""
fi

ROLE_GH_UNLINK_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-unlink-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"${DDB_GH_TOKENS_ARN}\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":${_GH_UNLINK_SECRET_RESOURCE}},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_AUTHORIZE_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-authorize-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Transactions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\"],\"Resource\":\"${DDB_GH_TRANSACTIONS_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_CALLBACK_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-callback-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Transactions\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:UpdateItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"${DDB_GH_TRANSACTIONS_ARN}\"},
    {\"Sid\":\"AuthCodes\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\"],\"Resource\":\"${DDB_GH_CODES_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_TOKEN_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-token-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"AuthCodes\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:DeleteItem\"],\"Resource\":\"${DDB_GH_CODES_ARN}\"},
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:PutItem\",\"dynamodb:GetItem\",\"dynamodb:UpdateItem\"],\"Resource\":\"${DDB_GH_TOKENS_ARN}\"},
    {\"Sid\":\"Secret\",\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${GOOGLE_SECRET_ARN}\"},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

ROLE_GH_FULFILLMENT_ARN=$(create_or_update_role "${ROLE_PREFIX}-gh-fulfillment-role" "{
  \"Version\":\"2012-10-17\",\"Statement\":[
    {\"Sid\":\"Tokens\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:GetItem\",\"dynamodb:Query\",\"dynamodb:DeleteItem\"],\"Resource\":[\"${DDB_GH_TOKENS_ARN}\",\"${DDB_GH_TOKENS_ARN}/index/*\"]},
    {\"Sid\":\"DeviceMapping\",\"Effect\":\"Allow\",\"Action\":[\"dynamodb:Query\"],\"Resource\":[\"${DDB_DEVICE_ARN}\",\"${DDB_DEVICE_ARN}/index/*\"]},
    {\"Sid\":\"Logs\",${LOGS_STMT}},{\"Sid\":\"XRay\",${XRAY_STMT}}
  ]
}")

# ── Step 6b: Package + Deploy Lambdas ─────────────────────────────────────────
_section "STEP 6b — GOOGLE HOME LAMBDA DEPLOY"

GH_COMMON=(
  "DATA_REGION=${AWS_REGION}"
  "LOG_LEVEL=${LOG_LEVEL}"
)

ALLOWED_REDIRECT_URIS="https://oauth-redirect.googleusercontent.com/r/${GOOGLE_AGENT_ID},https://oauth-redirect-sandbox.googleusercontent.com/r/${GOOGLE_AGENT_ID}"

ZIP=$(package_lambda "google_home_start" "${LAMBDA_DIR}/google_home_start")
deploy_lambda "google_home_start" "$ZIP" "$ROLE_GH_START_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE}" \
    "GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID}" \
    "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}" \
    "GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI}" \
    "GOOGLE_SCOPE=profile" \
    "OAUTH_BASE_URL=${API_BASE_URL}" \
    "SESSION_TTL_SECONDS=${SESSION_TTL_SECONDS}" \
    "GH_DEEP_LINK_BASE_URL=${GH_DEEP_LINK_BASE_URL}" \
    "GH_DEEP_LINK_PATH=${GH_DEEP_LINK_PATH}")" 15

ZIP=$(package_lambda "google_home_complete" "${LAMBDA_DIR}/google_home_complete")
deploy_lambda "google_home_complete" "$ZIP" "$ROLE_GH_COMPLETE_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE}" \
    "GH_TOKENS_TABLE=${GH_TOKENS_TABLE}")" 15

ZIP=$(package_lambda "google_home_status" "${LAMBDA_DIR}/google_home_status")
deploy_lambda "google_home_status" "$ZIP" "$ROLE_GH_STATUS_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_TOKENS_TABLE=${GH_TOKENS_TABLE}" \
    "GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID}")" 15

ZIP=$(package_lambda "google_home_unlink" "${LAMBDA_DIR}/google_home_unlink")
deploy_lambda "google_home_unlink" "$ZIP" "$ROLE_GH_UNLINK_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_TOKENS_TABLE=${GH_TOKENS_TABLE}" \
    "GOOGLE_SECRET_REGION=${GOOGLE_SECRET_REGION}" \
    "GOOGLE_SA_KEY_SECRET_ARN=${GOOGLE_SA_KEY_SECRET_ARN}")" 30

ZIP=$(package_lambda "google_home_oauth_authorize" "${LAMBDA_DIR}/google_home_oauth_authorize")
deploy_lambda "google_home_oauth_authorize" "$ZIP" "$ROLE_GH_AUTHORIZE_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}" \
    "ALLOWED_REDIRECT_URIS=${ALLOWED_REDIRECT_URIS}" \
    "COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}" \
    "COGNITO_HOSTED_UI_DOMAIN=${COGNITO_HOSTED_UI_DOMAIN}" \
    "COGNITO_CALLBACK_URI=${COGNITO_CALLBACK_URI}" \
    "GH_OAUTH_TRANSACTIONS_TABLE=${GH_OAUTH_TRANSACTIONS_TABLE}" \
    "TRANSACTION_TTL_SECONDS=${TRANSACTION_TTL_SECONDS}" \
    "APP_NAME=${APP_NAME}")" 15

ZIP=$(package_lambda "google_home_oauth_cognito_callback" "${LAMBDA_DIR}/google_home_oauth_cognito_callback")
deploy_lambda "google_home_oauth_cognito_callback" "$ZIP" "$ROLE_GH_CALLBACK_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID}" \
    "COGNITO_HOSTED_UI_DOMAIN=${COGNITO_HOSTED_UI_DOMAIN}" \
    "COGNITO_CALLBACK_URI=${COGNITO_CALLBACK_URI}" \
    "GH_OAUTH_TRANSACTIONS_TABLE=${GH_OAUTH_TRANSACTIONS_TABLE}" \
    "GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE}" \
    "AUTH_CODE_TTL_SECONDS=${AUTH_CODE_TTL_SECONDS}" \
    "APP_NAME=${APP_NAME}")" 15

ZIP=$(package_lambda "google_home_oauth_token" "${LAMBDA_DIR}/google_home_oauth_token")
deploy_lambda "google_home_oauth_token" "$ZIP" "$ROLE_GH_TOKEN_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE}" \
    "GH_TOKENS_TABLE=${GH_TOKENS_TABLE}" \
    "GOOGLE_CLIENT_SECRET_ARN=${GOOGLE_SECRET_ARN}" \
    "GOOGLE_SECRET_REGION=${GOOGLE_SECRET_REGION}" \
    "GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID}" \
    "ACCESS_TOKEN_TTL_SECONDS=${ACCESS_TOKEN_TTL_SECONDS}" \
    "REFRESH_TOKEN_TTL_SECONDS=${REFRESH_TOKEN_TTL_SECONDS}")" 15

ZIP=$(package_lambda "google_home_fulfillment" "${LAMBDA_DIR}/google_home_fulfillment")
deploy_lambda "google_home_fulfillment" "$ZIP" "$ROLE_GH_FULFILLMENT_ARN" \
  "$(env_json "${GH_COMMON[@]}" \
    "GH_TOKENS_TABLE=${GH_TOKENS_TABLE}" \
    "GH_TOKENS_ACCESS_TOKEN_INDEX=${GH_TOKENS_ACCESS_TOKEN_INDEX}" \
    "USER_DEVICE_MAPPING_TABLE=${USER_DEVICE_MAPPING_TABLE}" \
    "DEVICE_MANUFACTURER=${DEVICE_MANUFACTURER}")" 15

# ── Step 7b: API Gateway Routes ───────────────────────────────────────────────
_section "STEP 7b — GOOGLE HOME API GATEWAY ROUTES"

VOICE_ID=$(get_or_create_resource "$V1_ID"       "voice")
GH_ID=$(get_or_create_resource    "$VOICE_ID"    "google-home")
LINKING_ID=$(get_or_create_resource "$GH_ID"     "account-linking")
DEEP_LINK_ID=$(get_or_create_resource "$LINKING_ID" "deep-link")
GH_START_ID=$(get_or_create_resource "$DEEP_LINK_ID" "start")
GH_COMPLETE_ID=$(get_or_create_resource "$DEEP_LINK_ID" "complete")

wire_method POST   "$GH_START_ID"    "google_home_start"    "COGNITO_USER_POOLS"
wire_method POST   "$GH_COMPLETE_ID" "google_home_complete"  "COGNITO_USER_POOLS"
wire_method GET    "$LINKING_ID"     "google_home_status"    "COGNITO_USER_POOLS"
wire_method DELETE "$LINKING_ID"     "google_home_unlink"    "COGNITO_USER_POOLS"

# OAuth + Fulfillment routes (called by Google, no Cognito auth)
ROOT_GH_ID=$(get_or_create_resource "$ROOT_ID"      "google-home")
OAUTH_ID=$(get_or_create_resource   "$ROOT_GH_ID"   "oauth")
AUTHORIZE_ID=$(get_or_create_resource "$OAUTH_ID"   "authorize")
TOKEN_ID=$(get_or_create_resource     "$OAUTH_ID"   "token")
FULFILLMENT_ID=$(get_or_create_resource "$ROOT_GH_ID" "fulfillment")

COGNITO_CALLBACK_ID=$(get_or_create_resource "$OAUTH_ID"      "cognito")
COGNITO_CB_ID=$(get_or_create_resource      "$COGNITO_CALLBACK_ID" "callback")
CONSENT_ID=$(get_or_create_resource         "$OAUTH_ID"      "consent")

wire_method GET  "$AUTHORIZE_ID"   "google_home_oauth_authorize"        "NONE"
wire_method GET  "$COGNITO_CB_ID"  "google_home_oauth_cognito_callback"  "NONE"
wire_method POST "$CONSENT_ID"     "google_home_oauth_cognito_callback"  "NONE"
wire_method POST "$TOKEN_ID"       "google_home_oauth_token"             "NONE"
wire_method POST "$FULFILLMENT_ID" "google_home_fulfillment"             "NONE"

_pass "Google Home API Gateway routes wired"

fi  # end DEPLOY_GOOGLE

# ── Step 9: Deploy API Stage ───────────────────────────────────────────────────
_section "STEP 9 — DEPLOY API GATEWAY STAGE"

aws apigateway create-deployment \
  --rest-api-id "$API_GATEWAY_ID" \
  --stage-name "$API_STAGE" \
  --description "Deployed by deploy.sh ${ENV_LABEL} $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region "$AWS_REGION" --output text --query 'id' > /dev/null
_pass "Stage '$API_STAGE' deployed"

sleep 5  # brief pause for Lambda cold-start readiness

# ── Step 10: Smoke Tests ───────────────────────────────────────────────────────
_section "STEP 10 — SMOKE TESTS"

BASE="${API_BASE_URL}"

if [ "$DEPLOY_ALEXA" -eq 1 ]; then
  smoke "Alexa start — no auth → 401"    POST   "${BASE}/api/v1/alexa/startAppToApp"    "401"
  smoke "Alexa complete — no auth → 401" POST   "${BASE}/api/v1/alexa/completeAppToApp" "401"
  smoke "Alexa status — no auth → 401"   GET    "${BASE}/api/v1/alexa/status"            "401"
  smoke "Alexa unlink — no auth → 401"   DELETE "${BASE}/api/v1/alexa/unlink"            "401"
  smoke "Alexa callback — no params → 200" GET  "${BASE}/alexa/callback"                "200"
fi

if [ "$DEPLOY_GOOGLE" -eq 1 ]; then
  smoke "GH start — no auth → 401"        POST   "${BASE}/api/v1/voice/google-home/account-linking/deep-link/start"    "401"
  smoke "GH complete — no auth → 401"     POST   "${BASE}/api/v1/voice/google-home/account-linking/deep-link/complete" "401"
  smoke "GH status — no auth → 401"       GET    "${BASE}/api/v1/voice/google-home/account-linking"                    "401"
  smoke "GH unlink — no auth → 401"       DELETE "${BASE}/api/v1/voice/google-home/account-linking"                    "401"
  smoke "GH authorize — reachable → !5xx" GET    "${BASE}/google-home/oauth/authorize"                                  "400"
  smoke "GH token — reachable → !5xx"     POST   "${BASE}/google-home/oauth/token"                                      "400"
fi

# ── Step 11: Google Console Checklist ─────────────────────────────────────────
if [ "$DEPLOY_GOOGLE" -eq 1 ]; then

_section "STEP 11 — GOOGLE HOME DEVELOPER CONSOLE CHECKLIST"

cat <<CHECKLIST

  The following values are specific to this deployment.
  Complete each step in the Google Home Developer Console.

  Console URL: https://console.home.google.com

  ─────────────────────────────────────────────────────────
  1. PROJECT
  ─────────────────────────────────────────────────────────
  [ ] Open or create a project for:  ${ENV_LABEL}

  ─────────────────────────────────────────────────────────
  2. SMART HOME ACTION
  ─────────────────────────────────────────────────────────
  [ ] Add action type: Smart Home
  [ ] Fulfillment URL (webhook):
        ${BASE}/google-home/fulfillment

  ─────────────────────────────────────────────────────────
  3. ACCOUNT LINKING  (OAuth 2.0 / Authorization Code)
  ─────────────────────────────────────────────────────────
  [ ] Client ID:
        ${GOOGLE_CLIENT_ID}
        (must match GOOGLE_CLIENT_ID in Secrets Manager)

  [ ] Client Secret:
        ** Paste from Secrets Manager — do NOT retype manually **
        Secret ARN: ${GOOGLE_SECRET_ARN}

  [ ] Authorization URL:
        ${BASE}/google-home/oauth/authorize

  [ ] Token URL:
        ${BASE}/google-home/oauth/token

  ─────────────────────────────────────────────────────────
  4. APP FLIP (Android — bypasses browser login)
  ─────────────────────────────────────────────────────────
  [ ] App package name:   <Android app package name>
  [ ] SHA-256 fingerprint of release signing cert:
        Run: keytool -list -v -keystore release.keystore
        Copy the SHA-256 fingerprint and paste here.

  ─────────────────────────────────────────────────────────
  !! CRITICAL WARNING !!
  ─────────────────────────────────────────────────────────
  Verify the Client Secret in Secrets Manager EXACTLY
  matches what you enter in Google Console.

  A single character mismatch causes 'invalid_client'
  errors which trigger a 24–48 hour project-level Google
  backoff — NO token exchanges will succeed during this
  window even after fixing the secret.

  ALWAYS confirm the secret matches BEFORE running any
  end-to-end test that touches the token endpoint.
  ─────────────────────────────────────────────────────────

CHECKLIST

fi

# ── Final Results ──────────────────────────────────────────────────────────────
_section "RESULTS"

TOTAL=$((PASS+FAIL))
echo ""
echo "  Total checks: $TOTAL   ${GREEN}✓ Passed: ${PASS}${NC}   ${RED}✗ Failed: ${FAIL}${NC}"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo -e "  ${RED}Failed checks:${NC}"
  for c in "${FAILED_CHECKS[@]}"; do echo "    - $c"; done
  echo ""
  echo -e "  ${RED}OVERALL: FAIL${NC}"
  exit 1
fi

echo -e "  ${GREEN}OVERALL: PASS${NC}"
echo ""
echo "  Live API base: ${API_BASE_URL}"
echo ""

[ "$DEPLOY_ALEXA" -eq 1 ] && cat <<EOF
  Alexa endpoints:
    POST   ${BASE}/api/v1/alexa/startAppToApp
    POST   ${BASE}/api/v1/alexa/completeAppToApp
    GET    ${BASE}/api/v1/alexa/status
    DELETE ${BASE}/api/v1/alexa/unlink
    GET    ${BASE}/alexa/callback
EOF

[ "$DEPLOY_GOOGLE" -eq 1 ] && cat <<EOF
  Google Home endpoints:
    POST   ${BASE}/api/v1/voice/google-home/account-linking/deep-link/start
    POST   ${BASE}/api/v1/voice/google-home/account-linking/deep-link/complete
    GET    ${BASE}/api/v1/voice/google-home/account-linking
    DELETE ${BASE}/api/v1/voice/google-home/account-linking
    GET    ${BASE}/google-home/oauth/authorize
    POST   ${BASE}/google-home/oauth/authorize
    POST   ${BASE}/google-home/oauth/token
    POST   ${BASE}/google-home/fulfillment
EOF

echo ""
