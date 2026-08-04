#!/bin/bash
# alexa_live_test.sh — Digilux Alexa App-to-App live E2E tests
#
# TOKEN REQUIREMENTS
# ──────────────────
# ALL authenticated endpoints require a Flutter OAuth access token
# (smarthome_server/read+write scopes from Cognito Hosted UI OAuth flow).
# CLI USER_PASSWORD_AUTH tokens do NOT carry these scopes → rejected with 401.
#
# This script detects the token type automatically:
#   • Flutter OAuth access token  → all 25 checks run (full test)
#   • CLI ID/access token         → sections 2, 4, 5, 6, 7, 8 SKIPPED;
#                                    sections 1, 3, 9 still run (no auth / public)
#
# To run the full test suite, obtain a Flutter OAuth access token from the
# Cognito Hosted UI (smarthome_server scopes) and set it as ACCESS_TOKEN below.

ACCESS_TOKEN="<REPLACE_WITH_TOKEN>"

BASE="https://iot.digilux.co.in/smarthome"
PASS=0; FAIL=0; SKIP=0

check() {
  local desc="$1" expected="$2" actual="$3"
  if echo "$actual" | grep -qE "$expected"; then
    echo "  PASS  $desc"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $desc"
    echo "         expected: $expected"
    echo "         got:      $actual"
    FAIL=$((FAIL+1))
  fi
}

skip() {
  echo "  SKIP  $1"
  echo "         $2"
  SKIP=$((SKIP+1))
}

# ── Detect token type ─────────────────────────────────────────────────────────
TOKEN_SCOPE=$(echo "$ACCESS_TOKEN" | cut -d'.' -f2 | python3 -c "
import sys, base64, json
b = sys.stdin.read().strip()
b += '=' * (-len(b) % 4)
try:
    d = json.loads(base64.urlsafe_b64decode(b))
    print(d.get('scope', d.get('token_use', 'unknown')))
except Exception:
    print('unknown')
" 2>/dev/null)

HAS_SMARTHOME_SCOPE=false
echo "$TOKEN_SCOPE" | grep -q "smarthome_server" && HAS_SMARTHOME_SCOPE=true

echo "=================================================="
echo " Digilux Alexa App-to-App — AWS Live Tests"
echo " Date: $(date '+%Y-%m-%d %H:%M %Z')"
echo " Base: $BASE"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  echo " Auth: Flutter OAuth access token (smarthome_server scopes) — FULL TEST"
else
  echo " Auth: CLI token (no smarthome_server scopes) — authenticated sections SKIPPED"
  echo "        All Alexa endpoints require a Flutter OAuth access token."
fi
echo "=================================================="

echo ""
echo "── 1. Auth Guard (all 5 endpoints) ─────────────"
R=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 "$BASE/api/v1/alexa/status")
check "status — no auth → 401" "401" "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 -H "Authorization: Bearer badtoken" "$BASE/api/v1/alexa/status")
check "status — bad token → 401" "401" "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 -X POST "$BASE/api/v1/alexa/startAppToApp")
check "startAppToApp — no auth → 401" "401" "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 -X POST "$BASE/api/v1/alexa/completeAppToApp")
check "completeAppToApp — no auth → 401" "401" "$R"

R=$(curl -s -o /dev/null -w "%{http_code}" --http1.1 -X DELETE "$BASE/api/v1/alexa/unlink")
check "unlink — no auth → 401" "401" "$R"

echo ""
echo "── 2. Status — Before Linking ───────────────────"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" "$BASE/api/v1/alexa/status")
  check "GET /status → 200 with linked field" "linked" "$R"
  check "GET /status — linked=false" '"linked": false' "$R"
else
  skip "GET /status → 200 with linked field" "Requires Flutter OAuth access token (smarthome_server/read scope)"
  skip "GET /status — linked=false"          "Requires Flutter OAuth access token (smarthome_server/read scope)"
fi

echo ""
echo "── 3. Callback — Public Endpoint ────────────────"
R=$(curl -s --http1.1 "$BASE/alexa/callback?error=access_denied")
check "callback — error param → HTML error page" "Linking Failed" "$R"

R=$(curl -s --http1.1 "$BASE/alexa/callback?code=TESTCODE123&state=somestate")
check "callback — code+state → HTML success page" "digilux://" "$R"
check "callback success — deep link has code" "TESTCODE123" "$R"
check "callback success — Open Digilux App button" "Open Digilux App" "$R"

echo ""
echo "── 4. startAppToApp ─────────────────────────────"
echo "   NOTE: requires Flutter OAuth access token (smarthome_server/read+write scope)"

STATE=""
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" -X POST "$BASE/api/v1/alexa/startAppToApp")
  check "startAppToApp → state field" '"state"' "$R"
  check "returns codeChallenge" '"codeChallenge"' "$R"
  check "returns redirectUri" '"redirectUri"' "$R"

  STATE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('state',''))" 2>/dev/null)
  CODE_CHALLENGE=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('codeChallenge',''))" 2>/dev/null)
  REDIRECT_URI=$(echo "$R" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('redirectUri',''))" 2>/dev/null)

  [ ${#STATE} -eq 36 ] \
    && check "state is UUID (36 chars)" "." "ok_match" \
    || check "state is UUID (36 chars)" "36chars" "got_${#STATE}_chars"

  [ ${#CODE_CHALLENGE} -eq 43 ] \
    && check "codeChallenge is 43 chars (PKCE S256)" "." "ok_match" \
    || check "codeChallenge is 43 chars (PKCE S256)" "43chars" "got_${#CODE_CHALLENGE}_chars"

  check "redirectUri = iot.digilux.co.in/alexa/callback" "iot.digilux.co.in/alexa/callback" "$REDIRECT_URI"
else
  skip "startAppToApp → state field"                        "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "returns codeChallenge"                              "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "returns redirectUri"                                "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "state is UUID (36 chars)"                           "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "codeChallenge is 43 chars (PKCE S256)"              "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "redirectUri = iot.digilux.co.in/alexa/callback"    "Requires Flutter OAuth access token (smarthome_server scopes)"
fi

echo ""
echo "── 5. completeAppToApp — Input Validation ───────"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
    -X POST "$BASE/api/v1/alexa/completeAppToApp" -d '{}')
  check "empty body → 'code is required'" "code is required" "$R"

  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
    -X POST "$BASE/api/v1/alexa/completeAppToApp" -d '{"code":"X","state":"not-a-uuid"}')
  check "malformed state → 'Invalid state format'" "Invalid state format" "$R"

  FAKE_STATE="${STATE:-$(python3 -c 'import uuid; print(str(uuid.uuid4()))')}"
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" -H "Content-Type: application/json" \
    -X POST "$BASE/api/v1/alexa/completeAppToApp" \
    -d "{\"code\":\"FAKECODE\",\"state\":\"$FAKE_STATE\"}")
  check "valid UUID state → Lambda processed (LWA rejection expected)" "error|linked" "$R"
else
  skip "empty body → 'code is required'"                              "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "malformed state → 'Invalid state format'"                     "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "valid UUID state → Lambda processed (LWA rejection expected)" "Requires Flutter OAuth access token (smarthome_server scopes)"
fi

echo ""
echo "── 6. Unlink ────────────────────────────────────"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" -X DELETE "$BASE/api/v1/alexa/unlink")
  check "unlink → 404 No linked account" "No linked Alexa account" "$R"
else
  skip "unlink → 404 No linked account" "Requires Flutter OAuth access token (smarthome_server scopes)"
fi

echo ""
echo "── 7. Status — After Tests ──────────────────────"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  R=$(curl -s --http1.1 -H "Authorization: Bearer $ACCESS_TOKEN" "$BASE/api/v1/alexa/status")
  check "GET /status — linked=false (confirmed)" '"linked": false' "$R"
else
  skip "GET /status — linked=false (confirmed)" "Requires Flutter OAuth access token (smarthome_server scopes)"
fi

echo ""
echo "── 8. DynamoDB Consistency ──────────────────────"
if [ "$HAS_SMARTHOME_SCOPE" = "true" ]; then
  SESSION=$(aws dynamodb get-item \
    --table-name alexa_app_linking_sessions \
    --key "{\"state\":{\"S\":\"$STATE\"}}" \
    --region ap-south-1 \
    --query 'Item.status.S' --output text 2>/dev/null)
  check "session in DDB — status=USED" "USED" "$SESSION"

  TOKEN_RECORD=$(aws dynamodb get-item \
    --table-name digilux_honeywell_alexa_lwa_tokens \
    --key "{\"userId\":{\"S\":\"f123dd2a-7061-7004-d4f2-573c0585ad6b\"}}" \
    --region ap-south-1 \
    --output text 2>/dev/null)
  [ -z "$TOKEN_RECORD" ] \
    && check "tokens table — no record (user unlinked)" "." "ok_match" \
    || check "tokens table — no record (user unlinked)" "empty" "has_record"
else
  skip "session in DDB — status=USED"          "Requires Flutter OAuth access token (smarthome_server scopes)"
  skip "tokens table — no record (user unlinked)" "Requires Flutter OAuth access token (smarthome_server scopes)"
fi

echo ""
echo "── 9. assetlinks.json ───────────────────────────"
R=$(curl -s --http1.1 "https://iot.digilux.co.in/.well-known/assetlinks.json")
check "assetlinks.json — live at iot.digilux.co.in/.well-known/assetlinks.json" "sha256_cert_fingerprints" "$R"
check "assetlinks.json — correct prod SHA256 fingerprint" "B2:F9:FD" "$R"

echo ""
echo "=================================================="
echo " Results: $PASS passed, $FAIL failed, $SKIP skipped"
if [ $FAIL -eq 0 ]; then
  if [ $SKIP -gt 0 ]; then
    echo " PASSED (with $SKIP skipped — run with Flutter OAuth token for full coverage)"
  else
    echo " ALL TESTS PASSED"
  fi
else
  echo " SOME TESTS FAILED — see details above"
fi
echo "=================================================="
