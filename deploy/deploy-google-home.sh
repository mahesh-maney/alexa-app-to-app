#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# deploy-google-home.sh
#
# Zero-touch idempotent deployment of the Google Home Cloud-to-Cloud account
# linking feature to any AWS account.
#
# Idempotent: every resource is check-before-create. Safe to re-run.
#
# Usage:
#   ./deploy/deploy-google-home.sh --config deploy/env.myclient.json
#   ./deploy/deploy-google-home.sh --config deploy/env.myclient.json --dry-run
#   ./deploy/deploy-google-home.sh --config deploy/env.myclient.json --only lambdas
#
# Flags:
#   --config <file>   Required. JSON config from pre-deploy-google-home.sh
#   --dry-run         Print what would be done; do not create anything
#   --only <phase>    Run only: tables | iam | secrets | cognito | lambdas | apigw | smoke
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
skip()    { echo -e "  ${YELLOW}[SKIP]${RESET}  $* (already exists)"; }
dry()     { echo -e "  ${YELLOW}[DRY]${RESET}   would: $*"; }
section() {
  echo
  echo -e "${BOLD}══════════════════════════════════════════════════════${RESET}"
  echo -e "${BOLD}  $*${RESET}"
  echo -e "${BOLD}══════════════════════════════════════════════════════${RESET}"
}

# ── Args ──────────────────────────────────────────────────────────────────────
CONFIG_FILE=""
DRY_RUN=false
ONLY_PHASE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)   CONFIG_FILE="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true;     shift ;;
    --only)     ONLY_PHASE="$2";  shift 2 ;;
    -h|--help)
      sed -n '/^# Usage/,/^[^#]/p' "$0" | head -20 | sed 's/^# \?//'
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -z "$CONFIG_FILE" ]] && die "Usage: $0 --config deploy/env.<brand>.json"
[[ -f "$CONFIG_FILE" ]] || die "Config file not found: $CONFIG_FILE"

# ── Load config ───────────────────────────────────────────────────────────────
cfg() { jq -r --arg k "$1" '.[$k] // ""' "$CONFIG_FILE"; }

DATA_REGION=$(cfg DATA_REGION)
BRAND_PREFIX=$(cfg BRAND_PREFIX)
API_GATEWAY_ID=$(cfg API_GATEWAY_ID)
API_GATEWAY_STAGE=$(cfg API_GATEWAY_STAGE)
OAUTH_BASE_URL=$(cfg OAUTH_BASE_URL)
COGNITO_USER_POOL_ID=$(cfg COGNITO_USER_POOL_ID)
COGNITO_HOSTED_UI_BASE_URL=$(cfg COGNITO_HOSTED_UI_BASE_URL)
COGNITO_REDIRECT_URI=$(cfg COGNITO_REDIRECT_URI)
GH_COGNITO_APP_CLIENT_NAME=$(cfg GH_COGNITO_APP_CLIENT_NAME)
GOOGLE_AGENT_ID=$(cfg GOOGLE_AGENT_ID)
GH_OAUTH_CLIENT_ID=$(cfg GH_OAUTH_CLIENT_ID)
GH_OAUTH_SECRET_NAME=$(cfg GH_OAUTH_SECRET_NAME)
GOOGLE_SA_KEY_SECRET_NAME=$(cfg GOOGLE_SA_KEY_SECRET_NAME)
GOOGLE_SA_KEY_SECRET_ARN=$(cfg GOOGLE_SA_KEY_SECRET_ARN)
APP_NAME=$(cfg APP_NAME)
PRIVACY_POLICY_URL=$(cfg PRIVACY_POLICY_URL)
TERMS_URL=$(cfg TERMS_URL)
BRAND_LOGO_FILENAME=$(cfg BRAND_LOGO_FILENAME)
SESSION_TTL_SECONDS=$(cfg SESSION_TTL_SECONDS)
AUTH_CODE_TTL_SECONDS=$(cfg AUTH_CODE_TTL_SECONDS)
ACCESS_TOKEN_TTL_SECONDS=$(cfg ACCESS_TOKEN_TTL_SECONDS)
REFRESH_TOKEN_TTL_SECONDS=$(cfg REFRESH_TOKEN_TTL_SECONDS)
LAMBDA_TIMEOUT=$(cfg LAMBDA_TIMEOUT)
LAMBDA_MEMORY_MB=$(cfg LAMBDA_MEMORY_MB)
LAMBDA_RUNTIME=$(cfg LAMBDA_RUNTIME)
GH_TOKENS_TABLE=$(cfg GH_TOKENS_TABLE)
GH_SESSIONS_TABLE=$(cfg GH_SESSIONS_TABLE)
GH_AUTH_CODES_TABLE=$(cfg GH_AUTH_CODES_TABLE)
GH_OAUTH_TRANSACTIONS_TABLE=$(cfg GH_OAUTH_TRANSACTIONS_TABLE)
LAMBDA_S3_BUCKET=$(cfg LAMBDA_S3_BUCKET)
USER_DEVICE_TABLE=$(cfg USER_DEVICE_TABLE)
DEVICE_STATE_TABLE=$(cfg DEVICE_STATE_TABLE)
SCENE_TABLE=$(cfg SCENE_TABLE)
METADATA_S3_BUCKET=$(cfg METADATA_S3_BUCKET)
IOT_ENDPOINT_SECRET_NAME=$(cfg IOT_ENDPOINT_SECRET_NAME)
IOT_ENDPOINT=$(cfg IOT_ENDPOINT)
AWS_ACCOUNT_ID=$(cfg AWS_ACCOUNT_ID)

# Defaults for optional numerics
SESSION_TTL_SECONDS="${SESSION_TTL_SECONDS:-1800}"
AUTH_CODE_TTL_SECONDS="${AUTH_CODE_TTL_SECONDS:-900}"
ACCESS_TOKEN_TTL_SECONDS="${ACCESS_TOKEN_TTL_SECONDS:-3600}"
REFRESH_TOKEN_TTL_SECONDS="${REFRESH_TOKEN_TTL_SECONDS:-2592000}"
LAMBDA_TIMEOUT="${LAMBDA_TIMEOUT:-30}"
LAMBDA_MEMORY_MB="${LAMBDA_MEMORY_MB:-256}"
LAMBDA_RUNTIME="${LAMBDA_RUNTIME:-python3.12}"

# Derived
BPFX="${BRAND_PREFIX//-/_}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
LAMBDAS_DIR="$REPO_ROOT/lambdas"
BUILD_DIR="$REPO_ROOT/.build/google-home"

# Lambda function names
FN_AUTHORIZE="${BRAND_PREFIX}-gh-oauth-authorize"
FN_COGNITO_CB="${BRAND_PREFIX}-gh-oauth-cognito-callback"
FN_TOKEN="${BRAND_PREFIX}-gh-oauth-token"
FN_START="${BRAND_PREFIX}-gh-start"
FN_COMPLETE="${BRAND_PREFIX}-gh-complete"
FN_STATUS="${BRAND_PREFIX}-gh-status"
FN_UNLINK="${BRAND_PREFIX}-gh-unlink"
FN_FULFILLMENT="${BRAND_PREFIX}-gh-fulfillment"

# IAM role names
ROLE_OAUTH="${BRAND_PREFIX}-gh-oauth-role"
ROLE_FULFILLMENT="${BRAND_PREFIX}-gh-fulfillment-role"
ROLE_UNLINK="${BRAND_PREFIX}-gh-unlink-role"

# Phase filter helper
run_phase() { [[ -z "$ONLY_PHASE" || "$ONLY_PHASE" == "$1" ]]; }

# ── Prerequisite check ────────────────────────────────────────────────────────
section "Prerequisite check"
for cmd in aws jq python3 zip; do
  command -v "$cmd" &>/dev/null || die "$cmd not found"
done

[[ -n "$DATA_REGION" ]]        || die "DATA_REGION not set in config"
[[ -n "$BRAND_PREFIX" ]]       || die "BRAND_PREFIX not set in config"
[[ -n "$API_GATEWAY_ID" ]]     || die "API_GATEWAY_ID not set in config"
[[ -n "$COGNITO_USER_POOL_ID" ]] || die "COGNITO_USER_POOL_ID not set in config"
[[ -n "$GOOGLE_AGENT_ID" ]]    || die "GOOGLE_AGENT_ID not set in config"
[[ -n "$GH_OAUTH_CLIENT_ID" ]] || die "GH_OAUTH_CLIENT_ID not set in config"

AWS_IDENTITY=$(aws sts get-caller-identity 2>/dev/null) \
  || die "AWS CLI not authenticated"
ACTUAL_ACCOUNT=$(echo "$AWS_IDENTITY" | jq -r '.Account')
info "AWS account: $ACTUAL_ACCOUNT (config says: ${AWS_ACCOUNT_ID:-any})"

$DRY_RUN && warn "DRY RUN mode — no resources will be created"
info "Brand prefix: $BRAND_PREFIX"
info "Region: $DATA_REGION"
info "API Gateway: $API_GATEWAY_ID ($API_GATEWAY_STAGE)"
info "Config: $CONFIG_FILE"

mkdir -p "$BUILD_DIR"

# ── Helper: aws wrapper ───────────────────────────────────────────────────────
awscli() {
  if $DRY_RUN; then dry "aws $*"; return 0; fi
  aws "$@"
}

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 1: DynamoDB Tables
# ──────────────────────────────────────────────────────────────────────────────
if run_phase tables; then
section "Phase 1 — DynamoDB Tables"

create_table_if_missing() {
  local table_name="$1" key_name="$2" extra_args="${3:-}"
  info "Checking DynamoDB table: $table_name"
  EXISTING=$(aws dynamodb describe-table --table-name "$table_name" \
    --region "$DATA_REGION" --query 'Table.TableName' --output text 2>/dev/null || echo "")
  if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
    skip "Table $table_name"
    return
  fi
  info "Creating DynamoDB table: $table_name (key=$key_name)"
  if $DRY_RUN; then dry "create-table $table_name"; return; fi
  aws dynamodb create-table \
    --table-name "$table_name" \
    --attribute-definitions AttributeName="$key_name",AttributeType=S \
    --key-schema AttributeName="$key_name",KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$DATA_REGION" \
    $extra_args \
    >/dev/null
  aws dynamodb wait table-exists --table-name "$table_name" --region "$DATA_REGION"
  success "Created table: $table_name"
}

# google_home_tokens — needs accessToken-index and refreshToken-index GSIs
TOKENS_EXTRA_ARGS='--attribute-definitions AttributeName=userId,AttributeType=S AttributeName=accessToken,AttributeType=S AttributeName=refreshToken,AttributeType=S --key-schema AttributeName=userId,KeyType=HASH --global-secondary-indexes "[{\"IndexName\":\"accessToken-index\",\"KeySchema\":[{\"AttributeName\":\"accessToken\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}},{\"IndexName\":\"refreshToken-index\",\"KeySchema\":[{\"AttributeName\":\"refreshToken\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}]"'
info "Checking DynamoDB table: $GH_TOKENS_TABLE"
EXISTING=$(aws dynamodb describe-table --table-name "$GH_TOKENS_TABLE" \
  --region "$DATA_REGION" --query 'Table.TableName' --output text 2>/dev/null || echo "")
if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
  skip "Table $GH_TOKENS_TABLE"
else
  info "Creating table $GH_TOKENS_TABLE with GSIs..."
  if ! $DRY_RUN; then
    aws dynamodb create-table \
      --table-name "$GH_TOKENS_TABLE" \
      --attribute-definitions \
        AttributeName=userId,AttributeType=S \
        AttributeName=accessToken,AttributeType=S \
        AttributeName=refreshToken,AttributeType=S \
      --key-schema AttributeName=userId,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --region "$DATA_REGION" \
      --global-secondary-indexes \
        '[{"IndexName":"accessToken-index","Keys":"[{\"AttributeName\":\"accessToken\",\"KeyType\":\"HASH\"}]","Projection":{"ProjectionType":"ALL"}},{"IndexName":"refreshToken-index","Keys":"[{\"AttributeName\":\"refreshToken\",\"KeyType\":\"HASH\"}]","Projection":{"ProjectionType":"ALL"}}]' \
      >/dev/null 2>&1 || \
    aws dynamodb create-table \
      --table-name "$GH_TOKENS_TABLE" \
      --attribute-definitions \
        AttributeName=userId,AttributeType=S \
        AttributeName=accessToken,AttributeType=S \
        AttributeName=refreshToken,AttributeType=S \
      --key-schema AttributeName=userId,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST \
      --region "$DATA_REGION" \
      --global-secondary-indexes \
        "[{\"IndexName\":\"accessToken-index\",\"KeySchema\":[{\"AttributeName\":\"accessToken\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}},{\"IndexName\":\"refreshToken-index\",\"KeySchema\":[{\"AttributeName\":\"refreshToken\",\"KeyType\":\"HASH\"}],\"Projection\":{\"ProjectionType\":\"ALL\"}}]" \
      >/dev/null
    aws dynamodb wait table-exists --table-name "$GH_TOKENS_TABLE" --region "$DATA_REGION"
    success "Created table: $GH_TOKENS_TABLE (with accessToken-index + refreshToken-index GSIs)"
  else
    dry "create-table $GH_TOKENS_TABLE with GSIs"
  fi
fi

# Enable TTL on tokens table
if ! $DRY_RUN; then
  TTL_STATUS=$(aws dynamodb describe-time-to-live --table-name "$GH_TOKENS_TABLE" \
    --region "$DATA_REGION" --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || echo "DISABLED")
  if [[ "$TTL_STATUS" != "ENABLED" && "$TTL_STATUS" != "ENABLING" ]]; then
    aws dynamodb update-time-to-live --table-name "$GH_TOKENS_TABLE" \
      --time-to-live-specification "Enabled=true,AttributeName=ttl" \
      --region "$DATA_REGION" >/dev/null
    info "TTL enabled on $GH_TOKENS_TABLE (attribute: ttl)"
  fi
fi

create_table_if_missing "$GH_SESSIONS_TABLE"          "state"
create_table_if_missing "$GH_AUTH_CODES_TABLE"        "code"
create_table_if_missing "$GH_OAUTH_TRANSACTIONS_TABLE" "state"

# Enable TTL on all tables
for TABLE in "$GH_SESSIONS_TABLE" "$GH_AUTH_CODES_TABLE" "$GH_OAUTH_TRANSACTIONS_TABLE"; do
  if ! $DRY_RUN; then
    TTL_STATUS=$(aws dynamodb describe-time-to-live --table-name "$TABLE" \
      --region "$DATA_REGION" --query 'TimeToLiveDescription.TimeToLiveStatus' --output text 2>/dev/null || echo "DISABLED")
    if [[ "$TTL_STATUS" != "ENABLED" && "$TTL_STATUS" != "ENABLING" ]]; then
      aws dynamodb update-time-to-live --table-name "$TABLE" \
        --time-to-live-specification "Enabled=true,AttributeName=ttl" \
        --region "$DATA_REGION" >/dev/null
      info "TTL enabled on $TABLE"
    fi
  fi
done

success "All DynamoDB tables ready"
fi # run_phase tables

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2: Secrets Manager
# ──────────────────────────────────────────────────────────────────────────────
if run_phase secrets; then
section "Phase 2 — Secrets Manager"

create_or_update_secret() {
  local name="$1" value="$2" description="$3"
  info "Checking secret: $name"
  EXISTING_ARN=$(aws secretsmanager describe-secret --secret-id "$name" \
    --region "$DATA_REGION" --query 'ARN' --output text 2>/dev/null || echo "")
  if [[ -n "$EXISTING_ARN" && "$EXISTING_ARN" != "None" ]]; then
    skip "Secret $name"
    echo "$EXISTING_ARN"
    return
  fi
  if $DRY_RUN; then dry "create-secret $name"; echo "arn:aws:secretsmanager:dry-run"; return; fi
  ARN=$(aws secretsmanager create-secret \
    --name "$name" \
    --description "$description" \
    --secret-string "$value" \
    --region "$DATA_REGION" \
    --query 'ARN' --output text)
  success "Created secret: $name ($ARN)"
  echo "$ARN"
}

# GH OAuth credentials secret
GH_OAUTH_SECRET_VALUE=$(jq -nc \
  --arg cid "$GH_OAUTH_CLIENT_ID" \
  --arg cname "${BRAND_PREFIX}-google-home-cognito-client" \
  '{"client_id":$cid,"client_secret":"REPLACE_ME","cognito_client_name":$cname}')
GH_OAUTH_SECRET_ARN=$(create_or_update_secret \
  "$GH_OAUTH_SECRET_NAME" \
  "$GH_OAUTH_SECRET_VALUE" \
  "Google Home OAuth credentials for $BRAND_PREFIX")

# If we already have it and need to check the ARN
if [[ -z "$GH_OAUTH_SECRET_ARN" ]]; then
  GH_OAUTH_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$GH_OAUTH_SECRET_NAME" \
    --region "$DATA_REGION" --query 'ARN' --output text 2>/dev/null || echo "")
fi

# SA key secret (placeholder if not provided at pre-deploy time)
if [[ -z "$GOOGLE_SA_KEY_SECRET_ARN" ]] && [[ -n "$GOOGLE_SA_KEY_SECRET_NAME" ]]; then
  GOOGLE_SA_KEY_SECRET_ARN=$(create_or_update_secret \
    "$GOOGLE_SA_KEY_SECRET_NAME" \
    '{"type":"service_account","note":"Replace with real SA key JSON"}' \
    "Google Service Account key for $BRAND_PREFIX Home Graph API")
fi

success "Secrets ready"
info "GH OAuth Secret ARN: $GH_OAUTH_SECRET_ARN"
info "SA Key Secret ARN:   ${GOOGLE_SA_KEY_SECRET_ARN:-<not set>}"
fi # run_phase secrets

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 3: Cognito App Client + Resource Server
# ──────────────────────────────────────────────────────────────────────────────
if run_phase cognito; then
section "Phase 3 — Cognito App Client & Resource Server"

# Resource server (smarthome_server) for custom scopes
info "Checking Cognito resource server: smarthome_server"
EXISTING_RS=$(aws cognito-idp describe-resource-server \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --identifier "smarthome_server" \
  --region "$DATA_REGION" \
  --query 'ResourceServer.Identifier' --output text 2>/dev/null || echo "")
if [[ -n "$EXISTING_RS" && "$EXISTING_RS" != "None" ]]; then
  skip "Resource server smarthome_server"
else
  info "Creating resource server smarthome_server..."
  if ! $DRY_RUN; then
    aws cognito-idp create-resource-server \
      --user-pool-id "$COGNITO_USER_POOL_ID" \
      --identifier "smarthome_server" \
      --name "Smart Home Server" \
      --scopes \
        ScopeName=read,ScopeDescription="Read device data" \
        ScopeName=write,ScopeDescription="Control devices" \
      --region "$DATA_REGION" >/dev/null
    success "Created resource server smarthome_server (scopes: read, write)"
  else
    dry "create-resource-server smarthome_server"
  fi
fi

# GH Cognito App Client
info "Checking Cognito app client: $GH_COGNITO_APP_CLIENT_NAME"
GH_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --region "$DATA_REGION" \
  --query "UserPoolClients[?ClientName=='${GH_COGNITO_APP_CLIENT_NAME}'].ClientId" \
  --output text 2>/dev/null | awk '{print $1}')

if [[ -n "$GH_CLIENT_ID" && "$GH_CLIENT_ID" != "None" ]]; then
  skip "Cognito app client $GH_COGNITO_APP_CLIENT_NAME (ID: $GH_CLIENT_ID)"
  GH_COGNITO_CLIENT_ID="$GH_CLIENT_ID"
else
  info "Creating Cognito app client: $GH_COGNITO_APP_CLIENT_NAME"
  if $DRY_RUN; then
    dry "create-user-pool-client $GH_COGNITO_APP_CLIENT_NAME"
    GH_COGNITO_CLIENT_ID="DRY-RUN-CLIENT-ID"
  else
    CREATE_RESULT=$(aws cognito-idp create-user-pool-client \
      --user-pool-id "$COGNITO_USER_POOL_ID" \
      --client-name "$GH_COGNITO_APP_CLIENT_NAME" \
      --no-generate-secret \
      --allowed-o-auth-flows code \
      --allowed-o-auth-flows-user-pool-client \
      --allowed-o-auth-scopes openid email profile \
      --supported-identity-providers COGNITO \
      --callback-urls "$COGNITO_REDIRECT_URI" \
      --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_SRP_AUTH \
      --region "$DATA_REGION" \
      --query 'UserPoolClient.ClientId' --output text)
    GH_COGNITO_CLIENT_ID="$CREATE_RESULT"
    success "Created Cognito app client: $GH_COGNITO_CLIENT_ID"
  fi
fi

# Persist cognito client ID to config
if ! $DRY_RUN && [[ -n "$GH_COGNITO_CLIENT_ID" ]]; then
  jq --arg v "$GH_COGNITO_CLIENT_ID" '. + {"GH_COGNITO_CLIENT_ID": $v}' "$CONFIG_FILE" \
    > "${CONFIG_FILE}.tmp" && mv "${CONFIG_FILE}.tmp" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
fi

success "Cognito app client ID: $GH_COGNITO_CLIENT_ID"
fi # run_phase cognito

# Reload client ID from config if we skipped cognito phase
GH_COGNITO_CLIENT_ID="${GH_COGNITO_CLIENT_ID:-$(cfg GH_COGNITO_CLIENT_ID)}"

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 4: IAM Roles & Policies
# ──────────────────────────────────────────────────────────────────────────────
if run_phase iam; then
section "Phase 4 — IAM Roles & Policies"

TRUST_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

create_role_if_missing() {
  local role_name="$1"
  info "Checking IAM role: $role_name"
  EXISTING=$(aws iam get-role --role-name "$role_name" \
    --query 'Role.Arn' --output text 2>/dev/null || echo "")
  if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
    skip "IAM role $role_name"
    echo "$EXISTING"
    return
  fi
  if $DRY_RUN; then dry "create-role $role_name"; echo "arn:aws:iam::${ACTUAL_ACCOUNT}:role/${role_name}"; return; fi
  ARN=$(aws iam create-role \
    --role-name "$role_name" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --query 'Role.Arn' --output text)
  # Attach basic Lambda execution policy
  aws iam attach-role-policy \
    --role-name "$role_name" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  success "Created IAM role: $ARN"
  echo "$ARN"
}

put_inline_policy() {
  local role_name="$1" policy_name="$2" policy_json="$3"
  if $DRY_RUN; then dry "put-role-policy $role_name/$policy_name"; return; fi
  aws iam put-role-policy \
    --role-name "$role_name" \
    --policy-name "$policy_name" \
    --policy-document "$policy_json"
  success "Attached inline policy $policy_name to $role_name"
}

# OAuth role (authorize, cognito_callback, token, start, complete, status)
ROLE_OAUTH_ARN=$(create_role_if_missing "$ROLE_OAUTH")
OAUTH_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}/index/*",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_SESSIONS_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_AUTH_CODES_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_OAUTH_TRANSACTIONS_TABLE}"
      ]
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": ["${GH_OAUTH_SECRET_ARN:-*}"]
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${DATA_REGION}:${ACTUAL_ACCOUNT}:*"
    }
  ]
}
JSON
)
put_inline_policy "$ROLE_OAUTH" "${BRAND_PREFIX}-gh-oauth-policy" "$OAUTH_POLICY"

# Fulfillment role
ROLE_FULFILLMENT_ARN=$(create_role_if_missing "$ROLE_FULFILLMENT")
FULFILLMENT_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TokensTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}/index/*"
      ]
    },
    {
      "Sid": "DeviceData",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:Query","dynamodb:Scan"],
      "Resource": [
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${USER_DEVICE_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${USER_DEVICE_TABLE}/index/*",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${DEVICE_STATE_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${SCENE_TABLE:-none}"
      ]
    },
    {
      "Sid": "S3Metadata",
      "Effect": "Allow",
      "Action": ["s3:GetObject","s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::${METADATA_S3_BUCKET}",
        "arn:aws:s3:::${METADATA_S3_BUCKET}/*"
      ]
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "*"
    },
    {
      "Sid": "IoTPublish",
      "Effect": "Allow",
      "Action": ["iot:Publish"],
      "Resource": "arn:aws:iot:${DATA_REGION}:${ACTUAL_ACCOUNT}:topic/iot/device/*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${DATA_REGION}:${ACTUAL_ACCOUNT}:*"
    }
  ]
}
JSON
)
put_inline_policy "$ROLE_FULFILLMENT" "${BRAND_PREFIX}-gh-fulfillment-policy" "$FULFILLMENT_POLICY"

# Unlink role
ROLE_UNLINK_ARN=$(create_role_if_missing "$ROLE_UNLINK")
UNLINK_POLICY=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:DeleteItem","dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}",
        "arn:aws:dynamodb:${DATA_REGION}:${ACTUAL_ACCOUNT}:table/${GH_TOKENS_TABLE}/index/*"
      ]
    },
    {
      "Sid": "SecretsManager",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "${GH_OAUTH_SECRET_ARN:-*}",
        "${GOOGLE_SA_KEY_SECRET_ARN:-arn:aws:secretsmanager:${DATA_REGION}:${ACTUAL_ACCOUNT}:secret:${GOOGLE_SA_KEY_SECRET_NAME}*}"
      ]
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${DATA_REGION}:${ACTUAL_ACCOUNT}:*"
    }
  ]
}
JSON
)
put_inline_policy "$ROLE_UNLINK" "${BRAND_PREFIX}-gh-unlink-policy" "$UNLINK_POLICY"

success "All IAM roles ready"
info "OAuth role:      $ROLE_OAUTH_ARN"
info "Fulfillment role: $ROLE_FULFILLMENT_ARN"
info "Unlink role:     $ROLE_UNLINK_ARN"
fi # run_phase iam

# Reload role ARNs if we skipped IAM phase
if [[ -z "${ROLE_OAUTH_ARN:-}" ]]; then
  ROLE_OAUTH_ARN=$(aws iam get-role --role-name "$ROLE_OAUTH" \
    --query 'Role.Arn' --output text 2>/dev/null || echo "")
fi
if [[ -z "${ROLE_FULFILLMENT_ARN:-}" ]]; then
  ROLE_FULFILLMENT_ARN=$(aws iam get-role --role-name "$ROLE_FULFILLMENT" \
    --query 'Role.Arn' --output text 2>/dev/null || echo "")
fi
if [[ -z "${ROLE_UNLINK_ARN:-}" ]]; then
  ROLE_UNLINK_ARN=$(aws iam get-role --role-name "$ROLE_UNLINK" \
    --query 'Role.Arn' --output text 2>/dev/null || echo "")
fi
if [[ -z "${GH_OAUTH_SECRET_ARN:-}" ]]; then
  GH_OAUTH_SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$GH_OAUTH_SECRET_NAME" \
    --region "$DATA_REGION" --query 'ARN' --output text 2>/dev/null || echo "")
fi

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 5: Lambda Functions
# ──────────────────────────────────────────────────────────────────────────────
if run_phase lambdas; then
section "Phase 5 — Lambda Functions"

# Give IAM a moment to propagate new roles
if ! $DRY_RUN; then
  info "Waiting 10s for IAM role propagation..."
  sleep 10
fi

package_lambda() {
  local src_dir="$1" zip_out="$2"
  info "Packaging $(basename "$src_dir") → $(basename "$zip_out")"
  rm -f "$zip_out"
  (cd "$src_dir" && zip -q -r "$zip_out" .)
  success "Packaged $(basename "$zip_out") ($(du -sh "$zip_out" | cut -f1))"
}

deploy_lambda() {
  local fn_name="$1" src_dir="$2" role_arn="$3"
  shift 3
  local env_vars="$1"  # JSON string for --environment Variables={...}
  shift

  local zip_file="$BUILD_DIR/${fn_name}.zip"
  package_lambda "$src_dir" "$zip_file"

  info "Checking Lambda: $fn_name"
  EXISTING=$(aws lambda get-function --function-name "$fn_name" \
    --region "$DATA_REGION" --query 'Configuration.FunctionName' --output text 2>/dev/null || echo "")

  if $DRY_RUN; then
    dry "create/update Lambda $fn_name"
    return
  fi

  if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
    info "Updating code: $fn_name"
    aws lambda update-function-code \
      --function-name "$fn_name" \
      --zip-file "fileb://$zip_file" \
      --region "$DATA_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$fn_name" --region "$DATA_REGION"
    info "Updating config: $fn_name"
    aws lambda update-function-configuration \
      --function-name "$fn_name" \
      --role "$role_arn" \
      --timeout "$LAMBDA_TIMEOUT" \
      --memory-size "$LAMBDA_MEMORY_MB" \
      --environment "Variables={${env_vars}}" \
      --region "$DATA_REGION" >/dev/null
    aws lambda wait function-updated --function-name "$fn_name" --region "$DATA_REGION"
    success "Updated Lambda: $fn_name"
  else
    info "Creating Lambda: $fn_name"
    aws lambda create-function \
      --function-name "$fn_name" \
      --runtime "$LAMBDA_RUNTIME" \
      --role "$role_arn" \
      --handler lambda_function.lambda_handler \
      --zip-file "fileb://$zip_file" \
      --timeout "$LAMBDA_TIMEOUT" \
      --memory-size "$LAMBDA_MEMORY_MB" \
      --environment "Variables={${env_vars}}" \
      --region "$DATA_REGION" >/dev/null
    aws lambda wait function-active --function-name "$fn_name" --region "$DATA_REGION"
    success "Created Lambda: $fn_name"
  fi
}

# ── google_home_oauth_authorize ───────────────────────────────────────────────
deploy_lambda "$FN_AUTHORIZE" \
  "$LAMBDAS_DIR/google_home_oauth_authorize" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GH_OAUTH_SECRET_ARN=${GH_OAUTH_SECRET_ARN},COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},COGNITO_HOSTED_UI_BASE_URL=${COGNITO_HOSTED_UI_BASE_URL},COGNITO_REDIRECT_URI=${COGNITO_REDIRECT_URI},GH_TRANSACTIONS_TABLE=${GH_OAUTH_TRANSACTIONS_TABLE},OAUTH_BASE_URL=${OAUTH_BASE_URL}"

# ── google_home_oauth_cognito_callback ────────────────────────────────────────
deploy_lambda "$FN_COGNITO_CB" \
  "$LAMBDAS_DIR/google_home_oauth_cognito_callback" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GH_OAUTH_SECRET_ARN=${GH_OAUTH_SECRET_ARN},COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE},GH_TRANSACTIONS_TABLE=${GH_OAUTH_TRANSACTIONS_TABLE},OAUTH_BASE_URL=${OAUTH_BASE_URL},APP_NAME=${APP_NAME},PRIVACY_POLICY_URL=${PRIVACY_POLICY_URL},TERMS_URL=${TERMS_URL}"

# ── google_home_oauth_token ───────────────────────────────────────────────────
deploy_lambda "$FN_TOKEN" \
  "$LAMBDAS_DIR/google_home_oauth_token" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GH_OAUTH_SECRET_ARN=${GH_OAUTH_SECRET_ARN},GH_TOKENS_TABLE=${GH_TOKENS_TABLE},GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE},ACCESS_TOKEN_TTL_SECONDS=${ACCESS_TOKEN_TTL_SECONDS},REFRESH_TOKEN_TTL_SECONDS=${REFRESH_TOKEN_TTL_SECONDS}"

# ── google_home_start ─────────────────────────────────────────────────────────
deploy_lambda "$FN_START" \
  "$LAMBDAS_DIR/google_home_start" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID},GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE},SESSION_TTL_SECONDS=${SESSION_TTL_SECONDS},OAUTH_BASE_URL=${OAUTH_BASE_URL}"

# ── google_home_complete ──────────────────────────────────────────────────────
deploy_lambda "$FN_COMPLETE" \
  "$LAMBDAS_DIR/google_home_complete" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GH_TOKENS_TABLE=${GH_TOKENS_TABLE},GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE}"

# ── google_home_status ────────────────────────────────────────────────────────
deploy_lambda "$FN_STATUS" \
  "$LAMBDAS_DIR/google_home_status" \
  "$ROLE_OAUTH_ARN" \
  "DATA_REGION=${DATA_REGION},GH_TOKENS_TABLE=${GH_TOKENS_TABLE}"

# ── google_home_unlink ────────────────────────────────────────────────────────
deploy_lambda "$FN_UNLINK" \
  "$LAMBDAS_DIR/google_home_unlink" \
  "$ROLE_UNLINK_ARN" \
  "DATA_REGION=${DATA_REGION},GH_TOKENS_TABLE=${GH_TOKENS_TABLE},GH_OAUTH_SECRET_ARN=${GH_OAUTH_SECRET_ARN},GOOGLE_SA_KEY_SECRET_ARN=${GOOGLE_SA_KEY_SECRET_ARN:-}"

# ── google_home_fulfillment ───────────────────────────────────────────────────
deploy_lambda "$FN_FULFILLMENT" \
  "$LAMBDAS_DIR/google_home_fulfillment" \
  "$ROLE_FULFILLMENT_ARN" \
  "DATA_REGION=${DATA_REGION},GH_TOKENS_TABLE=${GH_TOKENS_TABLE},USER_DEVICE_TABLE=${USER_DEVICE_TABLE},DEVICE_STATE_TABLE=${DEVICE_STATE_TABLE},SCENE_TABLE=${SCENE_TABLE},METADATA_S3_BUCKET=${METADATA_S3_BUCKET},IOT_ENDPOINT_SECRET_NAME=${IOT_ENDPOINT_SECRET_NAME},IOT_ENDPOINT=${IOT_ENDPOINT}"

success "All Lambda functions deployed"
fi # run_phase lambdas

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 6: API Gateway Routes
# ──────────────────────────────────────────────────────────────────────────────
if run_phase apigw; then
section "Phase 6 — API Gateway Routes"

# Get root resource ID
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_GATEWAY_ID" \
  --region "$DATA_REGION" \
  --query 'items[?path==`/`].id' \
  --output text)
info "API Gateway root resource ID: $ROOT_ID"

# Helper: get or create resource at a path
get_or_create_resource() {
  local parent_id="$1" path_part="$2"
  EXISTING_ID=$(aws apigateway get-resources \
    --rest-api-id "$API_GATEWAY_ID" \
    --region "$DATA_REGION" \
    --query "items[?pathPart=='${path_part}' && parentId=='${parent_id}'].id" \
    --output text 2>/dev/null | awk '{print $1}')
  if [[ -n "$EXISTING_ID" && "$EXISTING_ID" != "None" ]]; then
    echo "$EXISTING_ID"
    return
  fi
  if $DRY_RUN; then dry "create-resource /${path_part}"; echo "DRY-${path_part}"; return; fi
  NEW_ID=$(aws apigateway create-resource \
    --rest-api-id "$API_GATEWAY_ID" \
    --parent-id "$parent_id" \
    --path-part "$path_part" \
    --region "$DATA_REGION" \
    --query 'id' --output text)
  info "Created resource: /$path_part (id=$NEW_ID)"
  echo "$NEW_ID"
}

# Helper: get Lambda ARN
lambda_arn() {
  local fn_name="$1"
  aws lambda get-function --function-name "$fn_name" \
    --region "$DATA_REGION" --query 'Configuration.FunctionArn' --output text 2>/dev/null || echo ""
}

# Helper: create API GW integration (idempotent)
put_method_and_integration() {
  local resource_id="$1" http_method="$2" fn_name="$3" auth_type="${4:-NONE}"
  info "Setting up $http_method on resource $resource_id → $fn_name"
  if $DRY_RUN; then dry "put-method $http_method $resource_id"; return; fi

  # Method
  aws apigateway put-method \
    --rest-api-id "$API_GATEWAY_ID" \
    --resource-id "$resource_id" \
    --http-method "$http_method" \
    --authorization-type "$auth_type" \
    --region "$DATA_REGION" 2>/dev/null || true  # ignore if already exists

  FN_ARN=$(lambda_arn "$fn_name")
  [[ -z "$FN_ARN" ]] && { warn "Lambda $fn_name not found — skipping integration"; return; }

  INTEGRATION_URI="arn:aws:apigateway:${DATA_REGION}:lambda:path/2015-03-31/functions/${FN_ARN}/invocations"

  # Integration
  aws apigateway put-integration \
    --rest-api-id "$API_GATEWAY_ID" \
    --resource-id "$resource_id" \
    --http-method "$http_method" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "$INTEGRATION_URI" \
    --region "$DATA_REGION" >/dev/null 2>/dev/null || true

  # Method response
  aws apigateway put-method-response \
    --rest-api-id "$API_GATEWAY_ID" \
    --resource-id "$resource_id" \
    --http-method "$http_method" \
    --status-code 200 \
    --region "$DATA_REGION" >/dev/null 2>/dev/null || true

  # Lambda permission
  STMT_ID="${fn_name}-apigw-$(echo "$resource_id" | tr -d '-')"
  aws lambda add-permission \
    --function-name "$fn_name" \
    --statement-id "$STMT_ID" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${DATA_REGION}:${ACTUAL_ACCOUNT}:${API_GATEWAY_ID}/*/${http_method}/*" \
    --region "$DATA_REGION" >/dev/null 2>/dev/null || true  # ignore if already exists

  success "$http_method → $fn_name"
}

# Build resource tree:
# /google-home
#   /oauth
#     /authorize          GET  → gh-oauth-authorize
#     /token              POST → gh-oauth-token
#     /cognito
#       /callback         GET  → gh-oauth-cognito-callback
#   /fulfillment          POST → gh-fulfillment
# /api/v1/voice/google-home/account-linking
#   (GET)                      → gh-status
#   (DELETE)                   → gh-unlink
#   /deep-link
#     /start              POST → gh-start
#     /complete           POST → gh-complete

# ── /google-home ──────────────────────────────────────────────────────────────
R_GH=$(get_or_create_resource "$ROOT_ID" "google-home")
R_OAUTH=$(get_or_create_resource "$R_GH" "oauth")
R_AUTHORIZE=$(get_or_create_resource "$R_OAUTH" "authorize")
R_TOKEN_RES=$(get_or_create_resource "$R_OAUTH" "token")
R_COGNITO=$(get_or_create_resource "$R_OAUTH" "cognito")
R_CB=$(get_or_create_resource "$R_COGNITO" "callback")
R_FULFILLMENT=$(get_or_create_resource "$R_GH" "fulfillment")

put_method_and_integration "$R_AUTHORIZE"   "GET"  "$FN_AUTHORIZE"
put_method_and_integration "$R_TOKEN_RES"   "POST" "$FN_TOKEN"
put_method_and_integration "$R_CB"          "GET"  "$FN_COGNITO_CB"
put_method_and_integration "$R_FULFILLMENT" "POST" "$FN_FULFILLMENT"

# ── /api/v1/voice/google-home/account-linking ─────────────────────────────────
# Reuse or create the /api, /api/v1, /api/v1/voice, etc. path segments
R_API=$(get_or_create_resource "$ROOT_ID" "api")
R_V1=$(get_or_create_resource "$R_API" "v1")
R_VOICE=$(get_or_create_resource "$R_V1" "voice")
R_GH_AL=$(get_or_create_resource "$R_VOICE" "google-home")
R_AL=$(get_or_create_resource "$R_GH_AL" "account-linking")
R_DL=$(get_or_create_resource "$R_AL" "deep-link")
R_START=$(get_or_create_resource "$R_DL" "start")
R_COMPLETE=$(get_or_create_resource "$R_DL" "complete")

put_method_and_integration "$R_AL"       "GET"    "$FN_STATUS"
put_method_and_integration "$R_AL"       "DELETE" "$FN_UNLINK"
put_method_and_integration "$R_START"    "POST"   "$FN_START"
put_method_and_integration "$R_COMPLETE" "POST"   "$FN_COMPLETE"

# ── Deploy API Gateway ────────────────────────────────────────────────────────
if ! $DRY_RUN; then
  info "Deploying API Gateway stage: $API_GATEWAY_STAGE"
  aws apigateway create-deployment \
    --rest-api-id "$API_GATEWAY_ID" \
    --stage-name "$API_GATEWAY_STAGE" \
    --description "Google Home feature deployment $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --region "$DATA_REGION" >/dev/null
  success "API Gateway deployed to stage: $API_GATEWAY_STAGE"
else
  dry "create-deployment to stage $API_GATEWAY_STAGE"
fi

success "All API Gateway routes configured"
fi # run_phase apigw

# ──────────────────────────────────────────────────────────────────────────────
# PHASE 7: Smoke Test
# ──────────────────────────────────────────────────────────────────────────────
if run_phase smoke; then
section "Phase 7 — Smoke Test"

if $DRY_RUN; then
  dry "smoke tests (skipped in dry-run mode)"
else
  SMOKE_PASS=true

  check_url() {
    local label="$1" url="$2" expected_status="${3:-200}"
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$url" --max-time 10 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "$expected_status" ]]; then
      success "$label → HTTP $HTTP_STATUS"
    else
      warn "$label → HTTP $HTTP_STATUS (expected $expected_status)"
      SMOKE_PASS=false
    fi
  }

  # OAuth authorize (no params → should return 400 or redirect, not 500)
  check_url "OAuth authorize" "${OAUTH_BASE_URL}/google-home/oauth/authorize?client_id=test&response_type=code&redirect_uri=test&state=test" "400"

  # Token endpoint (no params → 400)
  check_url "OAuth token" "${OAUTH_BASE_URL}/google-home/oauth/token" "400"

  # GH status endpoint (no auth → 401)
  check_url "GH status (no auth)" "${OAUTH_BASE_URL}/api/v1/voice/google-home/account-linking" "401"

  if $SMOKE_PASS; then
    success "Smoke tests passed"
  else
    warn "Some smoke tests returned unexpected status codes — check Lambda logs"
  fi
fi
fi # run_phase smoke

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
section "Deployment Complete"
echo
echo "  Brand:          $BRAND_PREFIX"
echo "  Region:         $DATA_REGION"
echo "  Account:        $ACTUAL_ACCOUNT"
echo
echo "  Google Actions Console URLs to configure:"
echo "    Auth URL:        ${OAUTH_BASE_URL}/google-home/oauth/authorize"
echo "    Token URL:       ${OAUTH_BASE_URL}/google-home/oauth/token"
echo "    Fulfillment URL: ${OAUTH_BASE_URL}/google-home/fulfillment"
echo
echo "  Lambda functions deployed:"
echo "    $FN_AUTHORIZE"
echo "    $FN_COGNITO_CB"
echo "    $FN_TOKEN"
echo "    $FN_START"
echo "    $FN_COMPLETE"
echo "    $FN_STATUS"
echo "    $FN_UNLINK"
echo "    $FN_FULFILLMENT"
echo
if [[ -z "${GOOGLE_SA_KEY_SECRET_ARN:-}" ]]; then
  echo -e "  ${YELLOW}[ACTION REQUIRED]${RESET} Store your Google Service Account JSON key:"
  echo "    aws secretsmanager put-secret-value \\"
  echo "      --secret-id $GOOGLE_SA_KEY_SECRET_NAME \\"
  echo "      --secret-string file://your-sa-key.json \\"
  echo "      --region $DATA_REGION"
  echo "  Then update the $FN_UNLINK Lambda env var GOOGLE_SA_KEY_SECRET_ARN."
fi
echo
echo -e "  ${GREEN}${BOLD}All done!${RESET}"
echo
