#!/bin/bash
# poc_cleanup_region_test.sh
# ---------------------------------------------------------------------------
# Deletes all AWS resources created by poc_deploy_region_test.sh.
# Run this after the POC test is complete.
#
# Resources deleted:
#   Lambda    : alexa-poc-routing-eu-west-2              (eu-west-2)
#   Lambda    : alexa-poc-routing-eu-central-1           (eu-central-1)
#   CW logs   : /aws/lambda/alexa-poc-routing-eu-west-2  (eu-west-2)
#   CW logs   : /aws/lambda/alexa-poc-routing-eu-central-1 (eu-central-1)
#   IAM role  : digilux-poc-alexa-routing-role           (global)
# ---------------------------------------------------------------------------

set -euo pipefail

ROLE_NAME="digilux-poc-alexa-routing-role"
FUNC_BASE="alexa-poc-routing"

echo "=================================================="
echo " Digilux — Alexa Region Routing POC — Cleanup"
echo "=================================================="

# ── Delete Lambda functions + log groups ─────────────────────────────────────
for REGION in "eu-west-2" "eu-central-1"; do
  FUNC_NAME="${FUNC_BASE}-${REGION}"
  LOG_GROUP="/aws/lambda/${FUNC_NAME}"

  echo ""
  echo "--- Region: $REGION ---"

  echo "    Deleting Lambda: $FUNC_NAME"
  aws lambda delete-function \
    --function-name "$FUNC_NAME" \
    --region "$REGION" 2>/dev/null \
    && echo "    Deleted" \
    || echo "    Not found — skipping"

  echo "    Deleting log group: $LOG_GROUP"
  aws logs delete-log-group \
    --log-group-name "$LOG_GROUP" \
    --region "$REGION" 2>/dev/null \
    && echo "    Deleted" \
    || echo "    Not found — skipping"
done

# ── Delete IAM role ───────────────────────────────────────────────────────────
echo ""
echo "--- IAM Role: $ROLE_NAME ---"

aws iam detach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
  2>/dev/null || true

aws iam delete-role \
  --role-name "$ROLE_NAME" 2>/dev/null \
  && echo "    Deleted" \
  || echo "    Not found — skipping"

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Cleanup complete. All POC resources removed."
echo ""
echo " REMINDER: If you haven't already, restore the Alexa skill"
echo " EU/India endpoint back to the production Lambda:"
echo "   arn:aws:lambda:eu-west-1:986906626244:function:alexa"
echo "=================================================="
