# Honeywell Nucleus — Google Home Account Linking
# PSAT API Inventory & Security Reference

**Prepared for:** Honeywell Cybersecurity / PSAT Team
**Environment:** Honeywell QA (nucleusqa) — `https://api.nucleusqa.honeywell.com`
**Date:** 2026-11-19
**Classification:** Internal / Restricted

---

## 1. Account Linking Flows

### Flow A — App-Initiated Linking (Nucleus App → Google Home)

```
1. Nucleus App  →  POST /api/v1/voice/google-home/account-linking/deep-link/start
   Auth: Cognito JWT (Bearer)
   Body: { "siteId": "<id>" }  (optional)
   Response: {
     "state":           "<csrf-token>",
     "agentId":         "honeywell-nucleus",
     "homeAppDeepLink": "https://home.google.com/home-app/?deeplink=...",
     "webFallbackUrl":  "https://auth.nucleusqa.honeywell.com/oauth2/authorize?..."
   }

2. App opens Google Home app deep link (or web fallback if GH app is not installed)
   User authenticates using their Nucleus/Honeywell credentials

3. Google redirects to Authorization URL (registered in Google Actions Console):
   GET /api/google-home/oauth/authorize
   ?client_id=<id>&redirect_uri=<uri>&response_type=code&state=<state>&scope=<scope>
   Server-side: validates params, stores OAuth transaction, redirects to Cognito Hosted UI

4. User logs in on Cognito Hosted UI (auth.nucleusqa.honeywell.com)
   Cognito redirects to:
   GET /api/google-home/oauth/cognito/callback?code=<cognito_code>&state=<state>
   Server: exchanges Cognito code, sets HMAC-signed consent cookie, renders consent page

5. User clicks Allow on the Nucleus consent page
   Browser POSTs to:
   POST /api/google-home/oauth/consent
   Body: action=approve&state=<state>
   Server: verifies cookie + CSRF state, generates auth code, redirects:
   → 302  https://oauth-redirect.googleusercontent.com/r/honeywell-nucleus?code=<auth_code>&state=<state>

6. Google exchanges the auth code at the token endpoint:
   POST /api/google-home/oauth/token
   Body: grant_type=authorization_code&code=<auth_code>&...
   Response: { "access_token", "refresh_token", "expires_in": 3600, "token_type": "Bearer" }

7. Google Home is now linked. Nucleus App confirms:
   POST /api/v1/voice/google-home/account-linking/deep-link/complete
   Auth: Cognito JWT (Bearer)
   Body: { "state": "<csrf-token>" }
   Response: { "linked": true, "agentId": "honeywell-nucleus", "linkedAt": "..." }
```

### Flow B — Nucleus App-Initiated Unlink

```
1. Nucleus App  →  DELETE /api/v1/voice/google-home/account-linking
   Auth: Cognito JWT (Bearer)
   Server: deletes token, calls Google Home Graph API (best-effort) to immediately
           reflect the unlink in the Google Home app. If Google Home Graph call fails,
           Google Home app syncs within ~1 hour when the access token expires.
   Response: { "linked": false }
```

### Flow C — Google Home-Initiated Unlink (User unlinks in Google Home app)

```
1. Google sends DISCONNECT intent to fulfillment endpoint:
   POST /api/google-home/fulfillment
   Body: { "inputs": [{ "intent": "action.devices.DISCONNECT" }] }
   Auth: Bearer <access_token>  (Google issues this; server validates via DDB lookup)

2. Server: deletes token record, fires async notification to set isGoogleEnabled=false
   Response: 200 OK (empty)

3. Next time Nucleus app checks:
   GET /api/v1/voice/google-home/account-linking
   Response: { "linked": false }
```

### Flow D — Token Refresh (Google refreshes an expired access token)

```
1. Google  →  POST /api/google-home/oauth/token
   Body: grant_type=refresh_token&refresh_token=<token>&client_id=<id>&client_secret=<secret>
   Response: { "access_token": "<new>", "token_type": "Bearer", "expires_in": 3600 }
   (refresh token itself does not rotate)
```

---

## 2. API Endpoint Inventory

### Group 1 — Google Home App-to-App Linking (Nucleus App calls these)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 1 | POST | `https://api.nucleusqa.honeywell.com/api/v1/voice/google-home/account-linking/deep-link/start` | Cognito JWT (Bearer) | Initiates GH linking session; returns deep link URLs and CSRF state |
| 2 | POST | `https://api.nucleusqa.honeywell.com/api/v1/voice/google-home/account-linking/deep-link/complete` | Cognito JWT (Bearer) | Verifies session state, confirms linking status after user returns from GH |
| 3 | GET | `https://api.nucleusqa.honeywell.com/api/v1/voice/google-home/account-linking` | Cognito JWT (Bearer) | Returns current GH linking status for the authenticated user |
| 4 | DELETE | `https://api.nucleusqa.honeywell.com/api/v1/voice/google-home/account-linking` | Cognito JWT (Bearer) | Unlinks Google Home for the authenticated user |

### Group 2 — Google OAuth Server Flow (Google / browser calls these)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 5 | GET | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/authorize` | None (public) | Google's entry point for account linking; validates client_id, redirect_uri, state; redirects to Cognito Hosted UI |
| 6 | GET | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/cognito/callback` | None (public) | Receives Cognito auth code after user login; exchanges code, renders consent page with HMAC-signed cookie |
| 7 | POST | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/consent` | HMAC-signed cookie (server-set) | User submits consent (approve / cancel); verifies CSRF state + cookie; issues auth code or redirects with error |
| 8 | POST | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/token` | Google client credentials (Basic Auth or body params) | Standard OAuth2 token endpoint; handles `authorization_code` and `refresh_token` grants |

### Group 3 — Smart Home Fulfillment (Google calls this for voice commands)

| # | Method | Full URL | Auth | Description |
|---|--------|----------|------|-------------|
| 9 | POST | `https://api.nucleusqa.honeywell.com/api/google-home/fulfillment` | Bearer token (server validates via token lookup) | Handles SYNC, QUERY, EXECUTE, DISCONNECT intents from Google Home |

---

## 3. Authentication Summary

| Endpoint Group | Mechanism | Token Source | Verification |
|----------------|-----------|-------------|-------------|
| Group 1 (app API calls) | Bearer JWT | AWS Cognito (Nucleus login) | API Gateway validates JWT signature against Cognito JWKS |
| Group 2 — /authorize | None — public redirect | Google's `state` param (server-side session) | Server validates `client_id`, `redirect_uri` against allowlist; `state` is opaque/unguessable |
| Group 2 — /cognito/callback | None — public redirect | HMAC-signed cookie (set by server, never exposed) | Cookie contains HMAC-SHA256 signature; server verifies on every consent POST |
| Group 2 — /consent | HMAC-signed cookie | Server-issued (120 s TTL) | Cookie signature verified; `state` from form body matched against cookie |
| Group 2 — /token | Google client credentials | Google Actions Console secret | Server verifies `client_id` + `client_secret` from Secrets Manager on every request |
| Group 3 (fulfillment) | Bearer access token | Issued by /token endpoint | Server looks up token in token store; identifies user |

### Consent Cookie Details

The consent flow uses a server-issued HMAC-signed cookie (`gh_consent`) instead of a session cookie or query param. This protects against CSRF and open-redirect attacks.

- Format: `base64url(json_payload).base64url(hmac_sha256_sig)`
- TTL: 120 seconds
- Contents: Cognito access token, Cognito refresh token, Google state parameter
- HttpOnly, Secure flags: set by API Gateway (HTTPS-only endpoint)
- Signing key: 32-character random secret (server-side only, never transmitted)

---

## 4. Error Response Schema

**Group 1 (app API)** — JSON error body:

```json
{ "error": "<error_code>" }
```

HTTP status codes:

| Status | Meaning |
|--------|---------|
| 401 | Missing, expired, or invalid Cognito JWT |
| 500 | Server configuration error or DDB failure |
| 200 | Always returned with `{ "linked": true/false }` for status/complete |

**Group 2 — /token** — RFC 6749 OAuth error body:

```json
{ "error": "<oauth_error>", "error_description": "<detail>" }
```

| Status | OAuth error code | Trigger |
|--------|-----------------|---------|
| 401 | `invalid_client` | client_id or client_secret mismatch |
| 400 | `invalid_request` | Missing `code` or `refresh_token` |
| 400 | `invalid_grant` | Auth code not found, expired, or redirect_uri mismatch |
| 400 | `unsupported_grant_type` | grant_type not `authorization_code` or `refresh_token` |
| 400 | `server_error` | DDB failure or Secrets Manager unavailable |

**Group 2 — /authorize** — Redirect errors (back to Google's `redirect_uri`):

| OAuth error | Trigger |
|-------------|---------|
| `unsupported_response_type` | `response_type` is not `code` |
| `invalid_request` | Missing `state` parameter |
| `server_error` | DDB transaction store failed |

HTML error page (not redirect) returned when `redirect_uri` is itself invalid or `client_id` is wrong.

**Group 2 — /consent** — Redirect errors:

| OAuth error | Trigger |
|-------------|---------|
| `access_denied` | User clicked Deny/Cancel |
| `invalid_request` | Missing cookie, expired cookie, state mismatch |
| `server_error` | DDB failure when creating auth code |

---

## 5. Fulfillment Endpoint (Endpoint #9) — Intent Reference

The fulfillment endpoint handles the following Google Smart Home intents:

| Intent | Trigger | Description |
|--------|---------|-------------|
| `action.devices.SYNC` | User says "Hey Google, sync devices" or first link | Returns the user's full device and scene list |
| `action.devices.QUERY` | "Hey Google, is the living room light on?" | Returns current state of requested devices |
| `action.devices.EXECUTE` | "Hey Google, turn on the fan" | Sends a command to the device via IoT/MQTT |
| `action.devices.DISCONNECT` | User unlinks from Google Home app | Revokes token and clears linked state |

### Sample Request (EXECUTE intent)

```json
{
  "requestId": "<uuid>",
  "inputs": [{
    "intent": "action.devices.EXECUTE",
    "payload": {
      "commands": [{
        "devices": [{ "id": "<encrypted-device-id>" }],
        "execution": [{
          "command": "action.devices.commands.OnOff",
          "params": { "on": true }
        }]
      }]
    }
  }]
}
```

Authorization header on this request: `Bearer <access_token>` — the token issued by `/token`.

### Testability

- Authentication: Send a request with a missing or invalid Bearer token → server returns `{ "requestId": "...", "payload": { "errorCode": "authFailure" } }`
- With a valid token: use the Google Home Developer Console test tool (Actions Console → Test tab) to trigger real SYNC/QUERY/EXECUTE intents
- Without Google tooling: a correct access token obtained through the OAuth flow is required; Google's request format must be replicated exactly

---

## 6. Transport Security (TLS)

| Property | Value |
|----------|-------|
| Protocol minimum | TLS 1.2 |
| Enforced by | AWS API Gateway (platform-level; not configurable per route) |
| TLS 1.0 / 1.1 | Disabled by AWS API Gateway default policy |
| Certificate authority | Amazon-issued certificate on `api.nucleusqa.honeywell.com` |
| HSTS | Not explicitly configured (AWS API GW default) |

---

## 7. CORS Policy

| Property | Value |
|----------|-------|
| `Access-Control-Allow-Origin` | `*` (wildcard) |
| `Access-Control-Allow-Headers` | `Content-Type, Authorization` |
| `Access-Control-Allow-Methods` | Per-endpoint (GET, POST, DELETE, OPTIONS) |

**Rationale:** The Nucleus mobile app is a native application (iOS/Android). It does not operate inside a browser origin. The wildcard CORS policy is intentional and does not reduce security for native app flows — CORS is a browser-only protection and does not apply to native HTTP clients.

The OAuth consent flow (Groups 2) opens in a system browser and relies on server-side 302 redirects — no cross-origin XHR is performed, so CORS does not apply to those flows.

---

## 8. Session and Token Lifetimes

| Token / Session Type | Lifetime | Notes |
|---------------------|----------|-------|
| Cognito access token (Nucleus login) | 1 hour | Validated by API Gateway on every app API call |
| Cognito refresh token | 180 days | Used by app to silently obtain new Cognito access tokens |
| OAuth state transaction (linking in progress) | 10 minutes | Server-side; expired states are rejected |
| App deep-link session (CSRF state) | 30 minutes | Stored server-side; binds user to in-progress linking |
| Consent cookie | 120 seconds | HMAC-signed; binds Cognito tokens to consent page session |
| Auth code (single-use) | 15 minutes | Deleted immediately after Google exchanges it |
| Google Home access token | 1 hour | Issued by `/token`; used by Google for all fulfillment calls |
| Google Home refresh token | 180 days | Used by Google to obtain new access tokens; revoked on unlink |

---

## 9. WAF and Rate Limiting

| Property | Value |
|----------|-------|
| AWS default rate limit | 10,000 requests/second per API (AWS API Gateway default) |
| Burst limit | 5,000 concurrent requests (AWS default) |
| DDoS protection | AWS Shield Standard (included with API Gateway, no additional configuration) |

> No custom IP allowlisting or geographic blocking is in place. The `/authorize` endpoint validates `client_id` and `redirect_uri` against a server-side allowlist to prevent open-redirect abuse. The `/token` endpoint validates Google client credentials on every call.

---

## 10. Google Actions Console Configuration

| Property | Value |
|----------|-------|
| Project / Agent ID | `honeywell-nucleus` |
| Authorization URL | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/authorize` |
| Token URL | `https://api.nucleusqa.honeywell.com/api/google-home/oauth/token` |
| Fulfillment URL | `https://api.nucleusqa.honeywell.com/api/google-home/fulfillment` |
| Redirect URIs (registered) | `https://oauth-redirect.googleusercontent.com/r/honeywell-nucleus` |
| Redirect URIs (sandbox) | `https://oauth-redirect-sandbox.googleusercontent.com/r/honeywell-nucleus` |
| Client ID | Contact Mrinal |
| Client Secret | Contact Mrinal (stored in AWS Secrets Manager) |

---

## 11. Contact / Escalation

For questions about specific implementation details, AWS resource access, Google Console access, or test account credentials, contact the Digilux/Honeywell engineering team (Mrinal).

---

*End of document*
