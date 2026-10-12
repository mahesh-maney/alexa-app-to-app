#!/bin/bash
# poc_deploy_region_test.sh
# ---------------------------------------------------------------------------
# Deploys a minimal routing-probe Lambda to eu-west-2 AND eu-central-1 to
# test whether EN-IN Alexa voice directives reach either region.
#
# The probe Lambda does NOT need DynamoDB, Secrets Manager, or a pycrypto
# layer — it simply logs the incoming directive and returns {}.
# That is sufficient to prove routing.
#
# Resources created (all cleaned up by poc_cleanup_region_test.sh):
#   IAM role  : digilux-poc-alexa-routing-role          (global)
#   Lambda    : alexa-poc-routing-eu-west-2              (eu-west-2)
#   Lambda    : alexa-poc-routing-eu-central-1           (eu-central-1)
#   CW logs   : /aws/lambda/alexa-poc-routing-eu-west-2  (eu-west-2, auto-created)
#   CW logs   : /aws/lambda/alexa-poc-routing-eu-central-1 (eu-central-1, auto-created)
#
# NOTE: The Alexa Developer Console only supports ONE EU/India endpoint at a
# time. After deploying both Lambdas, test sequentially:
#   Round 1 — set skill endpoint to eu-west-2 ARN, run voice commands
#   Round 2 — set skill endpoint to eu-central-1 ARN, run voice commands
#
# Usage:
#   chmod +x infrastructure/poc_deploy_region_test.sh
#   ./infrastructure/poc_deploy_region_test.sh
# ---------------------------------------------------------------------------

set -euo pipefail

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME="digilux-poc-alexa-routing-role"
FUNC_BASE="alexa-poc-routing"
SKILL_ID="amzn1.ask.skill.fc2dcda9-a8ce-459f-b714-746e84f0ccba"

echo "=================================================="
echo " Digilux — Alexa Region Routing POC"
echo " Account : $ACCOUNT_ID"
echo " Regions : eu-west-2, eu-central-1"
echo "=================================================="

# ── 1. IAM Role (global — shared by both regional Lambdas) ───────────────────
echo ""
echo "==> [1/3] IAM role: $ROLE_NAME"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "lambda.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}'

if aws iam get-role --role-name "$ROLE_NAME" 2>/dev/null | grep -q "$ROLE_NAME"; then
  echo "    Already exists — skipping create"
else
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" > /dev/null
  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  echo "    Created"
  echo "    Waiting 10s for IAM propagation..."
  sleep 10
fi

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"
echo "    Role ARN: $ROLE_ARN"

# ── 2. Package probe Lambda ───────────────────────────────────────────────────
echo ""
echo "==> [2/3] Packaging probe Lambda"

cat > /tmp/poc_lambda_function.py << 'PYEOF'
"""
Alexa region-routing probe Lambda.
Logs every incoming directive so CloudWatch proves which region received it.
Returns {} — sufficient for routing validation, Alexa will log a skill error
but the invocation itself is what we are measuring.
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_REGION", "unknown")


def lambda_handler(event, context):
    header    = event.get("directive", {}).get("header", {})
    namespace = header.get("namespace", "Unknown")
    name      = header.get("name", "Unknown")
    user_id   = (
        event.get("directive", {})
             .get("endpoint", {})
             .get("scope", {})
             .get("token", "no-token")[:20]
    )
    logger.info(
        "[POC-ROUTING] region=%s directive=%s/%s user_prefix=%s",
        _REGION, namespace, name, user_id
    )
    logger.info("[POC-ROUTING] full_event=%s", json.dumps(event))
    # Minimal response — enough to confirm invocation in CloudWatch
    return {}
PYEOF

cd /tmp
zip -q poc_lambda.zip poc_lambda_function.py
cd - > /dev/null

echo "    Packaged: /tmp/poc_lambda.zip"

# ── 3. Deploy to each region ──────────────────────────────────────────────────
deploy_to_region() {
  local REGION="$1"
  local FUNC_NAME="${FUNC_BASE}-${REGION}"

  echo ""
  echo "--- Region: $REGION  |  Function: $FUNC_NAME ---"

  if aws lambda get-function \
       --function-name "$FUNC_NAME" \
       --region "$REGION" \
       --query 'Configuration.FunctionName' \
       --output text 2>/dev/null | grep -q "$FUNC_NAME"; then

    echo "    Function exists — updating code"
    aws lambda update-function-code \
      --function-name "$FUNC_NAME" \
      --region "$REGION" \
      --zip-file fileb:///tmp/poc_lambda.zip > /dev/null
    aws lambda wait function-updated \
      --function-name "$FUNC_NAME" --region "$REGION"
    echo "    Code updated"
  else
    echo "    Creating function"
    aws lambda create-function \
      --function-name "$FUNC_NAME" \
      --region "$REGION" \
      --runtime python3.12 \
      --handler poc_lambda_function.lambda_handler \
      --role "$ROLE_ARN" \
      --zip-file fileb:///tmp/poc_lambda.zip \
      --timeout 15 \
      --memory-size 256 > /dev/null
    aws lambda wait function-active \
      --function-name "$FUNC_NAME" --region "$REGION"
    echo "    Created"
  fi

  # Grant Alexa Smart Home service permission to invoke this Lambda
  aws lambda add-permission \
    --function-name "$FUNC_NAME" \
    --region "$REGION" \
    --statement-id "alexa-smarthome-poc" \
    --action lambda:InvokeFunction \
    --principal alexa-connectedhome.amazon.com \
    --event-source-token "$SKILL_ID" \
    2>/dev/null \
    && echo "    Alexa invoke permission added" \
    || echo "    Alexa invoke permission already exists (ok)"

  FUNC_ARN=$(aws lambda get-function \
    --function-name "$FUNC_NAME" \
    --region "$REGION" \
    --query 'Configuration.FunctionArn' \
    --output text)

  echo "    ARN: $FUNC_ARN"
}

echo ""
echo "==> [3/3] Deploying probe Lambda to both regions"
deploy_to_region "eu-west-2"
deploy_to_region "eu-central-1"

# ── Summary + instructions ────────────────────────────────────────────────────
EU_WEST_2_ARN="arn:aws:lambda:eu-west-2:${ACCOUNT_ID}:function:${FUNC_BASE}-eu-west-2"
EU_CENTRAL_1_ARN="arn:aws:lambda:eu-central-1:${ACCOUNT_ID}:function:${FUNC_BASE}-eu-central-1"

echo ""
echo "=================================================="
echo " Deployment Complete — Resources Created"
echo ""
echo " IAM role       : $ROLE_NAME (global)"
echo " Lambda         : ${FUNC_BASE}-eu-west-2      (eu-west-2)"
echo " Lambda         : ${FUNC_BASE}-eu-central-1   (eu-central-1)"
echo " CW log group   : /aws/lambda/${FUNC_BASE}-eu-west-2"
echo " CW log group   : /aws/lambda/${FUNC_BASE}-eu-central-1"
echo ""
echo " Lambda ARNs:"
echo "   eu-west-2   : $EU_WEST_2_ARN"
echo "   eu-central-1: $EU_CENTRAL_1_ARN"
echo ""
echo "=================================================="
echo " NEXT STEPS — Test eu-west-2 first (Honeywell preference)"
echo ""
echo " 1. Open Alexa Developer Console:"
echo "    https://developer.amazon.com/alexa/console/ask"
echo " 2. Open skill: fc2dcda9-a8ce-459f-b714-746e84f0ccba"
echo " 3. Build > Smart Home > Default endpoint"
echo " 4. Set endpoint to: $EU_WEST_2_ARN"
echo " 5. Save and rebuild"
echo " 6. In Test tab (locale: English IN), issue:"
echo "      - 'discover my devices'"
echo "      - 'turn on living room light'"
echo "      - 'turn off living room light'"
echo " 7. Check CloudWatch in eu-west-2:"
echo "    /aws/lambda/${FUNC_BASE}-eu-west-2"
echo "    Look for: [POC-ROUTING] region=eu-west-2 directive=..."
echo ""
echo " If eu-west-2 passes, repeat steps 4-7 with eu-central-1 ARN."
echo ""
echo " IMPORTANT — When done testing:"
echo "   - Restore the skill endpoint to the production eu-west-1 ARN:"
echo "     arn:aws:lambda:eu-west-1:986906626244:function:alexa"
echo "   - Run: ./infrastructure/poc_cleanup_region_test.sh"
echo "=================================================="
