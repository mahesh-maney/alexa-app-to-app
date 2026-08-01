#!/bin/bash
# test.sh — Integration tests for Alexa App-to-App account linking
#
# Usage:
#   export COGNITO_TOKEN="<valid Cognito JWT for a test user>"
#   chmod +x test.sh && ./test.sh

set -euo pipefail

REGION="ap-south-1"
API_ID="5sros9vjc2"
BASE="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod"
PASS=0
FAIL=0

: "${COGNITO_TOKEN:?Set COGNITO_TOKEN env var to a valid Cognito JWT before running}"

header() { echo ""; echo "==> $1"; }
pass()   { echo "    PASS: $1"; ((PASS++)); }
fail()   { echo "    FAIL: $1"; ((FAIL++)); }

check_field() {
  local BODY="$1" FIELD="$2" EXPECTED="$3"
  local ACTUAL
  ACTUAL=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$FIELD','MISSING'))" 2>/dev/null)
  if [ "$ACTUAL" = "$EXPECTED" ]; then
    pass "$FIELD = $EXPECTED"
  else
    fail "$FIELD expected '$EXPECTED' got '$ACTUAL'"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
header "T01: startAppToApp — success"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/startAppToApp" \
  -H "Authorization: Bearer $COGNITO_TOKEN" \
  -H "Content-Type: application/json")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)

if [ "$CODE" = "200" ]; then
  pass "HTTP 200"
else
  fail "HTTP $CODE (expected 200)"
fi

STATE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null)
CHALLENGE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('codeChallenge',''))" 2>/dev/null)
REDIRECT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('redirectUri',''))" 2>/dev/null)

[ -n "$STATE" ]     && pass "state present (${STATE:0:8}...)" || fail "state missing"
[ -n "$CHALLENGE" ] && pass "codeChallenge present"           || fail "codeChallenge missing"
check_field "$BODY" "redirectUri" "https://www.digilux.co.in/alexa/callback"

echo "    state: $STATE"
echo "    codeChallenge: ${CHALLENGE:0:12}..."

# ─────────────────────────────────────────────────────────────────────────────
header "T02: startAppToApp — no auth token"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/startAppToApp" \
  -H "Content-Type: application/json")
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "401" ] && pass "HTTP 401 (Cognito authorizer blocked)" \
                     || fail "HTTP $CODE (expected 401)"

# ─────────────────────────────────────────────────────────────────────────────
header "T03: startAppToApp — malformed auth header"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/startAppToApp" \
  -H "Authorization: Bearer not.a.valid.jwt.at.all" \
  -H "Content-Type: application/json")
CODE=$(echo "$RESP" | tail -1)
# Cognito authorizer will reject this at the API GW layer
[ "$CODE" = "401" ] && pass "HTTP 401" || fail "HTTP $CODE (expected 401)"

# ─────────────────────────────────────────────────────────────────────────────
header "T04: completeAppToApp — missing code"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/completeAppToApp" \
  -H "Authorization: Bearer $COGNITO_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"state\":\"$STATE\"}")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "400" ] && pass "HTTP 400" || fail "HTTP $CODE (expected 400)"
echo "$BODY" | python3 -c "import sys,json; e=json.load(sys.stdin).get('error',''); print(f'    error: {e}')" 2>/dev/null

# ─────────────────────────────────────────────────────────────────────────────
header "T05: completeAppToApp — invalid state"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/completeAppToApp" \
  -H "Authorization: Bearer $COGNITO_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"code":"fake-code","state":"00000000-0000-0000-0000-000000000000"}')
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "400" ] && pass "HTTP 400 (invalid state)" || fail "HTTP $CODE (expected 400)"

# ─────────────────────────────────────────────────────────────────────────────
header "T06: completeAppToApp — valid state but fake code (Amazon will reject)"
# This tests the full flow up to the Amazon token exchange step.
# With a real state + fake code, Amazon returns 400 and we return 502.
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/completeAppToApp" \
  -H "Authorization: Bearer $COGNITO_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"FAKE_AUTH_CODE_FOR_TEST\",\"state\":\"$STATE\"}")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "502" ] && pass "HTTP 502 (Amazon rejected fake code — as expected)" \
                     || fail "HTTP $CODE (expected 502)"

# ─────────────────────────────────────────────────────────────────────────────
header "T07: completeAppToApp — state replay (same state used again)"
# State was just consumed in T06 (marked USED). A second attempt must fail.
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/v1/alexa/completeAppToApp" \
  -H "Authorization: Bearer $COGNITO_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"ANOTHER_FAKE_CODE\",\"state\":\"$STATE\"}")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "400" ] && pass "HTTP 400 (replay blocked)" || fail "HTTP $CODE (expected 400)"

# ─────────────────────────────────────────────────────────────────────────────
header "T08: callback — success redirect page"
RESP=$(curl -s -w "\n%{http_code}" \
  "$BASE/alexa/callback?code=TEST_CODE&state=TEST_STATE")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "200" ] && pass "HTTP 200" || fail "HTTP $CODE (expected 200)"
echo "$BODY" | grep -q "digilux://alexa/callback" && pass "Deep link present" || fail "Deep link missing"
echo "$BODY" | grep -q "Alexa Connected" && pass "Success page rendered" || fail "Success page missing"

# ─────────────────────────────────────────────────────────────────────────────
header "T09: callback — error page (user denied)"
RESP=$(curl -s -w "\n%{http_code}" \
  "$BASE/alexa/callback?error=access_denied&error_description=User+denied")
BODY=$(echo "$RESP" | head -1)
CODE=$(echo "$RESP" | tail -1)
[ "$CODE" = "200" ] && pass "HTTP 200" || fail "HTTP $CODE (expected 200)"
echo "$BODY" | grep -q "Linking Failed" && pass "Error page rendered" || fail "Error page missing"

# ─────────────────────────────────────────────────────────────────────────────
header "T10: DynamoDB — verify session was marked USED"
SESSION_STATUS=$(aws dynamodb get-item \
  --table-name alexa_app_linking_sessions \
  --region "$REGION" \
  --key "{\"state\":{\"S\":\"$STATE\"}}" \
  --query 'Item.status.S' --output text 2>/dev/null)
[ "$SESSION_STATUS" = "USED" ] && pass "Session status = USED in DynamoDB" \
                                 || fail "Session status = $SESSION_STATUS (expected USED)"

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "=================================================="
echo " Results: $PASS passed, $FAIL failed"
echo "=================================================="
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
