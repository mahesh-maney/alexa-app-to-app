#!/bin/bash
# google_home_deploy.sh — Google Home Cloud-to-Cloud Account Linking + Fulfillment
#
# What this script does:
#   1. Creates DynamoDB tables (sessions, auth codes, tokens with GSIs)
#   2. Creates IAM roles with least-privilege permissions per Lambda
#   3. Packages and deploys all 7 Lambda functions with X-Ray tracing
#   4. Wires API Gateway routes into the existing ds6nxf8ac5 API
#   5. Deploys the updated API stage (smarthome — NOT prod)
#   6. Sets log retention + CloudWatch alarms
#
# Prerequisites:
#   aws cli configured (ap-south-1)
#   jq installed  (brew install jq)
#
# Required env vars before running:
#   GOOGLE_AGENT_ID         — Google Home agent ID (from Actions Console)
#   GOOGLE_CLIENT_ID        — Google's OAuth client_id for your integration
#   GOOGLE_REDIRECT_URI     — Google's OAuth redirect URI
#                             e.g. https://oauth-redirect.googleusercontent.com/r/PROJECT_ID
#   GOOGLE_SECRET_ARN       — Secrets Manager ARN for {"client_id":..., "client_secret":...}
#   COGNITO_CLIENT_ID       — Digilux Cognito app client ID (for Cognito USER_PASSWORD_AUTH)
#
# Usage:
#   export GOOGLE_AGENT_ID=... GOOGLE_CLIENT_ID=... GOOGLE_REDIRECT_URI=... \
#          GOOGLE_SECRET_ARN=... COGNITO_CLIENT_ID=...
#   chmod +x google_home_deploy.sh && ./google_home_deploy.sh

set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# ── Existing infrastructure (do not change) ───────────────────────────────────
API_ID="ds6nxf8ac5"
AUTHORIZER_ID="fp1yfy"                          # Cognito USER_POOLS authorizer
COGNITO_USER_POOL_ID="ap-south-1_h1o8s7257"
COGNITO_REGION="ap-south-1"
OAUTH_BASE_URL="https://iot.digilux.co.in/smarthome"

# ── Required env vars ─────────────────────────────────────────────────────────
GOOGLE_AGENT_ID="${GOOGLE_AGENT_ID:-}"
GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-}"
GOOGLE_REDIRECT_URI="${GOOGLE_REDIRECT_URI:-}"
GOOGLE_SECRET_ARN="${GOOGLE_SECRET_ARN:-}"
COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:-q7189jitfkk4ttesepkgls491}"

if [ -z "$GOOGLE_AGENT_ID" ] || [ -z "$GOOGLE_CLIENT_ID" ] || \
   [ -z "$GOOGLE_REDIRECT_URI" ] || [ -z "$GOOGLE_SECRET_ARN" ]; then
  echo "ERROR: Missing required environment variables."
  echo "  Set: GOOGLE_AGENT_ID, GOOGLE_CLIENT_ID, GOOGLE_REDIRECT_URI, GOOGLE_SECRET_ARN"
  exit 1
fi

# ── Resource names ─────────────────────────────────────────────────────────────
GH_SESSIONS_TABLE="google_home_link_sessions"
GH_AUTH_CODES_TABLE="google_home_auth_codes"
GH_TOKENS_TABLE="google_home_tokens"
USER_DEVICE_MAPPING_TABLE="digilux_honeywell_user_device_mapping"

LAMBDA_START="google_home_start"
LAMBDA_COMPLETE="google_home_complete"
LAMBDA_STATUS="google_home_status"
LAMBDA_UNLINK="google_home_unlink"
LAMBDA_AUTHORIZE="google_home_oauth_authorize"
LAMBDA_TOKEN="google_home_oauth_token"
LAMBDA_FULFILLMENT="google_home_fulfillment"

ROLE_START="digilux-gh-start-role"
ROLE_COMPLETE="digilux-gh-complete-role"
ROLE_STATUS="digilux-gh-status-role"
ROLE_UNLINK="digilux-gh-unlink-role"
ROLE_AUTHORIZE="digilux-gh-authorize-role"
ROLE_TOKEN="digilux-gh-token-role"
ROLE_FULFILLMENT="digilux-gh-fulfillment-role"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="${SCRIPT_DIR}/../lambdas"

echo "=================================================="
echo " Digilux — Google Home Cloud-to-Cloud Integration"
echo " Account : $ACCOUNT_ID"
echo " Region  : $REGION"
echo " Agent   : $GOOGLE_AGENT_ID"
echo "=================================================="

# ── 1. DynamoDB tables ────────────────────────────────────────────────────────
echo ""
echo "==> [1/6] DynamoDB tables"

create_table_if_not_exists() {
  local NAME="$1"
  local EXTRA_ARGS="${2:-}"
  if aws dynamodb describe-table --table-name "$NAME" --region "$REGION" \
       --query 'Table.TableName' --output text 2>/dev/null | grep -q "$NAME"; then
    echo "    Table already exists: $NAME"
    return 0
  fi
  aws dynamodb create-table \
    --table-name "$NAME" \
    --region "$REGION" \
    --attribute-definitions AttributeName=state,AttributeType=S \
    --key-schema AttributeName=state,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    $EXTRA_ARGS > /dev/null
  aws dynamodb update-time-to-live \
    --table-name "$NAME" \
    --region "$REGION" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null
  echo "    Created: $NAME"
}

# Sessions table — PK: state
create_table_if_not_exists "$GH_SESSIONS_TABLE"

# Auth codes table — PK: code
if aws dynamodb describe-table --table-name "$GH_AUTH_CODES_TABLE" --region "$REGION" \
     --query 'Table.TableName' --output text 2>/dev/null | grep -q "$GH_AUTH_CODES_TABLE"; then
  echo "    Table already exists: $GH_AUTH_CODES_TABLE"
else
  aws dynamodb create-table \
    --table-name "$GH_AUTH_CODES_TABLE" \
    --region "$REGION" \
    --attribute-definitions AttributeName=code,AttributeType=S \
    --key-schema AttributeName=code,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST > /dev/null
  aws dynamodb update-time-to-live \
    --table-name "$GH_AUTH_CODES_TABLE" \
    --region "$REGION" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null
  echo "    Created: $GH_AUTH_CODES_TABLE"
fi

# Tokens table — PK: userId, GSIs on accessToken + refreshToken
if aws dynamodb describe-table --table-name "$GH_TOKENS_TABLE" --region "$REGION" \
     --query 'Table.TableName' --output text 2>/dev/null | grep -q "$GH_TOKENS_TABLE"; then
  echo "    Table already exists: $GH_TOKENS_TABLE"
else
  aws dynamodb create-table \
    --table-name "$GH_TOKENS_TABLE" \
    --region "$REGION" \
    --attribute-definitions \
      AttributeName=userId,AttributeType=S \
      AttributeName=accessToken,AttributeType=S \
      AttributeName=refreshToken,AttributeType=S \
    --key-schema AttributeName=userId,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --global-secondary-indexes \
      '[
        {
          "IndexName": "accessToken-index",
          "KeySchema": [{"AttributeName":"accessToken","KeyType":"HASH"}],
          "Projection": {"ProjectionType":"ALL"}
        },
        {
          "IndexName": "refreshToken-index",
          "KeySchema": [{"AttributeName":"refreshToken","KeyType":"HASH"}],
          "Projection": {"ProjectionType":"ALL"}
        }
      ]' > /dev/null
  echo "    Created: $GH_TOKENS_TABLE (with accessToken-index + refreshToken-index GSIs)"
fi

# ── 2. IAM roles ──────────────────────────────────────────────────────────────
echo ""
echo "==> [2/6] IAM roles (one per Lambda)"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

LOGS_STMT='{
  "Sid": "CloudWatchLogs",
  "Effect": "Allow",
  "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
  "Resource": "arn:aws:logs:'"${REGION}"':'"${ACCOUNT_ID}"':log-group:/aws/lambda/google_home_*:*"
}'

XRAY_STMT='{
  "Sid": "XRayTrace",
  "Effect": "Allow",
  "Action": ["xray:PutTraceSegments","xray:PutTelemetryRecords"],
  "Resource": "*"
}'

create_or_update_role() {
  local ROLE="$1"
  local POLICY_JSON="$2"

  if aws iam get-role --role-name "$ROLE" 2>/dev/null | grep -q "$ROLE"; then
    echo "    Role exists: $ROLE"
  else
    aws iam create-role \
      --role-name "$ROLE" \
      --assume-role-policy-document "$TRUST_POLICY" > /dev/null
    echo "    Created role: $ROLE"
  fi

  aws iam put-role-policy \
    --role-name "$ROLE" \
    --policy-name "permissions" \
    --policy-document "$POLICY_JSON"
}

# start: DDB sessions PutItem
create_or_update_role "$ROLE_START" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhSessions",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_SESSIONS_TABLE}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# complete: sessions GetItem + tokens GetItem
create_or_update_role "$ROLE_COMPLETE" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhSessions",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_SESSIONS_TABLE}"
    },
    {
      "Sid": "GhTokens",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# status: tokens GetItem
create_or_update_role "$ROLE_STATUS" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhTokens",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# unlink: tokens GetItem + DeleteItem + Secrets
create_or_update_role "$ROLE_UNLINK" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhTokens",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}"
    },
    {
      "Sid": "GhSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "${GOOGLE_SECRET_ARN}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# authorize: sessions GetItem + auth codes PutItem + Cognito initiate-auth
create_or_update_role "$ROLE_AUTHORIZE" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhSessions",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_SESSIONS_TABLE}"
    },
    {
      "Sid": "GhAuthCodes",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_AUTH_CODES_TABLE}"
    },
    {
      "Sid": "CognitoAuth",
      "Effect": "Allow",
      "Action": ["cognito-idp:InitiateAuth"],
      "Resource": "arn:aws:cognito-idp:${COGNITO_REGION}:${ACCOUNT_ID}:userpool/${COGNITO_USER_POOL_ID}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# token: auth codes GetItem+DeleteItem + tokens PutItem+UpdateItem+Query(GSI) + Secrets
create_or_update_role "$ROLE_TOKEN" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhAuthCodes",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_AUTH_CODES_TABLE}"
    },
    {
      "Sid": "GhTokens",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}/index/*"
      ]
    },
    {
      "Sid": "GhSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "${GOOGLE_SECRET_ARN}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

# fulfillment: tokens Query(GSI)+DeleteItem + device mapping Query
create_or_update_role "$ROLE_FULFILLMENT" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GhTokens",
      "Effect": "Allow",
      "Action": ["dynamodb:Query","dynamodb:DeleteItem"],
      "Resource": [
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}",
        "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${GH_TOKENS_TABLE}/index/*"
      ]
    },
    {
      "Sid": "DeviceMapping",
      "Effect": "Allow",
      "Action": ["dynamodb:Query"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${USER_DEVICE_MAPPING_TABLE}"
    },
    ${LOGS_STMT}, ${XRAY_STMT}
  ]
}
POLICY
)"

echo "    Waiting 10s for IAM propagation..."
sleep 10

# ── 3. Lambda functions ───────────────────────────────────────────────────────
echo ""
echo "==> [3/6] Lambda functions (python3.12, X-Ray)"

deploy_lambda() {
  local FUNC_NAME="$1"
  local SOURCE_DIR="$2"
  local ROLE_ARN="$3"
  local ENV_VARS="$4"
  local TIMEOUT="${5:-30}"
  local ZIP_FILE="/tmp/${FUNC_NAME}.zip"

  cd "$SOURCE_DIR"
  zip -q "$ZIP_FILE" lambda_function.py
  cd - > /dev/null

  if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" \
       --query 'Configuration.FunctionName' --output text 2>/dev/null | grep -q "$FUNC_NAME"; then
    aws lambda update-function-code \
      --function-name "$FUNC_NAME" --region "$REGION" \
      --zip-file "fileb://${ZIP_FILE}" > /dev/null
    aws lambda wait function-updated --function-name "$FUNC_NAME" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$FUNC_NAME" --region "$REGION" \
      --environment "Variables=${ENV_VARS}" \
      --timeout "$TIMEOUT" \
      --tracing-config Mode=Active > /dev/null
    echo "    Updated: $FUNC_NAME"
  else
    aws lambda create-function \
      --function-name "$FUNC_NAME" --region "$REGION" \
      --runtime python3.12 \
      --handler lambda_function.lambda_handler \
      --role "$ROLE_ARN" \
      --zip-file "fileb://${ZIP_FILE}" \
      --environment "Variables=${ENV_VARS}" \
      --timeout "$TIMEOUT" \
      --memory-size 256 \
      --tracing-config Mode=Active > /dev/null
    aws lambda wait function-active --function-name "$FUNC_NAME" --region "$REGION"
    echo "    Created: $FUNC_NAME"
  fi
}

ROLE_START_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_START}"
ROLE_COMPLETE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_COMPLETE}"
ROLE_STATUS_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_STATUS}"
ROLE_UNLINK_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_UNLINK}"
ROLE_AUTHORIZE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_AUTHORIZE}"
ROLE_TOKEN_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_TOKEN}"
ROLE_FULFILLMENT_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_FULFILLMENT}"

ALLOWED_REDIRECT_URIS="$GOOGLE_REDIRECT_URI"

START_ENV="{
  DATA_REGION=${REGION},
  GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE},
  SESSION_TTL_SECONDS=600,
  GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID},
  OAUTH_BASE_URL=${OAUTH_BASE_URL},
  GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},
  GOOGLE_REDIRECT_URI=${GOOGLE_REDIRECT_URI},
  GOOGLE_SCOPE=profile,
  LOG_LEVEL=INFO
}"

COMPLETE_ENV="{
  DATA_REGION=${REGION},
  GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE},
  GH_TOKENS_TABLE=${GH_TOKENS_TABLE},
  LOG_LEVEL=INFO
}"

STATUS_ENV="{
  DATA_REGION=${REGION},
  GH_TOKENS_TABLE=${GH_TOKENS_TABLE},
  GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID},
  LOG_LEVEL=INFO
}"

UNLINK_ENV="{
  DATA_REGION=${REGION},
  GH_TOKENS_TABLE=${GH_TOKENS_TABLE},
  GOOGLE_CLIENT_SECRET_ARN=${GOOGLE_SECRET_ARN},
  GOOGLE_SECRET_REGION=${REGION},
  HTTP_TIMEOUT=10,
  LOG_LEVEL=INFO
}"

AUTHORIZE_ENV="{
  DATA_REGION=${REGION},
  GH_SESSIONS_TABLE=${GH_SESSIONS_TABLE},
  GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE},
  GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID},
  ALLOWED_REDIRECT_URIS=${ALLOWED_REDIRECT_URIS},
  COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},
  COGNITO_REGION=${COGNITO_REGION},
  COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID},
  AUTH_CODE_TTL_SECONDS=300,
  APP_NAME=Digilux Smart Home,
  LOG_LEVEL=INFO
}"

TOKEN_ENV="{
  DATA_REGION=${REGION},
  GH_AUTH_CODES_TABLE=${GH_AUTH_CODES_TABLE},
  GH_TOKENS_TABLE=${GH_TOKENS_TABLE},
  GOOGLE_CLIENT_SECRET_ARN=${GOOGLE_SECRET_ARN},
  GOOGLE_SECRET_REGION=${REGION},
  GOOGLE_AGENT_ID=${GOOGLE_AGENT_ID},
  ACCESS_TOKEN_TTL_SECONDS=3600,
  REFRESH_TOKEN_TTL_SECONDS=15552000,
  LOG_LEVEL=INFO
}"

FULFILLMENT_ENV="{
  DATA_REGION=${REGION},
  GH_TOKENS_TABLE=${GH_TOKENS_TABLE},
  USER_DEVICE_MAPPING_TABLE=${USER_DEVICE_MAPPING_TABLE},
  LOG_LEVEL=INFO
}"

deploy_lambda "$LAMBDA_START"       "${LAMBDA_DIR}/google_home_start"           "$ROLE_START_ARN"       "$START_ENV"       15
deploy_lambda "$LAMBDA_COMPLETE"    "${LAMBDA_DIR}/google_home_complete"        "$ROLE_COMPLETE_ARN"    "$COMPLETE_ENV"    15
deploy_lambda "$LAMBDA_STATUS"      "${LAMBDA_DIR}/google_home_status"          "$ROLE_STATUS_ARN"      "$STATUS_ENV"      10
deploy_lambda "$LAMBDA_UNLINK"      "${LAMBDA_DIR}/google_home_unlink"          "$ROLE_UNLINK_ARN"      "$UNLINK_ENV"      15
deploy_lambda "$LAMBDA_AUTHORIZE"   "${LAMBDA_DIR}/google_home_oauth_authorize" "$ROLE_AUTHORIZE_ARN"   "$AUTHORIZE_ENV"   15
deploy_lambda "$LAMBDA_TOKEN"       "${LAMBDA_DIR}/google_home_oauth_token"     "$ROLE_TOKEN_ARN"       "$TOKEN_ENV"       15
deploy_lambda "$LAMBDA_FULFILLMENT" "${LAMBDA_DIR}/google_home_fulfillment"     "$ROLE_FULFILLMENT_ARN" "$FULFILLMENT_ENV" 30

# ── 4. API Gateway routes ──────────────────────────────────────────────────────
echo ""
echo "==> [4/6] API Gateway routes"

get_or_create_resource() {
  local PARENT_ID="$1"
  local PATH_PART="$2"

  EXISTING=$(aws apigateway get-resources \
    --rest-api-id "$API_ID" --region "$REGION" \
    --query "items[?parentId=='${PARENT_ID}' && pathPart=='${PATH_PART}'].id" \
    --output text 2>/dev/null)

  if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
    echo "$EXISTING"
  else
    aws apigateway create-resource \
      --rest-api-id "$API_ID" --region "$REGION" \
      --parent-id "$PARENT_ID" --path-part "$PATH_PART" \
      --query 'id' --output text
  fi
}

add_method() {
  local RESOURCE_ID="$1"
  local HTTP_METHOD="$2"
  local LAMBDA_NAME="$3"
  local USE_AUTH="${4:-true}"
  local AUTH_SCOPES="${5:-}"

  LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
  INTEGRATION_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations"

  local AUTH_TYPE
  local AUTH_ARGS=""
  if [ "$USE_AUTH" = "true" ]; then
    AUTH_TYPE="COGNITO_USER_POOLS"
    AUTH_ARGS="--authorizer-id $AUTHORIZER_ID"
    if [ -n "$AUTH_SCOPES" ]; then
      AUTH_ARGS="$AUTH_ARGS --authorization-scopes $AUTH_SCOPES"
    fi
  else
    AUTH_TYPE="NONE"
  fi

  aws apigateway put-method \
    --rest-api-id "$API_ID" --region "$REGION" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --authorization-type "$AUTH_TYPE" \
    $AUTH_ARGS \
    --no-api-key-required 2>/dev/null || true

  aws apigateway put-integration \
    --rest-api-id "$API_ID" --region "$REGION" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "$INTEGRATION_URI" > /dev/null

  STMT_ID="apigw-$(echo "$RESOURCE_ID" | cut -c1-8)-${HTTP_METHOD,,}"
  aws lambda add-permission \
    --function-name "$LAMBDA_NAME" --region "$REGION" \
    --statement-id "$STMT_ID" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/*" \
    2>/dev/null || true
}

# Get existing resource IDs
ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[?path==`/`].id' --output text)

V1_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[?path==`/api/v1`].id' --output text)

# ── /api/v1/voice/google-home/account-linking paths ──────────────────────────
VOICE_ID=$(get_or_create_resource    "$V1_ID" "voice")
GH_ID=$(get_or_create_resource       "$VOICE_ID" "google-home")
LINKING_ID=$(get_or_create_resource  "$GH_ID" "account-linking")

# GET + DELETE on /api/v1/voice/google-home/account-linking
add_method "$LINKING_ID" "GET"    "$LAMBDA_STATUS" "true"
add_method "$LINKING_ID" "DELETE" "$LAMBDA_UNLINK"  "true"
echo "    GET    /api/v1/voice/google-home/account-linking          -> $LAMBDA_STATUS (Cognito)"
echo "    DELETE /api/v1/voice/google-home/account-linking          -> $LAMBDA_UNLINK (Cognito)"

# /api/v1/voice/google-home/account-linking/deep-link
DEEPLINK_ID=$(get_or_create_resource "$LINKING_ID" "deep-link")

# POST /deep-link/start
START_RESOURCE_ID=$(get_or_create_resource "$DEEPLINK_ID" "start")
add_method "$START_RESOURCE_ID" "POST" "$LAMBDA_START" "true"
echo "    POST   /api/v1/voice/google-home/account-linking/deep-link/start   -> $LAMBDA_START (Cognito)"

# POST /deep-link/complete
COMPLETE_RESOURCE_ID=$(get_or_create_resource "$DEEPLINK_ID" "complete")
add_method "$COMPLETE_RESOURCE_ID" "POST" "$LAMBDA_COMPLETE" "true"
echo "    POST   /api/v1/voice/google-home/account-linking/deep-link/complete -> $LAMBDA_COMPLETE (Cognito)"

# ── /google-home/oauth + /google-home/fulfillment paths (public) ──────────────
ROOT_GH_ID=$(get_or_create_resource "$ROOT_ID" "google-home")

# /google-home/oauth/authorize (GET + POST, public)
OAUTH_ID=$(get_or_create_resource     "$ROOT_GH_ID" "oauth")
AUTHORIZE_ID=$(get_or_create_resource "$OAUTH_ID"   "authorize")
add_method "$AUTHORIZE_ID" "GET"  "$LAMBDA_AUTHORIZE" "false"
add_method "$AUTHORIZE_ID" "POST" "$LAMBDA_AUTHORIZE" "false"
echo "    GET    /google-home/oauth/authorize                        -> $LAMBDA_AUTHORIZE (public)"
echo "    POST   /google-home/oauth/authorize                        -> $LAMBDA_AUTHORIZE (public)"

# /google-home/oauth/token (POST, public — Google sends client credentials in body)
TOKEN_ID=$(get_or_create_resource "$OAUTH_ID" "token")
add_method "$TOKEN_ID" "POST" "$LAMBDA_TOKEN" "false"
echo "    POST   /google-home/oauth/token                            -> $LAMBDA_TOKEN (public)"

# /google-home/fulfillment (POST, public — Google validates via our access token)
FULFILL_ID=$(get_or_create_resource "$ROOT_GH_ID" "fulfillment")
add_method "$FULFILL_ID" "POST" "$LAMBDA_FULFILLMENT" "false"
echo "    POST   /google-home/fulfillment                            -> $LAMBDA_FULFILLMENT (public)"

# ── 5. Deploy API stage ───────────────────────────────────────────────────────
echo ""
echo "==> [5/6] Deploy API stage: smarthome"

aws apigateway create-deployment \
  --rest-api-id "$API_ID" --region "$REGION" \
  --stage-name "smarthome" \
  --description "Google Home Cloud-to-Cloud integration" > /dev/null

echo "    Deployed to stage: smarthome"

# ── 6. Log retention + CloudWatch alarms ─────────────────────────────────────
echo ""
echo "==> [6/6] Log retention + CloudWatch alarms"

ALL_LAMBDAS=(
  "$LAMBDA_START" "$LAMBDA_COMPLETE" "$LAMBDA_STATUS" "$LAMBDA_UNLINK"
  "$LAMBDA_AUTHORIZE" "$LAMBDA_TOKEN" "$LAMBDA_FULFILLMENT"
)

for FUNC in "${ALL_LAMBDAS[@]}"; do
  LOG_GROUP="/aws/lambda/$FUNC"
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "$LOG_GROUP" --retention-in-days 90 --region "$REGION"
done
echo "    Log retention set to 90 days for all 7 Lambdas"

create_alarm() {
  local ALARM_NAME="$1"
  local LOG_GROUP="$2"
  local FILTER_PATTERN="$3"
  local METRIC_NAME="$4"
  local THRESHOLD="${5:-1}"

  aws logs put-metric-filter \
    --log-group-name "$LOG_GROUP" \
    --filter-name "$METRIC_NAME" \
    --filter-pattern "$FILTER_PATTERN" \
    --metric-transformations \
      metricName="$METRIC_NAME",metricNamespace="DigiluxGoogleHome",metricValue=1,defaultValue=0 \
    --region "$REGION" 2>/dev/null || true

  aws cloudwatch put-metric-alarm \
    --alarm-name "$ALARM_NAME" --region "$REGION" \
    --metric-name "$METRIC_NAME" --namespace "DigiluxGoogleHome" \
    --statistic Sum --period 300 --evaluation-periods 1 \
    --threshold "$THRESHOLD" \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching 2>/dev/null || true
}

TOKEN_LOG="/aws/lambda/${LAMBDA_TOKEN}"
create_alarm "gh-invalid-client"        "$TOKEN_LOG" "GH_TOKEN_INVALID_CLIENT"   "GhInvalidClient"    1
create_alarm "gh-invalid-code"          "$TOKEN_LOG" "GH_TOKEN_EXCHANGE_INVALID_CODE" "GhInvalidCode" 1

AUTH_LOG="/aws/lambda/${LAMBDA_AUTHORIZE}"
create_alarm "gh-login-failed"          "$AUTH_LOG"  "GH_OAUTH_LOGIN_FAILED"     "GhLoginFailed"      5
create_alarm "gh-invalid-redirect"      "$AUTH_LOG"  "INVALID_REDIRECT_URI"      "GhInvalidRedirect"  1

for FUNC in "${ALL_LAMBDAS[@]}"; do
  LOG_GROUP="/aws/lambda/$FUNC"
  create_alarm "gh-config-error-${FUNC}" "$LOG_GROUP" "CONFIG_STARTUP_ERROR" "GhConfigError_${FUNC}" 1
done

echo "    CloudWatch alarms created"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Google Home integration deployed."
echo ""
echo " Flutter-facing endpoints (Cognito auth):"
echo "   GET    https://iot.digilux.co.in/smarthome/api/v1/voice/google-home/account-linking"
echo "   POST   https://iot.digilux.co.in/smarthome/api/v1/voice/google-home/account-linking/deep-link/start"
echo "   POST   https://iot.digilux.co.in/smarthome/api/v1/voice/google-home/account-linking/deep-link/complete"
echo "   DELETE https://iot.digilux.co.in/smarthome/api/v1/voice/google-home/account-linking"
echo ""
echo " OAuth / Fulfillment endpoints (public — Google calls these):"
echo "   GET    https://iot.digilux.co.in/smarthome/google-home/oauth/authorize"
echo "   POST   https://iot.digilux.co.in/smarthome/google-home/oauth/authorize"
echo "   POST   https://iot.digilux.co.in/smarthome/google-home/oauth/token"
echo "   POST   https://iot.digilux.co.in/smarthome/google-home/fulfillment"
echo ""
echo " DynamoDB tables:"
echo "   $GH_SESSIONS_TABLE"
echo "   $GH_AUTH_CODES_TABLE"
echo "   $GH_TOKENS_TABLE (GSIs: accessToken-index, refreshToken-index)"
echo ""
echo " Next steps:"
echo "   1. Register OAuth endpoints in Google Actions Console:"
echo "      Authorization: https://iot.digilux.co.in/smarthome/google-home/oauth/authorize"
echo "      Token:         https://iot.digilux.co.in/smarthome/google-home/oauth/token"
echo "   2. Register fulfillment webhook in Actions Console:"
echo "      https://iot.digilux.co.in/smarthome/google-home/fulfillment"
echo "   3. Set GOOGLE_SECRET_ARN in Secrets Manager:"
echo "      {\"client_id\": \"...\", \"client_secret\": \"...\"}"
echo "   4. Add GOOGLE_REDIRECT_URI to ALLOWED_REDIRECT_URIS in google_home_oauth_authorize"
echo "   5. Test end-to-end via Flutter app"
echo "=================================================="
