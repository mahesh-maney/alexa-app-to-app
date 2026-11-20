# Honeywell Nucleus — Alexa Account Linking
# PSAT API Inventory & Security Reference

**Prepared for:** Honeywell Cybersecurity / PSAT Team
**Environment:** Honeywell QA (nucleusqa) — `https://api.nucleusqa.honeywell.com`
**Date:** 2026-11-19
**Classification:** Internal / Restricted

---

## 1. Account Linking Flows

### Flow A — App-Initiated Linking (Nucleus App → Alexa)

```
1. Nucleus App  →  POST /api/alexa/app-to-app/start
   Response: { lwaUrl, state, codeChallenge }

2. App opens Amazon LWA OAuth URL in browser/Alexa app
   (User authenticates with Amazon credentials)

3. Amazon redirects to:  GET /api/alexa/callback?code=<lwa_code>&state=<state>
   Server-side: exchanges LWA code, stores token, redirects app via deep link

4. Nucleus App receives deep link callback
   App  →  POST /api/alexa/app-to-app/complete
   Response: { cognitoAuthUrl }

5. App opens Cognito OAuth URL in browser
   (User authenticates with Nucleus/Honeywell credentials)

6. Cognito redirects to:  GET /api/alexa/cognito/callback?code=<cognito_code>&state=<state>
   Server-side: exchanges Cognito code, writes linked status, redirects app via deep link

7. App polls:  GET /api/alexa/link-status?siteId=<id>
   Response: { linked: true }
```

### Flow B — Alexa-App-Initiated Linking (Alexa App → Account Linking)

```
1. User taps "Enable" in Alexa app
   Alexa redirects to: GET /api/alexa/cognito/callback (via Alexa Developer Console OAuth config)

2. Server redirects user to Cognito Hosted UI for Nucleus login

3. After login, Cognito redirects back to server callback
   Server writes linked status and redirects to Alexa redirect URI

4. Alexa sends SkillAccountLinked event to skill event webhook (internal)
   (Alexa app now shows skill as linked)

5. Next time Nucleus app checks:  GET /api/alexa/link-status?siteId=<id>
   Response: { linked: true }  (backfilled from Alexa event)
```

### Flow C — App-Initiated Unlink

```
1. Nucleus App  →  DELETE /api/alexa/account-linking?siteId=<id>
   Server: revokes LWA token, clears linked flag for siteId
   Response: 200 OK
```

### Flow D — Alexa-App-Initiated Unlink (User disables skill in Alexa app)

```
1. Alexa sends SkillDisabled event to skill event webhook (internal)
   Server: looks up Cognito user linked to that Amazon account, clears linked flag

2. Next time Nucleus app checks:  GET /api/alexa/link-status?siteId=<id>
   Response: { linked: false }
```

---

## 2. API Endpoint Inventory

### Group 1 — Alexa App-to-App Linking (Nucleus App calls these)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 1 | POST | `https://api.nucleusqa.honeywell.com/api/alexa/app-to-app/start` | Cognito JWT (Bearer) | Initiates linking; returns LWA OAuth URL + PKCE parameters |
| 2 | POST | `https://api.nucleusqa.honeywell.com/api/alexa/app-to-app/complete` | Cognito JWT (Bearer) | Completes first leg after LWA OAuth; returns Cognito OAuth URL |
| 3 | DELETE | `https://api.nucleusqa.honeywell.com/api/alexa/account-linking?siteId={siteId}` | Cognito JWT (Bearer) | Unlinks Alexa from the specified site |
| 4 | GET | `https://api.nucleusqa.honeywell.com/api/alexa/link-status?siteId={siteId}` | Cognito JWT (Bearer) | Returns current Alexa linking status for a site |

### Group 2 — Alexa OAuth Redirect Endpoints (Browser/Alexa app redirect targets)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 5 | GET | `https://api.nucleusqa.honeywell.com/api/alexa/callback` | None (public redirect) | Receives LWA auth code from Amazon; exchanges and redirects app via deep link |
| 6 | GET | `https://api.nucleusqa.honeywell.com/api/alexa/cognito/callback` | None (public redirect) | Receives Cognito auth code; completes OAuth and redirects app via deep link |

### Group 3 — Alexa Skill Event Webhook (Amazon posts to this)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 7 | POST | `https://api.nucleusqa.honeywell.com/api/alexa/skill-event` | Amazon RSA-SHA1 Signature (see §5) | Receives SkillAccountLinked and SkillDisabled events from Amazon |

### Group 4 — Smart Home Fulfillment (Alexa invokes this for voice commands)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 8 | POST | AWS Lambda (eu-west-1) — invoked directly by Alexa, not via API GW | LWA access token (validated internally) | Handles all Alexa smart home directives: Discover, TurnOn, TurnOff, SetBrightness, etc. |

### Group 5 — Alexa Token Endpoint (Amazon calls this)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 9 | POST | `https://auth.nucleusqa.honeywell.com/oauth2/token` | Cognito hosted OAuth (client_id + code) | Standard OAuth2 token exchange; managed by AWS Cognito |

> **Note on Group 4:** The smart home fulfillment Lambda is not publicly accessible via HTTPS. It is invoked directly by Amazon Alexa infrastructure using the Lambda ARN registered in the Alexa Developer Console. It is not reachable from the internet.

---

## 3. Authentication Summary

| Endpoint Group | Mechanism | Token Source | Verification |
|----------------|-----------|-------------|-------------|
| Group 1 (app API calls) | Bearer JWT | AWS Cognito (Nucleus login) | API Gateway validates JWT signature against Cognito JWKS |
| Group 2 (OAuth redirects) | None — public redirects | PKCE state parameter (opaque, server-side bound) | Server verifies `state` matches stored session; PKCE `code_verifier` checked on exchange |
| Group 3 (skill events) | Amazon RSA-SHA1 request signature | Amazon's signing certificate | Server verifies cert chain (Amazon CA), SAN, timestamp (±150 s), body SHA-256 hash, RSA-SHA1 signature |
| Group 4 (fulfillment) | LWA access token in Alexa directive | Amazon Login with Alexa (LWA) | Lambda validates token against Amazon token introspection |
| Group 5 (token endpoint) | OAuth2 client credentials + auth code | AWS Cognito | Cognito-managed; standard OAuth2 |

---

## 4. Error Response Schema

All Group 1–3 endpoints return errors in the following shape:

```json
{ "error": "<error_code>", "message": "<human_readable>" }
```

HTTP status codes used:

| Status | Meaning |
|--------|---------|
| 400 | Bad request — missing or malformed parameters |
| 401 | Unauthorized — missing, expired, or invalid JWT/token |
| 403 | Forbidden — valid token but insufficient permissions |
| 404 | Not found — resource or session does not exist |
| 409 | Conflict — state already exists (e.g. duplicate session) |
| 410 | Gone — session expired |
| 500 | Internal server error |

Representative error codes returned (not exhaustive):

```
MISSING_SITE_ID         — siteId query param absent
INVALID_STATE           — state parameter does not match stored session
SESSION_EXPIRED         — PKCE or linking session TTL exceeded
SESSION_NOT_FOUND       — no matching session for the given state
LWA_EXCHANGE_FAILED     — Amazon token exchange returned error
LWA_REVOKE_FAILED       — Amazon token revocation call failed
COGNITO_EXCHANGE_FAILED — Cognito token exchange returned error
INVALID_SIGNATURE       — RSA-SHA1 signature on skill event failed verification
INVALID_TIMESTAMP       — Skill event timestamp outside ±150 s window
CERT_FETCH_FAILED       — Could not fetch Amazon signing certificate
NOT_LINKED              — Alexa is not currently linked for this siteId
```

---

## 5. Endpoint #7 — Skill Event Webhook Testability

**URL:** `https://api.nucleusqa.honeywell.com/api/alexa/skill-event`
**Method:** POST
**Port:** 443 (HTTPS)

### Request Signature Requirements

Amazon signs all skill event requests. The server **rejects** any request that does not carry a valid Amazon signature. Test tools **must** replicate Amazon's signing process:

| Header | Description |
|--------|-------------|
| `SignatureCertChainUrl` | URL of Amazon's public certificate (must be `https://s3.amazonaws.com/echo.api/...`) |
| `Signature` | Base64-encoded RSA-SHA1 signature of the raw request body |

Verification steps the server performs:
1. Fetches the certificate from `SignatureCertChainUrl`
2. Verifies the certificate is issued by Amazon's CA and contains the required SAN (`echo-api.amazon.com`)
3. Verifies the request timestamp is within ±150 seconds of server time
4. Computes SHA-256 of the raw request body, verifies it matches the signed digest
5. Verifies the RSA-SHA1 signature using the certificate's public key

**What can be tested:**
- Send a request with a missing `Signature` header → expect `401 INVALID_SIGNATURE`
- Send a request with an invalid/forged signature → expect `401 INVALID_SIGNATURE`
- Send a request with a stale timestamp (>150 s old) → expect `401 INVALID_TIMESTAMP`
- Send a well-formed, correctly signed payload using the Amazon Skill Events simulator in Alexa Developer Console → expect `200 OK`

**What cannot be tested without Amazon tooling:**
- A correctly signed request cannot be crafted without Amazon's private key. Use the Alexa Developer Console "Test" tab or Amazon's ASK CLI to trigger real SkillAccountLinked / SkillDisabled events.

### Sample Payload Shapes

**SkillAccountLinked:**
```json
{
  "version": "1.0",
  "context": {
    "System": {
      "user": { "userId": "amzn1.ask.account.<string>" },
      "application": { "applicationId": "amzn1.ask.skill.<skill-id>" }
    }
  },
  "request": {
    "type": "AlexaSkillEvent.SkillAccountLinked",
    "requestId": "amzn1.echo-api.request.<string>",
    "timestamp": "2026-11-19T10:00:00Z",
    "body": { "accessToken": "<lwa-access-token>" }
  }
}
```

**SkillDisabled:**
```json
{
  "version": "1.0",
  "context": {
    "System": {
      "user": { "userId": "amzn1.ask.account.<string>" },
      "application": { "applicationId": "amzn1.ask.skill.<skill-id>" }
    }
  },
  "request": {
    "type": "AlexaSkillEvent.SkillDisabled",
    "requestId": "amzn1.echo-api.request.<string>",
    "timestamp": "2026-11-19T10:00:00Z"
  }
}
```

---

## 6. Transport Security (TLS)

| Property | Value |
|----------|-------|
| Protocol minimum | TLS 1.2 |
| Enforced by | AWS API Gateway (platform-level; not configurable per route) |
| TLS 1.0 / 1.1 | Disabled by AWS API Gateway default policy |
| Certificate authority | Amazon-issued certificate on `api.nucleusqa.honeywell.com` |
| HSTS | Not explicitly configured (AWS API GW default) |

> AWS API Gateway enforces a minimum of TLS 1.2 on all custom domain names. Clients attempting TLS 1.0 or 1.1 will receive a connection error before any HTTP layer is reached.

---

## 7. CORS Policy

| Property | Value |
|----------|-------|
| `Access-Control-Allow-Origin` | `*` (wildcard) |
| `Access-Control-Allow-Headers` | `Content-Type, Authorization` |
| `Access-Control-Allow-Methods` | Per-endpoint (GET, POST, DELETE, OPTIONS) |

**Rationale:** The Nucleus mobile app is a native application (iOS/Android). It does not operate inside a browser origin. The wildcard CORS policy is intentional and does not reduce security for native app flows — CORS is a browser-only protection and does not apply to native HTTP clients.

**OAuth redirect endpoints (Group 2)** open in a system browser. The browser is redirected server-side (HTTP 302); no cross-origin XHR is performed, so CORS does not apply to those flows.

---

## 8. Session and Token Lifetimes

| Token / Session Type | Lifetime | Notes |
|---------------------|----------|-------|
| Cognito access token | 1 hour | Standard JWT; validated by API Gateway on every request |
| Cognito refresh token | 180 days | Used by app to silently obtain new access tokens |
| PKCE linking session | 10 minutes | Server-side state for in-progress account linking |
| LWA (Amazon) access token | 1 hour | Stored server-side; used for Alexa proactive events |
| LWA refresh token | Indefinite (until revoked) | Revoked on user-initiated unlink |

---

## 9. WAF and Rate Limiting

| Property | Value |
|----------|-------|
| AWS default rate limit | 10,000 requests/second per API (AWS API Gateway default) |
| Burst limit | 5,000 concurrent requests (AWS default) |
| DDoS protection | AWS Shield Standard (included with API Gateway, no additional configuration) |

> No custom IP allowlisting or geographic blocking is in place. The skill event endpoint (Group 3, endpoint #7) relies on Amazon RSA-SHA1 request signing for authenticity rather than network-layer restrictions.

---

## 10. Skill Information

| Property | Value |
|----------|-------|
| Alexa Skill ID | `amzn1.ask.skill.ebdd110e-1700-4eb9-9195-f7b81fd032d7` |
| Skill Stage | `development` (QA environment) |
| Alexa Developer Account | Contact Mrinal for access |
| Skill Event Webhook | `https://api.nucleusqa.honeywell.com/api/alexa/skill-event` |
| Fulfillment Region | eu-west-1 (AWS Lambda, invoked directly by Amazon) |

---

## 11. Contact / Escalation

For questions about specific implementation details, AWS resource access, or test account credentials, contact the Digilux/Honeywell engineering team (Mrinal).

---

*End of document*
