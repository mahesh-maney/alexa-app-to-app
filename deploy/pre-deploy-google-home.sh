#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# pre-deploy-google-home.sh
#
# Interactive wizard that collects every parameter required to deploy the
# Google Home Cloud-to-Cloud account linking feature to a new AWS account.
# Outputs a JSON config file compatible with deploy-google-home.sh.
#
# Usage:
#   ./deploy/pre-deploy-google-home.sh
#   ./deploy/pre-deploy-google-home.sh --config deploy/env.myclient.json  # pre-fill
#
# Output: deploy/env.<brand>.json  (chmod 600)
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
section() { echo; echo -e "${BOLD}──────────────────────────────────────────${RESET}"; echo -e "${BOLD}  $*${RESET}"; echo -e "${BOLD}──────────────────────────────────────────${RESET}"; }

# ── Parse arguments ───────────────────────────────────────────────────────────
EXISTING_CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) EXISTING_CONFIG="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--config deploy/env.existing.json]"
      echo "  --config  Pre-fill answers from an existing config file (re-run safe)"
      exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

# ── Prerequisite check ────────────────────────────────────────────────────────
section "Checking prerequisites"
for cmd in aws jq python3 zip; do
  if command -v "$cmd" &>/dev/null; then
    success "$cmd found"
  else
    die "$cmd is required but not installed. Please install it and re-run."
  fi
done

AWS_IDENTITY=$(aws sts get-caller-identity 2>/dev/null) \
  || die "AWS CLI not authenticated. Run 'aws configure' or set AWS env vars."
AWS_ACCOUNT_ID=$(echo "$AWS_IDENTITY" | jq -r '.Account')
AWS_USER_ARN=$(echo "$AWS_IDENTITY" | jq -r '.Arn')
success "AWS authenticated: $AWS_USER_ARN (account $AWS_ACCOUNT_ID)"

# ── Load existing config ──────────────────────────────────────────────────────
declare -A CFG=()

load_existing() {
  local file="$1"
  [[ -f "$file" ]] || { warn "Config file not found: $file (starting fresh)"; return; }
  info "Loading existing config from $file"
  while IFS='=' read -r key value; do
    CFG["$key"]="$value"
  done < <(jq -r 'to_entries[] | .key + "=" + (.value // "")' "$file")
  success "Loaded ${#CFG[@]} values"
}

[[ -n "$EXISTING_CONFIG" ]] && load_existing "$EXISTING_CONFIG"

# ── Helper: prompt with default ───────────────────────────────────────────────
# prompt <varname> <question> <default>
prompt() {
  local varname="$1" question="$2" default="${3:-}"
  local display_default=""
  [[ -n "$default" ]] && display_default=" [${CYAN}${default}${RESET}]"
  echo -en "  ${question}${display_default}: "
  read -r input
  if [[ -z "$input" && -n "$default" ]]; then
    CFG["$varname"]="$default"
  else
    CFG["$varname"]="$input"
  fi
}

# prompt_secret <varname> <question> <default>
prompt_secret() {
  local varname="$1" question="$2" default="${3:-}"
  local display_default=""
  [[ -n "$default" ]] && display_default=" [${CYAN}****${RESET}]"
  echo -en "  ${question}${display_default}: "
  read -rs input
  echo
  if [[ -z "$input" && -n "$default" ]]; then
    CFG["$varname"]="$default"
  else
    CFG["$varname"]="$input"
  fi
}

# prompt_yn <varname> <question> <default y|n>
prompt_yn() {
  local varname="$1" question="$2" default="${3:-n}"
  local display="(y/n)"
  [[ "$default" == "y" ]] && display="(${CYAN}Y${RESET}/n)" || display="(y/${CYAN}N${RESET})"
  echo -en "  ${question} ${display}: "
  read -r input
  input="${input:-$default}"
  CFG["$varname"]=$(echo "$input" | tr '[:upper:]' '[:lower:]')
}

# ── Validate helpers ──────────────────────────────────────────────────────────
validate_aws_region() {
  local region="$1"
  aws ec2 describe-regions --region-names "$region" --query 'Regions[0].RegionName' \
    --output text 2>/dev/null | grep -q "$region" \
    && return 0 || return 1
}

validate_cognito_pool() {
  local region="$1" pool_id="$2"
  aws cognito-idp describe-user-pool --user-pool-id "$pool_id" \
    --region "$region" --query 'UserPool.Id' --output text 2>/dev/null | grep -q "$pool_id"
}

validate_apigw() {
  local region="$1" apigw_id="$2"
  aws apigateway get-rest-api --rest-api-id "$apigw_id" \
    --region "$region" --query 'id' --output text 2>/dev/null | grep -q "$apigw_id"
}

validate_s3_bucket() {
  local bucket="$1"
  aws s3api head-bucket --bucket "$bucket" 2>/dev/null
}

# ── SECTION 1: AWS & Naming ───────────────────────────────────────────────────
section "1/8  AWS Region & Brand Naming"
echo "  These values identify the AWS account and namespace all resources."
echo

prompt "DATA_REGION" "AWS region for all resources (e.g. ap-south-1)" \
  "${CFG[DATA_REGION]:-ap-south-1}"

info "Validating region ${CFG[DATA_REGION]}..."
if validate_aws_region "${CFG[DATA_REGION]}"; then
  success "Region ${CFG[DATA_REGION]} is valid"
else
  warn "Could not validate region ${CFG[DATA_REGION]} — continuing anyway"
fi

prompt "BRAND_PREFIX" \
  "Brand prefix (lowercase, hyphens ok — used in resource names, e.g. digilux)" \
  "${CFG[BRAND_PREFIX]:-}"
[[ -z "${CFG[BRAND_PREFIX]}" ]] && die "BRAND_PREFIX is required"
BRAND_PREFIX="${CFG[BRAND_PREFIX]}"

# ── SECTION 2: API Gateway ────────────────────────────────────────────────────
section "2/8  API Gateway"
echo "  The existing REST API Gateway that hosts your app's routes."
echo

prompt "API_GATEWAY_ID" "API Gateway REST API ID (e.g. ds6nxf8ac5)" \
  "${CFG[API_GATEWAY_ID]:-}"
[[ -z "${CFG[API_GATEWAY_ID]}" ]] && die "API_GATEWAY_ID is required"

info "Validating API Gateway ${CFG[API_GATEWAY_ID]}..."
if validate_apigw "${CFG[DATA_REGION]}" "${CFG[API_GATEWAY_ID]}"; then
  success "API Gateway ${CFG[API_GATEWAY_ID]} found"
else
  warn "Could not validate API Gateway ID — check it is in region ${CFG[DATA_REGION]}"
fi

prompt "API_GATEWAY_STAGE" "API Gateway stage name (e.g. smarthome)" \
  "${CFG[API_GATEWAY_STAGE]:-smarthome}"

# Derive base URL
DERIVED_BASE_URL="https://$(aws apigateway get-rest-api \
  --rest-api-id "${CFG[API_GATEWAY_ID]}" \
  --region "${CFG[DATA_REGION]}" \
  --query 'id' --output text 2>/dev/null).execute-api.${CFG[DATA_REGION]}.amazonaws.com/${CFG[API_GATEWAY_STAGE]}" 2>/dev/null || true

prompt "OAUTH_BASE_URL" \
  "Public base URL for OAuth endpoints (e.g. https://iot.example.com/smarthome)" \
  "${CFG[OAUTH_BASE_URL]:-$DERIVED_BASE_URL}"
[[ -z "${CFG[OAUTH_BASE_URL]}" ]] && die "OAUTH_BASE_URL is required"

# ── SECTION 3: Cognito ────────────────────────────────────────────────────────
section "3/8  Cognito User Pool"
echo "  The Cognito User Pool that authenticates your app users."
echo

prompt "COGNITO_USER_POOL_ID" "Cognito User Pool ID (e.g. ap-south-1_h1o8s7257)" \
  "${CFG[COGNITO_USER_POOL_ID]:-}"
[[ -z "${CFG[COGNITO_USER_POOL_ID]}" ]] && die "COGNITO_USER_POOL_ID is required"

info "Validating Cognito pool ${CFG[COGNITO_USER_POOL_ID]}..."
if validate_cognito_pool "${CFG[DATA_REGION]}" "${CFG[COGNITO_USER_POOL_ID]}"; then
  success "Cognito pool found"
else
  warn "Could not validate Cognito pool — check the ID and region"
fi

echo
echo "  A new Cognito App Client will be created for Google Home OAuth."
echo "  Leave blank to auto-generate names."
prompt "GH_COGNITO_APP_CLIENT_NAME" \
  "Cognito App Client name for Google Home" \
  "${CFG[GH_COGNITO_APP_CLIENT_NAME]:-${BRAND_PREFIX}-google-home-oauth}"

# Hosted UI domain
POOL_DOMAIN=$(aws cognito-idp describe-user-pool \
  --user-pool-id "${CFG[COGNITO_USER_POOL_ID]}" \
  --region "${CFG[DATA_REGION]}" \
  --query 'UserPool.Domain' --output text 2>/dev/null || echo "")

if [[ -n "$POOL_DOMAIN" && "$POOL_DOMAIN" != "None" && "$POOL_DOMAIN" != "null" ]]; then
  DERIVED_HOSTED_UI="https://${POOL_DOMAIN}.auth.${CFG[DATA_REGION]}.amazoncognito.com"
  success "Detected Cognito domain: ${DERIVED_HOSTED_UI}"
  prompt "COGNITO_HOSTED_UI_BASE_URL" "Cognito Hosted UI base URL" \
    "${CFG[COGNITO_HOSTED_UI_BASE_URL]:-$DERIVED_HOSTED_UI}"
else
  warn "No Cognito domain found on pool. You need a custom domain or Cognito domain."
  prompt "COGNITO_HOSTED_UI_BASE_URL" \
    "Cognito Hosted UI base URL (e.g. https://auth.example.com)" \
    "${CFG[COGNITO_HOSTED_UI_BASE_URL]:-}"
fi
[[ -z "${CFG[COGNITO_HOSTED_UI_BASE_URL]}" ]] && die "COGNITO_HOSTED_UI_BASE_URL is required"

# ── SECTION 4: Google OAuth / Actions Console ─────────────────────────────────
section "4/8  Google Home Actions Console"
echo "  Values from your Google Home Actions Console project."
echo "  See: https://console.home.google.com"
echo

prompt "GOOGLE_AGENT_ID" \
  "Google Home Agent ID (from Actions Console → Project overview)" \
  "${CFG[GOOGLE_AGENT_ID]:-}"
[[ -z "${CFG[GOOGLE_AGENT_ID]}" ]] && die "GOOGLE_AGENT_ID is required"

prompt "GH_OAUTH_CLIENT_ID" \
  "OAuth Client ID configured in Actions Console account linking" \
  "${CFG[GH_OAUTH_CLIENT_ID]:-}"
[[ -z "${CFG[GH_OAUTH_CLIENT_ID]}" ]] && die "GH_OAUTH_CLIENT_ID is required"

echo
echo "  The OAuth client secret will be stored in AWS Secrets Manager."
echo "  It must match what you entered in Google Actions Console."
prompt_secret "GH_OAUTH_CLIENT_SECRET_VALUE" \
  "OAuth Client Secret (from Actions Console)" \
  "${CFG[GH_OAUTH_CLIENT_SECRET_VALUE]:-}"
[[ -z "${CFG[GH_OAUTH_CLIENT_SECRET_VALUE]}" ]] && die "GH_OAUTH_CLIENT_SECRET_VALUE is required"

prompt "GH_OAUTH_SECRET_NAME" \
  "Secrets Manager secret name for GH OAuth credentials" \
  "${CFG[GH_OAUTH_SECRET_NAME]:-${BRAND_PREFIX}-google-home-oauth}"

echo
echo "  Google Service Account key (for ReportStateAndNotification + deleteAgentUser)."
echo "  Leave blank to skip (unlink from GH app will not send real-time updates)."
prompt "GOOGLE_SA_KEY_SECRET_NAME" \
  "Secrets Manager secret name for Google Service Account JSON key" \
  "${CFG[GOOGLE_SA_KEY_SECRET_NAME]:-${BRAND_PREFIX}-google-home-sa-key}"

prompt_yn "HAS_SA_KEY_JSON" \
  "Do you have the Service Account JSON key to store now?" \
  "${CFG[HAS_SA_KEY_JSON]:-n}"

if [[ "${CFG[HAS_SA_KEY_JSON]}" == "y" ]]; then
  echo "  Paste the full Service Account JSON (single line), then press Enter:"
  echo -n "  > "
  read -r SA_KEY_JSON
  CFG[GOOGLE_SA_KEY_JSON_VALUE]="$SA_KEY_JSON"
else
  CFG[GOOGLE_SA_KEY_JSON_VALUE]="${CFG[GOOGLE_SA_KEY_JSON_VALUE]:-}"
  warn "SA key skipped — you can set it later in Secrets Manager and update the Lambda env var."
fi

# ── SECTION 5: Branding ───────────────────────────────────────────────────────
section "5/8  Branding (Consent Page)"
echo "  These values appear on the OAuth consent page shown during account linking."
echo

prompt "APP_NAME" \
  "App display name on consent page (e.g. Digilux)" \
  "${CFG[APP_NAME]:-${BRAND_PREFIX^}}"

prompt "PRIVACY_POLICY_URL" \
  "Privacy Policy URL shown on consent page" \
  "${CFG[PRIVACY_POLICY_URL]:-${CFG[OAUTH_BASE_URL]}/privacy}"

prompt "TERMS_URL" \
  "Terms & Conditions URL shown on consent page" \
  "${CFG[TERMS_URL]:-${CFG[OAUTH_BASE_URL]}/terms}"

echo
echo "  Brand logo (optional). Must be a PNG file bundled in the Lambda zip."
prompt "BRAND_LOGO_FILENAME" \
  "Logo PNG filename relative to lambda dir (leave blank for none)" \
  "${CFG[BRAND_LOGO_FILENAME]:-brand-logo.png}"

# ── SECTION 6: Lambda Runtime Settings ───────────────────────────────────────
section "6/8  Lambda Runtime Settings"
echo

prompt "SESSION_TTL_SECONDS" \
  "Deep link session TTL in seconds (default 1800 = 30 min)" \
  "${CFG[SESSION_TTL_SECONDS]:-1800}"

prompt "AUTH_CODE_TTL_SECONDS" \
  "OAuth auth code TTL in seconds (default 900 = 15 min)" \
  "${CFG[AUTH_CODE_TTL_SECONDS]:-900}"

prompt "ACCESS_TOKEN_TTL_SECONDS" \
  "OAuth access token TTL in seconds (default 3600 = 1 hour)" \
  "${CFG[ACCESS_TOKEN_TTL_SECONDS]:-3600}"

prompt "REFRESH_TOKEN_TTL_SECONDS" \
  "OAuth refresh token TTL in seconds (default 2592000 = 30 days)" \
  "${CFG[REFRESH_TOKEN_TTL_SECONDS]:-2592000}"

prompt "LAMBDA_TIMEOUT" \
  "Lambda timeout in seconds (default 30)" \
  "${CFG[LAMBDA_TIMEOUT]:-30}"

prompt "LAMBDA_MEMORY_MB" \
  "Lambda memory in MB (default 256)" \
  "${CFG[LAMBDA_MEMORY_MB]:-256}"

prompt "LAMBDA_RUNTIME" \
  "Lambda runtime (default python3.12)" \
  "${CFG[LAMBDA_RUNTIME]:-python3.12}"

# ── SECTION 7: DynamoDB & Existing Infrastructure ────────────────────────────
section "7/8  DynamoDB Table Names"
echo "  Table names for the 4 Google Home tables. Defaults derived from BRAND_PREFIX."
echo "  If these tables already exist they will be reused (idempotent)."
echo

BPFX="${BRAND_PREFIX//-/_}"  # hyphens → underscores for DDB names

prompt "GH_TOKENS_TABLE" \
  "Linked user tokens table" \
  "${CFG[GH_TOKENS_TABLE]:-${BPFX}_google_home_tokens}"

prompt "GH_SESSIONS_TABLE" \
  "Deep-link sessions (CSRF state) table" \
  "${CFG[GH_SESSIONS_TABLE]:-${BPFX}_google_home_link_sessions}"

prompt "GH_AUTH_CODES_TABLE" \
  "OAuth auth codes table" \
  "${CFG[GH_AUTH_CODES_TABLE]:-${BPFX}_google_home_auth_codes}"

prompt "GH_OAUTH_TRANSACTIONS_TABLE" \
  "Cognito OAuth transaction state table" \
  "${CFG[GH_OAUTH_TRANSACTIONS_TABLE]:-${BPFX}_google_home_oauth_transactions}"

echo
echo "  Lambda source bucket (for zipped Lambda packages)."
echo "  Leave blank to package and upload directly (no S3 needed)."
prompt "LAMBDA_S3_BUCKET" \
  "S3 bucket for Lambda packages (leave blank to use direct upload)" \
  "${CFG[LAMBDA_S3_BUCKET]:-}"

if [[ -n "${CFG[LAMBDA_S3_BUCKET]}" ]]; then
  info "Validating S3 bucket ${CFG[LAMBDA_S3_BUCKET]}..."
  if validate_s3_bucket "${CFG[LAMBDA_S3_BUCKET]}"; then
    success "S3 bucket found"
  else
    warn "Could not validate S3 bucket — it may need to be created first"
  fi
fi

# ── SECTION 8: IoT / Device Data Tables ──────────────────────────────────────
section "8/8  Device Data (Fulfillment)"
echo "  Table / S3 / IoT endpoint names used by the fulfillment Lambda."
echo "  These are read-only by Google Home; they must already exist."
echo

prompt "USER_DEVICE_TABLE" \
  "DynamoDB table for user→device mapping" \
  "${CFG[USER_DEVICE_TABLE]:-user_device_details}"

prompt "DEVICE_STATE_TABLE" \
  "DynamoDB table for live device state" \
  "${CFG[DEVICE_STATE_TABLE]:-device_state}"

prompt "SCENE_TABLE" \
  "DynamoDB table for scenes (leave blank if no scenes)" \
  "${CFG[SCENE_TABLE]:-digilux_scene_data}"

prompt "METADATA_S3_BUCKET" \
  "S3 bucket containing device metadata / trait mappings" \
  "${CFG[METADATA_S3_BUCKET]:-digilux-metadata-bucket}"

prompt "IOT_ENDPOINT_SECRET_NAME" \
  "Secrets Manager secret name that holds the IoT endpoint key" \
  "${CFG[IOT_ENDPOINT_SECRET_NAME]:-digilux-iot-endpoint-key}"

IOT_ENDPOINT_DETECTED=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
  --region "${CFG[DATA_REGION]}" --query 'endpointAddress' --output text 2>/dev/null || echo "")
if [[ -n "$IOT_ENDPOINT_DETECTED" ]]; then
  success "Detected IoT endpoint: $IOT_ENDPOINT_DETECTED"
fi
prompt "IOT_ENDPOINT" \
  "AWS IoT Core endpoint (xxxxxxxxxx.iot.<region>.amazonaws.com)" \
  "${CFG[IOT_ENDPOINT]:-$IOT_ENDPOINT_DETECTED}"

# ── Derive Cognito callback URL ───────────────────────────────────────────────
CFG[COGNITO_REDIRECT_URI]="${CFG[OAUTH_BASE_URL]}/google-home/oauth/cognito/callback"

# ── Summary ───────────────────────────────────────────────────────────────────
section "Configuration Summary"
echo
printf "  %-40s %s\n" "DATA_REGION:"              "${CFG[DATA_REGION]}"
printf "  %-40s %s\n" "BRAND_PREFIX:"             "${CFG[BRAND_PREFIX]}"
printf "  %-40s %s\n" "API_GATEWAY_ID:"           "${CFG[API_GATEWAY_ID]}"
printf "  %-40s %s\n" "API_GATEWAY_STAGE:"        "${CFG[API_GATEWAY_STAGE]}"
printf "  %-40s %s\n" "OAUTH_BASE_URL:"           "${CFG[OAUTH_BASE_URL]}"
printf "  %-40s %s\n" "COGNITO_USER_POOL_ID:"     "${CFG[COGNITO_USER_POOL_ID]}"
printf "  %-40s %s\n" "COGNITO_HOSTED_UI_BASE_URL:" "${CFG[COGNITO_HOSTED_UI_BASE_URL]}"
printf "  %-40s %s\n" "COGNITO_REDIRECT_URI:"     "${CFG[COGNITO_REDIRECT_URI]}"
printf "  %-40s %s\n" "GOOGLE_AGENT_ID:"          "${CFG[GOOGLE_AGENT_ID]}"
printf "  %-40s %s\n" "GH_OAUTH_CLIENT_ID:"       "${CFG[GH_OAUTH_CLIENT_ID]}"
printf "  %-40s %s\n" "GH_OAUTH_SECRET_NAME:"     "${CFG[GH_OAUTH_SECRET_NAME]}"
printf "  %-40s %s\n" "APP_NAME:"                 "${CFG[APP_NAME]}"
printf "  %-40s %s\n" "GH_TOKENS_TABLE:"          "${CFG[GH_TOKENS_TABLE]}"
printf "  %-40s %s\n" "GH_SESSIONS_TABLE:"        "${CFG[GH_SESSIONS_TABLE]}"
printf "  %-40s %s\n" "GH_AUTH_CODES_TABLE:"      "${CFG[GH_AUTH_CODES_TABLE]}"
printf "  %-40s %s\n" "GH_OAUTH_TRANSACTIONS_TABLE:" "${CFG[GH_OAUTH_TRANSACTIONS_TABLE]}"
echo

# ── Write output JSON ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_FILE="${SCRIPT_DIR}/env.${BRAND_PREFIX}.json"

echo -en "  ${BOLD}Save to ${OUTPUT_FILE}?${RESET} (${CYAN}Y${RESET}/n): "
read -r confirm
confirm="${confirm:-y}"
if [[ "${confirm,,}" != "y" ]]; then
  echo "  Enter output path: "
  read -r OUTPUT_FILE
fi

# Build JSON (strip SA key JSON from stored config — store separately)
python3 - <<PYEOF
import json, sys

cfg = {
  "DATA_REGION":                    "${CFG[DATA_REGION]}",
  "BRAND_PREFIX":                   "${CFG[BRAND_PREFIX]}",
  "API_GATEWAY_ID":                 "${CFG[API_GATEWAY_ID]}",
  "API_GATEWAY_STAGE":              "${CFG[API_GATEWAY_STAGE]}",
  "OAUTH_BASE_URL":                 "${CFG[OAUTH_BASE_URL]}",
  "COGNITO_USER_POOL_ID":           "${CFG[COGNITO_USER_POOL_ID]}",
  "COGNITO_HOSTED_UI_BASE_URL":     "${CFG[COGNITO_HOSTED_UI_BASE_URL]}",
  "COGNITO_REDIRECT_URI":           "${CFG[COGNITO_REDIRECT_URI]}",
  "GH_COGNITO_APP_CLIENT_NAME":     "${CFG[GH_COGNITO_APP_CLIENT_NAME]}",
  "GOOGLE_AGENT_ID":                "${CFG[GOOGLE_AGENT_ID]}",
  "GH_OAUTH_CLIENT_ID":             "${CFG[GH_OAUTH_CLIENT_ID]}",
  "GH_OAUTH_SECRET_NAME":           "${CFG[GH_OAUTH_SECRET_NAME]}",
  "GOOGLE_SA_KEY_SECRET_NAME":      "${CFG[GOOGLE_SA_KEY_SECRET_NAME]}",
  "APP_NAME":                       "${CFG[APP_NAME]}",
  "PRIVACY_POLICY_URL":             "${CFG[PRIVACY_POLICY_URL]}",
  "TERMS_URL":                      "${CFG[TERMS_URL]}",
  "BRAND_LOGO_FILENAME":            "${CFG[BRAND_LOGO_FILENAME]}",
  "SESSION_TTL_SECONDS":            "${CFG[SESSION_TTL_SECONDS]}",
  "AUTH_CODE_TTL_SECONDS":          "${CFG[AUTH_CODE_TTL_SECONDS]}",
  "ACCESS_TOKEN_TTL_SECONDS":       "${CFG[ACCESS_TOKEN_TTL_SECONDS]}",
  "REFRESH_TOKEN_TTL_SECONDS":      "${CFG[REFRESH_TOKEN_TTL_SECONDS]}",
  "LAMBDA_TIMEOUT":                 "${CFG[LAMBDA_TIMEOUT]}",
  "LAMBDA_MEMORY_MB":               "${CFG[LAMBDA_MEMORY_MB]}",
  "LAMBDA_RUNTIME":                 "${CFG[LAMBDA_RUNTIME]}",
  "GH_TOKENS_TABLE":                "${CFG[GH_TOKENS_TABLE]}",
  "GH_SESSIONS_TABLE":              "${CFG[GH_SESSIONS_TABLE]}",
  "GH_AUTH_CODES_TABLE":            "${CFG[GH_AUTH_CODES_TABLE]}",
  "GH_OAUTH_TRANSACTIONS_TABLE":    "${CFG[GH_OAUTH_TRANSACTIONS_TABLE]}",
  "LAMBDA_S3_BUCKET":               "${CFG[LAMBDA_S3_BUCKET]:-}",
  "USER_DEVICE_TABLE":              "${CFG[USER_DEVICE_TABLE]}",
  "DEVICE_STATE_TABLE":             "${CFG[DEVICE_STATE_TABLE]}",
  "SCENE_TABLE":                    "${CFG[SCENE_TABLE]}",
  "METADATA_S3_BUCKET":             "${CFG[METADATA_S3_BUCKET]}",
  "IOT_ENDPOINT_SECRET_NAME":       "${CFG[IOT_ENDPOINT_SECRET_NAME]}",
  "IOT_ENDPOINT":                   "${CFG[IOT_ENDPOINT]}",
  "AWS_ACCOUNT_ID":                 "${AWS_ACCOUNT_ID}"
}

# Remove empty string values that are truly optional
optional_empty = {"LAMBDA_S3_BUCKET", "GOOGLE_SA_KEY_SECRET_NAME", "IOT_ENDPOINT"}
out = {k: v for k, v in cfg.items() if v or k in optional_empty}

with open("${OUTPUT_FILE}", "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")

print(f"  Written {len(out)} keys to ${OUTPUT_FILE}")
PYEOF

chmod 600 "$OUTPUT_FILE"
success "Config saved to $OUTPUT_FILE (chmod 600)"

# Store SA key in Secrets Manager if provided
if [[ -n "${CFG[GOOGLE_SA_KEY_JSON_VALUE]:-}" ]]; then
  SECRET_NAME="${CFG[GOOGLE_SA_KEY_SECRET_NAME]}"
  info "Storing Service Account JSON in Secrets Manager as '${SECRET_NAME}'..."
  EXISTING=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
    --region "${CFG[DATA_REGION]}" --query 'ARN' --output text 2>/dev/null || echo "")
  if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
    aws secretsmanager put-secret-value \
      --secret-id "$SECRET_NAME" \
      --secret-string "${CFG[GOOGLE_SA_KEY_JSON_VALUE]}" \
      --region "${CFG[DATA_REGION]}" >/dev/null
    success "Updated existing secret: $EXISTING"
    # Store ARN back into config
    SA_ARN="$EXISTING"
  else
    SA_ARN=$(aws secretsmanager create-secret \
      --name "$SECRET_NAME" \
      --description "Google Home Service Account key for ${BRAND_PREFIX}" \
      --secret-string "${CFG[GOOGLE_SA_KEY_JSON_VALUE]}" \
      --region "${CFG[DATA_REGION]}" \
      --query 'ARN' --output text)
    success "Created secret: $SA_ARN"
  fi
  # Update JSON with ARN
  jq --arg arn "$SA_ARN" '. + {"GOOGLE_SA_KEY_SECRET_ARN": $arn}' "$OUTPUT_FILE" \
    > "${OUTPUT_FILE}.tmp" && mv "${OUTPUT_FILE}.tmp" "$OUTPUT_FILE"
  chmod 600 "$OUTPUT_FILE"
fi

echo
echo -e "${BOLD}${GREEN}Pre-deploy wizard complete!${RESET}"
echo
echo "  Next step:"
echo -e "    ${CYAN}./deploy/deploy-google-home.sh --config ${OUTPUT_FILE}${RESET}"
echo
echo "  Before running deploy, also ensure:"
echo "    1. Google Actions Console is configured with the OAuth and fulfillment URLs"
echo "       Auth URL:        ${CFG[OAUTH_BASE_URL]}/google-home/oauth/authorize"
echo "       Token URL:       ${CFG[OAUTH_BASE_URL]}/google-home/oauth/token"
echo "       Fulfillment URL: ${CFG[OAUTH_BASE_URL]}/google-home/fulfillment"
echo "    2. A brand logo PNG is present at:"
echo "       lambdas/google_home_oauth_cognito_callback/${CFG[BRAND_LOGO_FILENAME]}"
echo "    3. The Cognito User Pool has a domain (Hosted UI) configured"
echo
