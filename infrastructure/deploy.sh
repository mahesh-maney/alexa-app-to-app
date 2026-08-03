#!/bin/bash
# deploy.sh — Alexa App-to-App Account Linking infrastructure
#
# What this script does:
#   1. Creates DynamoDB tables + KMS key
#   2. Creates separate least-privilege IAM roles per Lambda
#   3. Packages and deploys all 4 Lambda functions with X-Ray tracing
#   4. Creates AWS WAF WebACL with rate-based rule + common rule set
#   5. Wires routes into the existing digilux-honeywell-user-device-api
#   6. Deploys the updated API stage
#   7. Creates CloudWatch metric filters + alarms for security events
#
# Prerequisites:
#   aws cli configured (ap-south-1)
#   jq installed  (brew install jq)
#
# Usage:
#   chmod +x deploy.sh && ./deploy.sh

set -euo pipefail

REGION="ap-south-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Existing infrastructure (do not change)
API_ID="5sros9vjc2"
AUTHORIZER_ID="g9cj6w"
LWA_SECRET_ARN="arn:aws:secretsmanager:eu-west-1:${ACCOUNT_ID}:secret:digilux/alexa/lwa-7RDOUm"
REDIRECT_URI="https://iot.digilux.co.in/smarthome/alexa/callback"
ALLOWED_REDIRECT_HOSTS="iot.digilux.co.in"

# Resource names
SESSION_TABLE="alexa_app_linking_sessions"
LWA_TOKENS_TABLE="digilux_honeywell_alexa_lwa_tokens"
LAMBDA_START="alexa_start_app_to_app"
LAMBDA_COMPLETE="alexa_complete_app_to_app"
LAMBDA_CALLBACK="alexa_callback"
LAMBDA_UNLINK="alexa_unlink"

# IAM role names (one per Lambda for least privilege)
ROLE_START="digilux-alexa-start-role"
ROLE_COMPLETE="digilux-alexa-complete-role"
ROLE_CALLBACK="digilux-alexa-callback-role"
ROLE_UNLINK="digilux-alexa-unlink-role"

# Alexa skill
ALEXA_SKILL_ID="${ALEXA_SKILL_ID:-amzn1.ask.skill.YOUR_SKILL_ID}"
ALEXA_SKILL_STAGE="live"
SKILL_ENABLEMENT_URL="https://api.amazonalexa.com/v1/users/~current/skills"
LWA_REVOKE_URL="https://api.amazon.com/auth/o2/revoke"

# Cognito (set COGNITO_USER_POOL_ID env var before running in production)
COGNITO_USER_POOL_ID="${COGNITO_USER_POOL_ID:-}"
COGNITO_REGION="${COGNITO_REGION:-${REGION}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAMBDA_DIR="${SCRIPT_DIR}/../lambdas"

echo "=================================================="
echo " Digilux — Alexa App-to-App Account Linking"
echo " Account : $ACCOUNT_ID"
echo " Region  : $REGION"
echo "=================================================="

# ── 1. DynamoDB tables + KMS key ─────────────────────────────────────────────
echo ""
echo "==> [1/7] DynamoDB tables + KMS key"

# Sessions table
if aws dynamodb describe-table --table-name "$SESSION_TABLE" --region "$REGION" \
     --query 'Table.TableName' --output text 2>/dev/null | grep -q "$SESSION_TABLE"; then
  echo "    Table already exists: $SESSION_TABLE"
else
  aws dynamodb create-table \
    --table-name "$SESSION_TABLE" \
    --region "$REGION" \
    --attribute-definitions AttributeName=state,AttributeType=S \
    --key-schema AttributeName=state,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST > /dev/null

  aws dynamodb update-time-to-live \
    --table-name "$SESSION_TABLE" \
    --region "$REGION" \
    --time-to-live-specification "Enabled=true,AttributeName=ttl" > /dev/null

  echo "    Created: $SESSION_TABLE (TTL on 'ttl')"
fi

# LWA tokens table
if aws dynamodb describe-table --table-name "$LWA_TOKENS_TABLE" --region "$REGION" \
     --query 'Table.TableName' --output text 2>/dev/null | grep -q "$LWA_TOKENS_TABLE"; then
  echo "    Table already exists: $LWA_TOKENS_TABLE"
else
  aws dynamodb create-table \
    --table-name "$LWA_TOKENS_TABLE" \
    --region "$REGION" \
    --attribute-definitions AttributeName=userId,AttributeType=S \
    --key-schema AttributeName=userId,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST > /dev/null

  echo "    Created: $LWA_TOKENS_TABLE"
fi

# KMS key for field-level encryption
KMS_KEY_ARN=$(aws kms list-aliases --region "$REGION" \
  --query "Aliases[?AliasName=='alias/digilux-alexa-tokens'].TargetKeyId" \
  --output text 2>/dev/null || echo "")

if [ -z "$KMS_KEY_ARN" ] || [ "$KMS_KEY_ARN" = "None" ]; then
  KEY_ID=$(aws kms create-key \
    --region "$REGION" \
    --description "Digilux Alexa token field-level encryption" \
    --key-usage ENCRYPT_DECRYPT \
    --query 'KeyMetadata.KeyId' --output text)
  aws kms create-alias \
    --alias-name "alias/digilux-alexa-tokens" \
    --target-key-id "$KEY_ID" \
    --region "$REGION"
  KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KEY_ID}"
  echo "    KMS key created: $KMS_KEY_ARN"
else
  KMS_KEY_ARN="arn:aws:kms:${REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ARN}"
  echo "    KMS key exists: alias/digilux-alexa-tokens"
fi

# ── 2. Separate IAM roles (least privilege per Lambda) ───────────────────────
echo ""
echo "==> [2/7] IAM roles (one per Lambda)"

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
  "Resource": "arn:aws:logs:'"${REGION}"':'"${ACCOUNT_ID}"':log-group:/aws/lambda/alexa_*:*"
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
    aws iam attach-role-policy \
      --role-name "$ROLE" \
      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    echo "    Created role: $ROLE"
  fi

  aws iam put-role-policy \
    --role-name "$ROLE" \
    --policy-name "permissions" \
    --policy-document "$POLICY_JSON"
}

# start role: DDB sessions (GetItem, PutItem, UpdateItem) + KMS encrypt
create_or_update_role "$ROLE_START" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SessionTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${SESSION_TABLE}"
    },
    {
      "Sid": "KmsEncrypt",
      "Effect": "Allow",
      "Action": ["kms:Encrypt","kms:GenerateDataKey"],
      "Resource": "${KMS_KEY_ARN}"
    },
    ${LOGS_STMT},
    ${XRAY_STMT}
  ]
}
POLICY
)"

# complete role: DDB sessions (GetItem, UpdateItem) + tokens (PutItem) + Secrets + KMS decrypt+encrypt
create_or_update_role "$ROLE_COMPLETE" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SessionTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${SESSION_TABLE}"
    },
    {
      "Sid": "TokensTable",
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${LWA_TOKENS_TABLE}"
    },
    {
      "Sid": "LwaSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "${LWA_SECRET_ARN}"
    },
    {
      "Sid": "KmsCrypto",
      "Effect": "Allow",
      "Action": ["kms:Decrypt","kms:Encrypt","kms:GenerateDataKey"],
      "Resource": "${KMS_KEY_ARN}"
    },
    ${LOGS_STMT},
    ${XRAY_STMT}
  ]
}
POLICY
)"

# callback role: logs only (no DDB, no secrets)
create_or_update_role "$ROLE_CALLBACK" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [${LOGS_STMT}, ${XRAY_STMT}]
}
POLICY
)"

# unlink role: tokens table (GetItem, DeleteItem) + Secrets + KMS decrypt
create_or_update_role "$ROLE_UNLINK" "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TokensTable",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${LWA_TOKENS_TABLE}"
    },
    {
      "Sid": "LwaSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "${LWA_SECRET_ARN}"
    },
    {
      "Sid": "KmsDecrypt",
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "${KMS_KEY_ARN}"
    },
    ${LOGS_STMT},
    ${XRAY_STMT}
  ]
}
POLICY
)"

echo "    Waiting 10s for IAM propagation..."
sleep 10

ROLE_START_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_START}"
ROLE_COMPLETE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_COMPLETE}"
ROLE_CALLBACK_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_CALLBACK}"
ROLE_UNLINK_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_UNLINK}"

# ── 3. Package + deploy Lambda functions ──────────────────────────────────────
echo ""
echo "==> [3/7] Lambda functions (with X-Ray tracing)"

deploy_lambda() {
  local FUNC_NAME="$1"
  local SOURCE_DIR="$2"
  local ROLE="$3"
  local ENV_VARS="$4"
  local TIMEOUT="${5:-30}"
  local ZIP_FILE="/tmp/${FUNC_NAME}.zip"

  cd "$SOURCE_DIR"
  zip -q "$ZIP_FILE" lambda_function.py
  cd - > /dev/null

  if aws lambda get-function --function-name "$FUNC_NAME" --region "$REGION" \
       --query 'Configuration.FunctionName' --output text 2>/dev/null | grep -q "$FUNC_NAME"; then
    aws lambda update-function-code \
      --function-name "$FUNC_NAME" \
      --region "$REGION" \
      --zip-file "fileb://${ZIP_FILE}" > /dev/null
    aws lambda wait function-updated \
      --function-name "$FUNC_NAME" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$FUNC_NAME" \
      --region "$REGION" \
      --environment "Variables=${ENV_VARS}" \
      --timeout "$TIMEOUT" \
      --tracing-config Mode=Active > /dev/null
    echo "    Updated: $FUNC_NAME"
  else
    aws lambda create-function \
      --function-name "$FUNC_NAME" \
      --region "$REGION" \
      --runtime python3.12 \
      --handler lambda_function.lambda_handler \
      --role "$ROLE" \
      --zip-file "fileb://${ZIP_FILE}" \
      --environment "Variables=${ENV_VARS}" \
      --timeout "$TIMEOUT" \
      --memory-size 256 \
      --tracing-config Mode=Active > /dev/null
    aws lambda wait function-active \
      --function-name "$FUNC_NAME" --region "$REGION"
    echo "    Created: $FUNC_NAME"
  fi
}

# start env vars
START_ENV="{
  DATA_REGION=${REGION},
  SESSION_TABLE=${SESSION_TABLE},
  REDIRECT_URI=${REDIRECT_URI},
  SESSION_TTL_SECONDS=600,
  MAX_PENDING_SESSIONS_PER_USER=5,
  ALLOWED_REDIRECT_HOSTS=${ALLOWED_REDIRECT_HOSTS},
  KMS_KEY_ARN=${KMS_KEY_ARN},
  COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},
  COGNITO_REGION=${COGNITO_REGION},
  LOG_LEVEL=INFO
}"

# complete env vars
COMPLETE_ENV="{
  DATA_REGION=${REGION},
  SESSION_TABLE=${SESSION_TABLE},
  LWA_TOKENS_TABLE=${LWA_TOKENS_TABLE},
  REDIRECT_URI=${REDIRECT_URI},
  LWA_SECRET_ARN=${LWA_SECRET_ARN},
  LWA_SECRET_REGION=eu-west-1,
  LWA_TOKEN_URL=https://api.amazon.com/auth/o2/token,
  ALEXA_SKILL_ID=${ALEXA_SKILL_ID},
  ALEXA_SKILL_STAGE=${ALEXA_SKILL_STAGE},
  SKILL_ENABLEMENT_URL=${SKILL_ENABLEMENT_URL},
  LWA_HTTP_TIMEOUT=10,
  TOKEN_EXPIRY_BUFFER_SECONDS=60,
  MAX_REQUEST_BODY_BYTES=4096,
  MAX_AUTH_CODE_LEN=2048,
  KMS_KEY_ARN=${KMS_KEY_ARN},
  COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},
  COGNITO_REGION=${COGNITO_REGION},
  LWA_REVOKE_URL=${LWA_REVOKE_URL},
  LOG_LEVEL=INFO
}"

# callback env vars
CALLBACK_ENV="{
  APP_SCHEME=digilux,
  LOG_LEVEL=INFO
}"

# unlink env vars
UNLINK_ENV="{
  DATA_REGION=${REGION},
  LWA_TOKENS_TABLE=${LWA_TOKENS_TABLE},
  LWA_SECRET_ARN=${LWA_SECRET_ARN},
  LWA_SECRET_REGION=eu-west-1,
  LWA_REVOKE_URL=${LWA_REVOKE_URL},
  SKILL_ENABLEMENT_URL=${SKILL_ENABLEMENT_URL},
  ALEXA_SKILL_ID=${ALEXA_SKILL_ID},
  KMS_KEY_ARN=${KMS_KEY_ARN},
  COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID},
  COGNITO_REGION=${COGNITO_REGION},
  LWA_HTTP_TIMEOUT=10,
  LOG_LEVEL=INFO
}"

deploy_lambda "$LAMBDA_START"    "${LAMBDA_DIR}/alexa_start_app_to_app"    "$ROLE_START_ARN"    "$START_ENV"    15
deploy_lambda "$LAMBDA_COMPLETE" "${LAMBDA_DIR}/alexa_complete_app_to_app" "$ROLE_COMPLETE_ARN" "$COMPLETE_ENV" 30
deploy_lambda "$LAMBDA_CALLBACK" "${LAMBDA_DIR}/alexa_callback"            "$ROLE_CALLBACK_ARN" "$CALLBACK_ENV" 10
deploy_lambda "$LAMBDA_UNLINK"   "${LAMBDA_DIR}/alexa_unlink"              "$ROLE_UNLINK_ARN"   "$UNLINK_ENV"   15

# ── 4. AWS WAF WebACL ─────────────────────────────────────────────────────────
echo ""
echo "==> [4/7] AWS WAF WebACL"

WAF_ACL_NAME="digilux-alexa-waf"
WAF_ACL_ID=$(aws wafv2 list-web-acls \
  --scope REGIONAL --region "$REGION" \
  --query "WebACLs[?Name=='${WAF_ACL_NAME}'].Id" \
  --output text 2>/dev/null || echo "")

if [ -n "$WAF_ACL_ID" ] && [ "$WAF_ACL_ID" != "None" ]; then
  echo "    WAF WebACL already exists: $WAF_ACL_NAME"
else
  WAF_ACL_ARN=$(aws wafv2 create-web-acl \
    --name "$WAF_ACL_NAME" \
    --scope REGIONAL \
    --region "$REGION" \
    --default-action Allow={} \
    --rules '[
      {
        "Name": "RateLimitPerIP",
        "Priority": 1,
        "Statement": {
          "RateBasedStatement": {
            "Limit": 100,
            "AggregateKeyType": "IP"
          }
        },
        "Action": {"Block": {}},
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "RateLimitPerIP"
        }
      },
      {
        "Name": "AWSCommonRules",
        "Priority": 2,
        "OverrideAction": {"None": {}},
        "Statement": {
          "ManagedRuleGroupStatement": {
            "VendorName": "AWS",
            "Name": "AWSManagedRulesCommonRuleSet"
          }
        },
        "VisibilityConfig": {
          "SampledRequestsEnabled": true,
          "CloudWatchMetricsEnabled": true,
          "MetricName": "AWSCommonRules"
        }
      }
    ]' \
    --visibility-config "SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=${WAF_ACL_NAME}" \
    --query 'Summary.ARN' --output text)

  # Associate WAF with API Gateway stage
  API_STAGE_ARN="arn:aws:apigateway:${REGION}::/restapis/${API_ID}/stages/prod"
  aws wafv2 associate-web-acl \
    --web-acl-arn "$WAF_ACL_ARN" \
    --resource-arn "$API_STAGE_ARN" \
    --region "$REGION" 2>/dev/null || \
    echo "    Warning: WAF association may require API stage to exist first — run again after step 6"

  echo "    Created WAF WebACL: $WAF_ACL_NAME (100 req/5min/IP)"
fi

# ── 5. Wire API Gateway routes ────────────────────────────────────────────────
echo ""
echo "==> [5/7] API Gateway routes"

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
      --rest-api-id "$API_ID" \
      --region "$REGION" \
      --parent-id "$PARENT_ID" \
      --path-part "$PATH_PART" \
      --query 'id' --output text
  fi
}

add_method_with_auth() {
  local RESOURCE_ID="$1"
  local HTTP_METHOD="$2"
  local LAMBDA_NAME="$3"
  local USE_AUTH="${4:-true}"

  LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}"
  INTEGRATION_URI="arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN}/invocations"

  aws apigateway put-method \
    --rest-api-id "$API_ID" \
    --region "$REGION" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --authorization-type "$([ "$USE_AUTH" = "true" ] && echo "COGNITO_USER_POOLS" || echo "NONE")" \
    $([ "$USE_AUTH" = "true" ] && echo "--authorizer-id $AUTHORIZER_ID") \
    --no-api-key-required 2>/dev/null || true

  aws apigateway put-integration \
    --rest-api-id "$API_ID" \
    --region "$REGION" \
    --resource-id "$RESOURCE_ID" \
    --http-method "$HTTP_METHOD" \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri "$INTEGRATION_URI" > /dev/null

  STATEMENT_ID="apigw-$(echo "$RESOURCE_ID" | cut -c1-8)-${HTTP_METHOD,,}"
  aws lambda add-permission \
    --function-name "$LAMBDA_NAME" \
    --region "$REGION" \
    --statement-id "$STATEMENT_ID" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/${HTTP_METHOD}/*" \
    2>/dev/null || true
}

ROOT_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[?path==`/`].id' --output text)

V1_ID=$(aws apigateway get-resources \
  --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[?path==`/api/v1`].id' --output text)

ALEXA_ID=$(get_or_create_resource "$V1_ID" "alexa")
echo "    /api/v1/alexa resource: $ALEXA_ID"

START_ID=$(get_or_create_resource "$ALEXA_ID" "startAppToApp")
add_method_with_auth "$START_ID" "POST" "$LAMBDA_START" "true"
echo "    POST   /api/v1/alexa/startAppToApp    -> $LAMBDA_START (Cognito auth)"

COMPLETE_ID=$(get_or_create_resource "$ALEXA_ID" "completeAppToApp")
add_method_with_auth "$COMPLETE_ID" "POST" "$LAMBDA_COMPLETE" "true"
echo "    POST   /api/v1/alexa/completeAppToApp -> $LAMBDA_COMPLETE (Cognito auth)"

UNLINK_ID=$(get_or_create_resource "$ALEXA_ID" "unlink")
add_method_with_auth "$UNLINK_ID" "DELETE" "$LAMBDA_UNLINK" "true"
echo "    DELETE /api/v1/alexa/unlink           -> $LAMBDA_UNLINK (Cognito auth)"

ROOT_ALEXA_ID=$(get_or_create_resource "$ROOT_ID" "alexa")
CALLBACK_ID=$(get_or_create_resource "$ROOT_ALEXA_ID" "callback")
add_method_with_auth "$CALLBACK_ID" "GET" "$LAMBDA_CALLBACK" "false"
echo "    GET    /alexa/callback                -> $LAMBDA_CALLBACK (no auth)"

# ── 6. Deploy API stage ───────────────────────────────────────────────────────
echo ""
echo "==> [6/7] Deploy API stage"

aws apigateway create-deployment \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --stage-name "prod" \
  --description "Alexa App-to-App account linking" > /dev/null

echo "    Deployed to stage: prod"

# Log retention (90 days) + CloudWatch alarms
echo ""
echo "==> [7/7] Log retention + CloudWatch alarms"

for FUNC in "$LAMBDA_START" "$LAMBDA_COMPLETE" "$LAMBDA_CALLBACK" "$LAMBDA_UNLINK"; do
  LOG_GROUP="/aws/lambda/$FUNC"
  aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days 90 \
    --region "$REGION"
done
echo "    Log retention set to 90 days for all 4 Lambdas"

# CloudWatch metric filters + alarms for security events
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
      metricName="$METRIC_NAME",metricNamespace="DigiluxAlexa",metricValue=1,defaultValue=0 \
    --region "$REGION" 2>/dev/null || true

  aws cloudwatch put-metric-alarm \
    --alarm-name "$ALARM_NAME" \
    --region "$REGION" \
    --metric-name "$METRIC_NAME" \
    --namespace "DigiluxAlexa" \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --threshold "$THRESHOLD" \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --treat-missing-data notBreaching 2>/dev/null || true
}

COMPLETE_LOG="/aws/lambda/${LAMBDA_COMPLETE}"
create_alarm "alexa-session-already-used"   "$COMPLETE_LOG" "SESSION_ALREADY_USED"   "SessionAlreadyUsed"   1
create_alarm "alexa-session-owner-mismatch" "$COMPLETE_LOG" "SESSION_OWNER_MISMATCH" "SessionOwnerMismatch" 1
create_alarm "alexa-lwa-exchange-failed"    "$COMPLETE_LOG" "LWA_EXCHANGE_FAILED"    "LwaExchangeFailed"    1
create_alarm "alexa-skill-enable-failed"    "$COMPLETE_LOG" "SKILL_ENABLE_FAILED"    "SkillEnableFailed"    1

START_LOG="/aws/lambda/${LAMBDA_START}"
create_alarm "alexa-auth-failed"            "$START_LOG"    "AUTH_FAILED"            "AuthFailed"           5
create_alarm "alexa-rate-limit-exceeded"    "$START_LOG"    "RATE_LIMIT_EXCEEDED"    "RateLimitExceeded"    1

for FUNC in "$LAMBDA_START" "$LAMBDA_COMPLETE" "$LAMBDA_CALLBACK" "$LAMBDA_UNLINK"; do
  LOG_GROUP="/aws/lambda/$FUNC"
  create_alarm "alexa-config-error-${FUNC}" "$LOG_GROUP" "CONFIG_STARTUP_ERROR" "ConfigStartupError_${FUNC}" 1
done

echo "    CloudWatch alarms created for: SESSION_ALREADY_USED, SESSION_OWNER_MISMATCH,"
echo "    LWA_EXCHANGE_FAILED, SKILL_ENABLE_FAILED, AUTH_FAILED, RATE_LIMIT_EXCEEDED, CONFIG_STARTUP_ERROR"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Deployment complete."
echo ""
echo " Endpoints:"
echo "   POST   https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/api/v1/alexa/startAppToApp"
echo "   POST   https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/api/v1/alexa/completeAppToApp"
echo "   DELETE https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/api/v1/alexa/unlink"
echo "   GET    https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod/alexa/callback"
echo ""
echo " DynamoDB tables : $SESSION_TABLE, $LWA_TOKENS_TABLE"
echo " KMS key         : $KMS_KEY_ARN"
echo " WAF WebACL      : $WAF_ACL_NAME"
echo ""
echo " Next steps:"
echo "   1. Set COGNITO_USER_POOL_ID env var and re-run to enable JWT sig verification"
echo "   2. Set ALEXA_SKILL_ID env var before first deploy"
echo "   3. Register redirect URI in Alexa Developer Console:"
echo "      https://iot.digilux.co.in/smarthome/alexa/callback"
echo "   4. No web server needed — callback is served directly by the Lambda"
echo "=================================================="
