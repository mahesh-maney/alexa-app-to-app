# Alexa App-to-App Account Linking — Technical Deep Dive

**Version:** 1.0
**Date:** 2026-08-02
**Audience:** Engineering leads, architects, backend and mobile engineers

---

## Table of Contents

1. [What Is This Feature?](#1-what-is-this-feature)
2. [Core Concepts](#2-core-concepts)
3. [System Architecture](#3-system-architecture)
4. [Data Stores — Tables and Schemas](#4-data-stores--tables-and-schemas)
5. [Attribute Reference — Complete Catalogue](#5-attribute-reference--complete-catalogue)
6. [Linking Flow — Detailed Step by Step](#6-linking-flow--detailed-step-by-step)
7. [Callback Flow](#7-callback-flow)
8. [Unlink Flow — Detailed Step by Step](#8-unlink-flow--detailed-step-by-step)
9. [Security Architecture](#9-security-architecture)
10. [Environment Variables Reference](#10-environment-variables-reference)
11. [CloudWatch Audit Log Events](#11-cloudwatch-audit-log-events)
12. [Error Catalogue](#12-error-catalogue)

---

## 1. What Is This Feature?

When a Digilux user says "Alexa, turn on my living room light", Alexa must know which Digilux account belongs to this user. **Account Linking** is the process of connecting those two identities.

The traditional method requires the user to open a browser, sign in to Digilux again, and approve. **App-to-App Account Linking** eliminates the browser entirely: the user is taken directly from the Digilux Flutter app into the Alexa app on the same device, approves with one tap, and is returned back to Digilux — all without re-entering credentials.

### What gets established at the end

After the flow completes:

- Amazon knows: *"this Alexa user is Digilux user `sub=abc123`"*
- Digilux knows: *"this user has an Alexa LWA access token and refresh token we can use to call Alexa APIs on their behalf"*
- The Alexa skill is activated for this specific user

### What it enables downstream

Once linked, the backend can:
- Receive Alexa skill invocations routed to the right Digilux user
- Proactively send notifications to the user's Alexa devices (proactive events)
- Refresh the access token using the stored refresh token when it expires

---

## 2. Core Concepts

### 2.1 OAuth 2.0 Authorization Code Flow

This feature is built on the standard OAuth 2.0 Authorization Code Grant (RFC 6749). The parties involved are:

| Role | Entity |
|------|--------|
| Resource Owner | The Digilux user |
| Client | The Digilux backend (acting on behalf of the app) |
| Authorization Server | Amazon (Login with Amazon — LWA) |
| Resource Server | Alexa APIs (Skill Enablement, Proactive Events) |

The flow produces two tokens:
- **access_token** — short-lived (typically 1 hour), used to call Alexa APIs right now
- **refresh_token** — long-lived, used to obtain new access tokens when the current one expires

### 2.2 PKCE — Proof Key for Code Exchange (RFC 7636)

PKCE prevents authorization code interception attacks. Without PKCE, if a malicious app on the same device intercepts the redirect URI callback, it can steal the authorization code and exchange it for tokens. PKCE binds the authorization request to the token exchange request cryptographically.

**How it works:**

```
Step 1 (startAppToApp):
  code_verifier  = 32 cryptographically random bytes → BASE64URL encoded (43 chars)
  code_challenge = BASE64URL( SHA-256( code_verifier ) )

  → code_challenge is sent to Amazon in the authorization URL
  → code_verifier is kept secret on the server, never sent to the client

Step 2 (completeAppToApp):
  code_verifier is sent to Amazon along with the authorization code
  Amazon recomputes SHA-256(code_verifier) and verifies it equals code_challenge
  → Only the backend that started the session can complete it
```

The `code_verifier` never leaves the backend. The Flutter app only ever sees the `code_challenge`.

### 2.3 State Parameter — CSRF Token

The `state` is a cryptographically random UUID4 generated fresh for every linking session. It travels:

```
Backend → Flutter app → Alexa authorization URL → Amazon redirects it back
→ Flutter extracts it from deep link → sends it to completeAppToApp
→ Backend verifies it matches the one stored in DynamoDB
```

This protects against Cross-Site Request Forgery (CSRF): an attacker cannot forge a completion request because they do not know the state value, and they cannot inject a state because it must match exactly what is in DynamoDB for that user.

### 2.4 Login with Amazon (LWA)

LWA is Amazon's OAuth 2.0 authorization service. It is the actual authorization server in this flow. The Digilux app is registered as an "LWA Security Profile" in the Amazon developer console, which issues a `client_id` and `client_secret`. These credentials are stored in AWS Secrets Manager and never exposed to the client.

### 2.5 Alexa Skill Enablement API

After exchanging the authorization code for tokens, the backend must explicitly activate the Alexa skill for the user. This is done via:

```
PUT https://api.amazonalexa.com/v1/users/~current/skills/{SKILL_ID}/enablement
Authorization: Bearer <LWA access_token>
Content-Type: application/json

{ "stage": "live" }
```

`~current` is a special identifier that Amazon resolves to the user identified by the Bearer token. Without this call, the skill is linked on the Digilux side but not enabled on the Alexa side, so voice commands will not work.

### 2.6 Cognito JWT Authentication

Every API call from the Flutter app must include the Cognito access token issued during Digilux login. The backend:

1. Extracts the token from the `Authorization: Bearer <token>` header
2. Decodes the JWT payload (base64url) to extract the `sub` claim (the Cognito User ID)
3. Optionally verifies the RS256 signature against Cognito's JWKS endpoint (defense-in-depth)

The `sub` claim becomes the `userId` used throughout the rest of the flow to bind sessions and tokens to the correct user.

---

## 3. System Architecture

### 3.1 AWS Components

```
┌─────────────────────────────────────────────────────────────────────────┐
│  DIGILUX AWS ACCOUNT (ap-south-1)                                       │
│                                                                         │
│  ┌──────────────────┐    ┌─────────────────────────────────────────┐   │
│  │  API Gateway     │    │  Lambda Functions                       │   │
│  │  (REST API)      │    │                                         │   │
│  │                  │    │  alexa_start_app_to_app                 │   │
│  │  POST  /start    │───>│  alexa_complete_app_to_app              │   │
│  │  POST  /complete │    │  alexa_callback                         │   │
│  │  DELETE /unlink  │    │  alexa_unlink                           │   │
│  │  GET   /callback │    └──────────────┬──────────────────────────┘   │
│  │                  │                   │                               │
│  │  Cognito Auth    │    ┌──────────────▼──────────────────────────┐   │
│  │  (start/complete │    │  DynamoDB                               │   │
│  │   /unlink only)  │    │                                         │   │
│  └──────────────────┘    │  alexa_app_linking_sessions             │   │
│                           │  digilux_honeywell_alexa_lwa_tokens    │   │
│  ┌──────────────────┐    └─────────────────────────────────────────┘   │
│  │  AWS WAF         │                                                   │
│  │  (rate limiting  │    ┌─────────────────────────────────────────┐   │
│  │   + common rules)│    │  Secrets Manager (eu-west-1)            │   │
│  └──────────────────┘    │  digilux/alexa/lwa                      │   │
│                           │  { client_id, client_secret }          │   │
│  ┌──────────────────┐    └─────────────────────────────────────────┘   │
│  │  KMS             │                                                   │
│  │  alias/digilux-  │    ┌─────────────────────────────────────────┐   │
│  │  alexa-tokens    │    │  CloudWatch Logs + Alarms               │   │
│  └──────────────────┘    │  90-day retention, 6 security alarms    │   │
│                           └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘

EXTERNAL:
  Amazon LWA:     https://api.amazon.com/auth/o2/token
  Alexa Skill:    https://api.amazonalexa.com/v1/users/~current/skills/
  Cognito JWKS:   https://cognito-idp.ap-south-1.amazonaws.com/{POOL_ID}/.well-known/jwks.json
```

### 3.2 Lambda Functions

| Function | Trigger | HTTP Method | Path | Auth |
|----------|---------|-------------|------|------|
| `alexa_start_app_to_app` | API Gateway | POST | `/api/v1/alexa/startAppToApp` | Cognito |
| `alexa_complete_app_to_app` | API Gateway | POST | `/api/v1/alexa/completeAppToApp` | Cognito |
| `alexa_callback` | API Gateway | GET | `/alexa/callback` | None (public) |
| `alexa_unlink` | API Gateway | DELETE | `/api/v1/alexa/unlink` | Cognito |

### 3.3 IAM Roles — Least Privilege

Each Lambda has its own IAM role with only the permissions it needs:

| Role | DynamoDB | Secrets | KMS |
|------|----------|---------|-----|
| `digilux-alexa-start-role` | sessions: GetItem, PutItem, UpdateItem | — | Encrypt |
| `digilux-alexa-complete-role` | sessions: GetItem, UpdateItem; tokens: PutItem | LWA secret | Decrypt + Encrypt |
| `digilux-alexa-callback-role` | — | — | — |
| `digilux-alexa-unlink-role` | tokens: GetItem, DeleteItem | LWA secret | Decrypt |

---

## 4. Data Stores — Tables and Schemas

### 4.1 Sessions Table: `alexa_app_linking_sessions`

**Purpose:** Stores one record per linking attempt. Acts as the server-side state for PKCE and CSRF protection.

**Partition key:** `state` (String)

**Item schema:**

```json
{
  "state":        "a3f2c1d0-e4b5-4678-9abc-def012345678",
  "userId":       "cognito-sub-uuid",
  "codeVerifier": "KMS_ENCRYPTED_BASE64_STRING",
  "status":       "PENDING",
  "createdAt":    1722550800,
  "expiresAt":    1722551400,
  "ttl":          1722551400
}
```

After `completeAppToApp` succeeds, two more fields are added:

```json
{
  "status":  "USED",
  "usedAt":  1722550950
}
```

**TTL:** DynamoDB automatically deletes items when Unix timestamp `ttl` is passed. Default: `createdAt + 600` seconds (10 minutes). This means expired sessions are cleaned up automatically — no manual purge needed.

**Rate-limit counter items** (also stored in this table):

```json
{
  "state":  "RL#cognito-sub-uuid",
  "userId": "RATELIMIT",
  "count":  3,
  "ttl":    1722551400
}
```

These are atomic counters (DynamoDB `ADD`) that auto-expire along with the session window.

---

### 4.2 Tokens Table: `digilux_honeywell_alexa_lwa_tokens`

**Purpose:** Persistent store of LWA tokens after a successful account link. Used by other backend services to call Alexa APIs on behalf of the user.

**Partition key:** `userId` (String)

**Item schema:**

```json
{
  "userId":       "cognito-sub-uuid",
  "accessToken":  "KMS_ENCRYPTED_BASE64_STRING",
  "refreshToken": "KMS_ENCRYPTED_BASE64_STRING",
  "expiresAt":    1722554340,
  "linkedAt":     1722550950,
  "linkMethod":   "app-to-app"
}
```

`put_item` is used (not `update_item`), so re-linking overwrites the previous record entirely — no stale token fragments are left behind.

---

## 5. Attribute Reference — Complete Catalogue

This section documents every attribute involved in the feature: where it originates, what it contains, where it is stored, and where it flows.

---

### 5.1 `state`

| Property | Value |
|----------|-------|
| **Generated by** | `alexa_start_app_to_app` using `str(uuid.uuid4())` |
| **Format** | UUID v4 string, e.g., `"a3f2c1d0-e4b5-4678-9abc-def012345678"` |
| **Stored in** | DynamoDB `alexa_app_linking_sessions` as the partition key |
| **Returned to client** | Yes — included in `startAppToApp` response |
| **Used in** | Alexa companion URL as `?state=` parameter; returned by Amazon in callback |
| **Sent back by client** | Yes — Flutter sends it in `completeAppToApp` request body |
| **Validated against** | DynamoDB lookup by this key; owner and expiry checked |
| **Lifecycle** | Created as `PENDING` → updated to `USED` → deleted by DynamoDB TTL after 10 min |
| **Security role** | CSRF protection; single-use; UUID4 format validated before DDB lookup |

---

### 5.2 `code_verifier`

| Property | Value |
|----------|-------|
| **Generated by** | `alexa_start_app_to_app`: `base64url( secrets.token_bytes(32) )` |
| **Format** | BASE64URL string, 43 characters, no padding |
| **Stored in** | DynamoDB `alexa_app_linking_sessions.codeVerifier` — KMS-encrypted |
| **Returned to client** | Never — this value never leaves the backend |
| **Used in** | `alexa_complete_app_to_app`: sent to Amazon LWA as `code_verifier` in the token exchange POST |
| **Security role** | PKCE secret; allows Amazon to verify the token exchange came from the same backend that started the session |
| **Encryption** | Encrypted with KMS (`alias/digilux-alexa-tokens`) before DDB write; decrypted before LWA exchange |

---

### 5.3 `code_challenge`

| Property | Value |
|----------|-------|
| **Generated by** | `alexa_start_app_to_app`: `base64url( SHA-256( code_verifier ) )` |
| **Format** | BASE64URL string, 43 characters, no padding |
| **Stored in** | Not stored — computed on the fly and returned to client |
| **Returned to client** | Yes — included in `startAppToApp` response |
| **Used in** | Flutter builds the Alexa companion URL with `?code_challenge=<value>&code_challenge_method=S256` |
| **Sent to Amazon** | Yes — Amazon stores it and later verifies it against the `code_verifier` |
| **Security role** | PKCE public value; safe to send to client and Amazon because SHA-256 is one-way |

---

### 5.4 `redirectUri`

| Property | Value |
|----------|-------|
| **Origin** | Lambda environment variable `REDIRECT_URI` |
| **Value** | `"https://www.digilux.co.in/alexa/callback"` |
| **Returned to client** | Yes — included in `startAppToApp` response |
| **Used in** | Flutter builds Alexa companion URL with `?redirect_uri=<value>` |
| **Used in token exchange** | Yes — sent as `redirect_uri` in the LWA POST; Amazon verifies it exactly matches what was registered |
| **Validation** | Startup: HTTPS scheme check; host must be in `ALLOWED_REDIRECT_HOSTS` if configured |

---

### 5.5 `userId`

| Property | Value |
|----------|-------|
| **Origin** | Extracted from Cognito JWT `sub` claim (fallback: `username` claim) |
| **Format** | Cognito-issued UUID, e.g., `"7d3f9a12-4b8c-11ee-be56-0242ac120002"` |
| **Stored in** | Sessions table as `userId`; tokens table as partition key |
| **Used for** | Binding a session to a specific user; owner-match check in `completeAppToApp`; token lookup in `unlink` |
| **Never in response** | Not returned in any API response body |

---

### 5.6 `code` (authorization code)

| Property | Value |
|----------|-------|
| **Generated by** | Amazon (LWA authorization server) after user approves consent |
| **Format** | Opaque alphanumeric string, typically 20–100 chars, max enforced at 2048 chars |
| **Delivered to** | Flutter app via the redirect URI deep link: `?code=AUTH_CODE&state=STATE` |
| **Sent by client** | Yes — Flutter sends it in `completeAppToApp` request body as `"code"` |
| **Used in** | LWA token exchange POST as `code` parameter |
| **Validity** | Single-use, short-lived (typically 2–5 minutes, Amazon-enforced) |
| **Never stored** | Not persisted anywhere — consumed immediately in the token exchange |

---

### 5.7 `access_token`

| Property | Value |
|----------|-------|
| **Generated by** | Amazon LWA in response to the token exchange |
| **Format** | Opaque Bearer token string (Amazon-issued) |
| **Stored in** | `digilux_honeywell_alexa_lwa_tokens.accessToken` — KMS-encrypted |
| **Used in** | Alexa Skill Enablement API: `Authorization: Bearer <access_token>`; Alexa API calls by other services |
| **Expiry** | `expires_in` seconds from exchange (typically 3600 = 1 hour); `expiresAt = now + expires_in - 60s buffer` |
| **Never returned to client** | Never sent back to the Flutter app |

---

### 5.8 `refresh_token`

| Property | Value |
|----------|-------|
| **Generated by** | Amazon LWA in response to the token exchange |
| **Format** | Opaque long-lived token string (Amazon-issued) |
| **Stored in** | `digilux_honeywell_alexa_lwa_tokens.refreshToken` — KMS-encrypted |
| **Used in** | Token refresh flow (not yet implemented as a Lambda, but this is what enables it); revocation on unlink |
| **Expiry** | No fixed expiry — lasts until explicitly revoked or account is unlinked |
| **Never returned to client** | Never sent back to the Flutter app |

---

### 5.9 `expiresAt`

| Property | Value |
|----------|-------|
| **Stored in** | Sessions table and tokens table |
| **In sessions table** | `createdAt + SESSION_TTL_SECONDS` (default 600 = 10 min); also stored as `ttl` for DynamoDB TTL |
| **In tokens table** | `now + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS` (default buffer = 60s to account for clock skew) |
| **Format** | Unix timestamp (integer seconds) |

---

### 5.10 `status` (session status)

| Value | Set by | Meaning |
|-------|--------|---------|
| `"PENDING"` | `startAppToApp` on session creation | Session is valid and awaiting completion |
| `"USED"` | `completeAppToApp` **before** calling LWA | Session has been consumed; cannot be reused |

The status is flipped to `USED` before the LWA token exchange network call. This prevents a race condition where two simultaneous `completeAppToApp` calls with the same state could both read `PENDING`, both validate, and both exchange the authorization code — wasting one code exchange and potentially storing inconsistent tokens.

---

### 5.11 `createdAt`, `linkedAt`, `usedAt`

| Attribute | Table | Set by | Meaning |
|-----------|-------|--------|---------|
| `createdAt` | sessions | `startAppToApp` | Unix timestamp when the session was created |
| `usedAt` | sessions | `completeAppToApp` | Unix timestamp when the session was marked USED |
| `linkedAt` | tokens | `completeAppToApp` | Unix timestamp when account linking completed |

---

### 5.12 `linkMethod`

| Property | Value |
|----------|-------|
| **Stored in** | tokens table |
| **Value** | Always `"app-to-app"` for this feature |
| **Purpose** | Differentiates links made via this flow from other linking methods (e.g., browser-based) for analytics and debugging |

---

### 5.13 `ttl`

| Property | Value |
|----------|-------|
| **Stored in** | sessions table only |
| **Value** | Same as `expiresAt` |
| **Purpose** | DynamoDB TTL attribute — DynamoDB automatically deletes the item after this Unix timestamp |
| **Note** | Deletion is eventual (within 48 hours of expiry), so `expiresAt` is also checked explicitly in code to catch the lag window |

---

## 6. Linking Flow — Detailed Step by Step

### Phase 1: Start Session — `POST /api/v1/alexa/startAppToApp`

```
Flutter App                          Lambda: alexa_start_app_to_app                     DynamoDB
    │                                              │                                        │
    │ POST /api/v1/alexa/startAppToApp             │                                        │
    │ Authorization: Bearer <cognito_token>        │                                        │
    │─────────────────────────────────────────────>│                                        │
    │                                              │                                        │
    │                                              │ 1. Verify env vars present             │
    │                                              │                                        │
    │                                              │ 2. Extract JWT from Authorization header
    │                                              │    Decode base64(payload) → sub claim  │
    │                                              │    userId = "abc-123-def"              │
    │                                              │                                        │
    │                                              │ 3. RS256 sig verify (if COGNITO_USER_POOL_ID set)
    │                                              │                                        │
    │                                              │ 4. Rate limit check                    │
    │                                              │    DDB update_item ADD count :1        │
    │                                              │    on key "RL#abc-123-def"             │
    │                                              │─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─>│
    │                                              │<─ ─ ─ ─ ─count=1 (≤5, OK) ─ ─ ─ ─ ─ │
    │                                              │                                        │
    │                                              │ 5. Generate state = uuid4()            │
    │                                              │    state = "a3f2c1d0-e4b5-4678-..."   │
    │                                              │                                        │
    │                                              │ 6. Generate PKCE                       │
    │                                              │    verifier  = b64url(rand_bytes(32))  │
    │                                              │    challenge = b64url(SHA256(verifier)) │
    │                                              │                                        │
    │                                              │ 7. KMS encrypt verifier                │
    │                                              │    enc_verifier = kms.encrypt(verifier)│
    │                                              │                                        │
    │                                              │ 8. DDB put_item                        │
    │                                              │    state        = "a3f2c1d0-..."      │
    │                                              │    userId       = "abc-123-def"        │
    │                                              │    codeVerifier = enc_verifier         │
    │                                              │    status       = "PENDING"            │
    │                                              │    createdAt    = 1722550800           │
    │                                              │    expiresAt    = 1722551400           │
    │                                              │    ttl          = 1722551400           │
    │                                              │───────────────────────────────────────>│
    │                                              │<─────────────────── OK ────────────────│
    │                                              │                                        │
    │ 200 OK                                       │                                        │
    │ { "state":         "a3f2c1d0-...",          │                                        │
    │   "codeChallenge": "nLyVoqHFj...",          │                                        │
    │   "redirectUri":   "https://www.digilux..." }│                                        │
    │<─────────────────────────────────────────────│                                        │
```

**What the Flutter app does with the response:**

The app builds the Alexa companion URL by combining the returned values with the LWA `client_id` from the Alexa developer console:

```
https://alexa.amazon.com/app-to-app-linking/default-provider
  ?response_type=code
  &client_id=amzn1.application-oa2-client.XXXXX
  &scope=alexa::skills:account_linking
  &redirect_uri=https://www.digilux.co.in/alexa/callback   ← from response
  &state=a3f2c1d0-e4b5-4678-9abc-def012345678               ← from response
  &code_challenge=nLyVoqHFjLnz2hO_0tRq5c5wBpHCeFAB8Ys9t6XQTXM  ← from response
  &code_challenge_method=S256
```

This URL is opened in the Alexa app (or browser fallback). The `code_verifier` is never in this URL — only the `code_challenge`.

---

### Phase 2: Amazon Consent

Amazon displays a consent screen to the user asking permission to link their Alexa account with Digilux. Amazon:

1. Stores the `state` and `code_challenge` internally
2. Generates an authorization `code` (opaque, short-lived)
3. Redirects the browser to `redirectUri?code=AUTH_CODE&state=STATE`

The OS intercepts this redirect via Android App Links and opens the Digilux Flutter app instead of the browser.

---

### Phase 3: Complete Session — `POST /api/v1/alexa/completeAppToApp`

```
Flutter App                    Lambda: alexa_complete_app_to_app              DynamoDB        Amazon LWA
    │                                         │                                   │                │
    │ POST /api/v1/alexa/completeAppToApp     │                                   │                │
    │ Authorization: Bearer <cognito_token>   │                                   │                │
    │ { "code":  "ANKkDFxyz...",             │                                   │                │
    │   "state": "a3f2c1d0-..." }            │                                   │                │
    │────────────────────────────────────────>│                                   │                │
    │                                         │                                   │                │
    │                                         │ 1. Verify env vars                │                │
    │                                         │                                   │                │
    │                                         │ 2. Extract JWT → userId           │                │
    │                                         │    RS256 sig verify               │                │
    │                                         │                                   │                │
    │                                         │ 3. Body size check (≤4096 bytes)  │                │
    │                                         │                                   │                │
    │                                         │ 4. Parse body → code, state       │                │
    │                                         │    Validate state is UUID4 format │                │
    │                                         │    Validate len(code) ≤ 2048      │                │
    │                                         │                                   │                │
    │                                         │ 5. DDB get_item(state)            │                │
    │                                         │───────────────────────────────────>               │
    │                                         │<──────────── session item ─────────               │
    │                                         │                                   │                │
    │                                         │ 6. Validate session:              │                │
    │                                         │    session.userId == userId? ✓    │                │
    │                                         │    expiresAt > now?  ✓            │                │
    │                                         │    status == PENDING? ✓           │                │
    │                                         │                                   │                │
    │                                         │ 7. KMS decrypt codeVerifier       │                │
    │                                         │    verifier = kms.decrypt(enc)    │                │
    │                                         │                                   │                │
    │                                         │ 8. DDB update_item (ATOMIC)       │                │
    │                                         │    status = "USED"                │                │
    │                                         │    usedAt = now                   │                │
    │                                         │───────────────────────────────────>               │
    │                                         │<─────────────────── OK ────────────               │
    │                                         │   ← state is now USED; replay     │                │
    │                                         │     attempts will fail here       │                │
    │                                         │                                   │                │
    │                                         │ 9. LWA token exchange            │                │
    │                                         │    POST /auth/o2/token            │                │
    │                                         │    grant_type=authorization_code  │                │
    │                                         │    code=ANKkDFxyz...              │                │
    │                                         │    redirect_uri=https://...       │                │
    │                                         │    client_id=amzn1.app...         │                │
    │                                         │    client_secret=...              │                │
    │                                         │    code_verifier=base64url...     │                │
    │                                         │──────────────────────────────────────────────────>│
    │                                         │<── { access_token, refresh_token, expires_in } ───│
    │                                         │                                   │                │
    │                                         │ 10. Alexa Skill Enablement        │                │
    │                                         │    PUT .../skills/{SKILL_ID}/enablement            │
    │                                         │    Authorization: Bearer access_token              │
    │                                         │    { "stage": "live" }                            │
    │                                         │──────────────────────────────────────────────────>│
    │                                         │<───────────────────── 201 Created ────────────────│
    │                                         │                                   │                │
    │                                         │ 11. KMS encrypt access_token      │                │
    │                                         │     KMS encrypt refresh_token     │                │
    │                                         │                                   │                │
    │                                         │ 12. DDB put_item (tokens table)   │                │
    │                                         │    userId       = "abc-123-def"   │                │
    │                                         │    accessToken  = enc_access      │                │
    │                                         │    refreshToken = enc_refresh     │                │
    │                                         │    expiresAt    = now+3540        │                │
    │                                         │    linkedAt     = now             │                │
    │                                         │    linkMethod   = "app-to-app"    │                │
    │                                         │───────────────────────────────────>               │
    │                                         │<─────────────────── OK ────────────               │
    │                                         │                                   │                │
    │ 200 OK                                  │                                   │                │
    │ { "linked": true }                      │                                   │                │
    │<────────────────────────────────────────│                                   │                │
```

---

## 7. Callback Flow

The callback Lambda (`alexa_callback`) handles the public-facing redirect URI `GET /alexa/callback`. It is intentionally simple — it does no DynamoDB writes. Its only job is to redirect the user back into the Digilux app.

### Two paths

**Path A — Android App Links (preferred):**
When Android App Links are configured with `assetlinks.json`, the OS intercepts the URL before the browser loads it and opens the Digilux app directly. The `alexa_callback` Lambda **never fires** in this path.

**Path B — Browser fallback:**
When App Links aren't configured (iOS, browser-only flow, App Links misconfigured), the browser loads the callback URL and the Lambda fires:

```
Amazon redirects browser to:
https://www.digilux.co.in/alexa/callback?code=AUTH_CODE&state=STATE

Lambda receives:
  queryStringParameters.code  = "ANKkDFxyz..."
  queryStringParameters.state = "a3f2c1d0-..."

Lambda builds deep link:
  digilux://alexa/callback?code=ANKkDFxyz...&state=a3f2c1d0-...

Lambda returns HTML page that:
  1. Immediately executes window.location.href = deep_link  → opens Digilux app
  2. After 2-second timeout, shows "Open Digilux App" button as fallback
```

### Error case

If Amazon returns `?error=access_denied` (user denied consent):

```
Lambda returns an HTML error page with:
  - "Linking Failed" heading
  - The error description
  - A "Return to App" button: digilux://alexa/callback?error=access_denied
```

The code and state are URL-encoded before being embedded in the deep link to prevent injection.

---

## 8. Unlink Flow — Detailed Step by Step

```
Flutter App                    Lambda: alexa_unlink              DynamoDB        Amazon LWA / Alexa
    │                                    │                           │                    │
    │ DELETE /api/v1/alexa/unlink        │                           │                    │
    │ Authorization: Bearer <token>      │                           │                    │
    │───────────────────────────────────>│                           │                    │
    │                                    │                           │                    │
    │                                    │ 1. JWT → userId           │                    │
    │                                    │    RS256 sig verify       │                    │
    │                                    │                           │                    │
    │                                    │ 2. DDB get_item           │                    │
    │                                    │    tokens[userId]         │                    │
    │                                    │──────────────────────────>│                    │
    │                                    │<──── token item or {} ─────│                    │
    │                                    │                           │                    │
    │                                    │ (if no item → 404)        │                    │
    │                                    │                           │                    │
    │                                    │ 3. KMS decrypt            │                    │
    │                                    │    accessToken            │                    │
    │                                    │    refreshToken           │                    │
    │                                    │                           │                    │
    │                                    │ 4. [BEST EFFORT]          │                    │
    │                                    │    DELETE .../enablement  │                    │
    │                                    │    Auth: Bearer access_token                   │
    │                                    │────────────────────────────────────────────────>
    │                                    │<─────────────── 200 OK (or error, ignored) ────
    │                                    │                           │                    │
    │                                    │ 5. [BEST EFFORT]          │                    │
    │                                    │    POST /auth/o2/revoke   │                    │
    │                                    │    token=refresh_token    │                    │
    │                                    │    token_type_hint=refresh_token               │
    │                                    │    client_id + client_secret                   │
    │                                    │────────────────────────────────────────────────>
    │                                    │<─────────────── 200 OK (or error, ignored) ────
    │                                    │                           │                    │
    │                                    │ 6. DDB delete_item        │                    │
    │                                    │    tokens[userId]         │                    │
    │                                    │──────────────────────────>│                    │
    │                                    │<──────────── OK ───────────│                    │
    │                                    │                           │                    │
    │ 200 OK                             │                           │                    │
    │ { "unlinked": true }               │                           │                    │
    │<───────────────────────────────────│                           │                    │
```

### Best-effort semantics

Steps 4 (skill disable) and 5 (token revoke) are **best-effort**:
- If they fail (HTTP error, network timeout, Amazon API down), the error is logged as a WARNING but the unlink proceeds regardless.
- The DynamoDB record is **always** deleted — the user is always unlinked from the Digilux side, regardless of Amazon API availability.
- This ensures unlink is not blocked by external dependencies.

---

## 9. Security Architecture

### 9.1 Authentication — Two Layers

| Layer | What | How |
|-------|------|-----|
| API Gateway | Cognito User Pools Authorizer | Validates token presence and expiry before Lambda is invoked |
| Lambda | JWT `sub` extraction + optional RS256 sig verify | Extracts `userId` and verifies signature against Cognito JWKS if `COGNITO_USER_POOL_ID` is set |

The Lambda-level check is defense-in-depth. If the API Gateway authorizer is misconfigured, the Lambda still validates.

### 9.2 CSRF Protection

The `state` parameter is a UUID4 generated per-session and stored in DynamoDB. The `completeAppToApp` Lambda checks:

1. The `state` exists in DynamoDB
2. The `state.userId` matches the JWT `sub` of the requester
3. The `state.status == "PENDING"` (single-use)
4. The `state` is not expired

An attacker who intercepts the Alexa redirect cannot complete the flow unless they also have the victim's Cognito token (auth check) and even then, the state is bound to the userId.

### 9.3 Replay Protection

The session status is atomically set to `USED` **before** the LWA token exchange network call. This means:

- Even if two simultaneous requests arrive with the same `state`, only the first `update_item` will find status `PENDING` — the second will find `USED` and return 400.
- The authorization code is consumed only once.

### 9.4 PKCE Binding

The `code_verifier` stored server-side (and KMS-encrypted) is required to complete the token exchange. Even if an attacker intercepts the authorization code `?code=...` in the redirect, they cannot exchange it without the `code_verifier`, which never leaves the backend.

### 9.5 KMS Field-Level Encryption

Three sensitive values are encrypted with KMS (`alias/digilux-alexa-tokens`) before DynamoDB writes:

| Field | Table | Encrypted? |
|-------|-------|-----------|
| `codeVerifier` | sessions | Yes |
| `accessToken` | tokens | Yes |
| `refreshToken` | tokens | Yes |

If the DynamoDB table is accessed directly (e.g., accidental public exposure, insider threat), these values are useless without KMS access. The KMS key is protected by IAM — each Lambda role only has the minimum KMS actions it needs.

### 9.6 Input Validation

| Validation | Where | Limit |
|-----------|-------|-------|
| Request body size | `completeAppToApp` handler | ≤ 4096 bytes |
| Authorization code length | `completeAppToApp` handler | ≤ 2048 chars |
| State format | `completeAppToApp` handler | Must match UUID4 regex |
| Redirect URI scheme | Startup | Must be HTTPS |
| Redirect URI host | Startup | Must be in `ALLOWED_REDIRECT_HOSTS` |

### 9.7 Rate Limiting

| Layer | Limit |
|-------|-------|
| AWS WAF | 100 requests per 5 minutes per IP |
| Per-user application rate limit | Max 5 pending sessions per user per 10-minute window |

The per-user limit uses a DynamoDB atomic counter (`RL#{userId}`) with the same TTL as sessions, so the window resets automatically.

### 9.8 Token Revocation on Unlink

When a user unlinks, the backend calls Amazon's LWA revocation endpoint with the refresh token. This invalidates both the refresh token and all access tokens derived from it — Amazon can no longer use these to identify the user.

---

## 10. Environment Variables Reference

### `alexa_start_app_to_app`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATA_REGION` | Yes | — | AWS region where DynamoDB tables live |
| `SESSION_TABLE` | Yes | — | DynamoDB table name for linking sessions |
| `REDIRECT_URI` | Yes | — | OAuth redirect URI; must match Alexa Developer Console |
| `SESSION_TTL_SECONDS` | No | `600` | How long a PENDING session is valid (seconds) |
| `MAX_PENDING_SESSIONS_PER_USER` | No | `5` | Per-user rate limit per TTL window |
| `KMS_KEY_ARN` | No | `""` | KMS key for encrypting `codeVerifier`; plaintext if empty |
| `COGNITO_USER_POOL_ID` | No | `""` | Enables RS256 JWT sig verification; skipped if empty |
| `COGNITO_REGION` | No | `DATA_REGION` | Region of the Cognito User Pool |
| `ALLOWED_REDIRECT_HOSTS` | No | `""` | Comma-separated allowlist of redirect URI hosts |
| `LOG_LEVEL` | No | `INFO` | `DEBUG` / `INFO` / `WARNING` |

### `alexa_complete_app_to_app`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATA_REGION` | Yes | — | AWS region |
| `SESSION_TABLE` | Yes | — | Sessions DynamoDB table |
| `LWA_TOKENS_TABLE` | Yes | — | Tokens DynamoDB table |
| `REDIRECT_URI` | Yes | — | Must exactly match what was sent in the authorization URL |
| `LWA_SECRET_ARN` | Yes | — | ARN of the Secrets Manager secret holding LWA credentials |
| `LWA_SECRET_REGION` | Yes | — | Region of the Secrets Manager secret |
| `LWA_TOKEN_URL` | Yes | — | Amazon token endpoint (`https://api.amazon.com/auth/o2/token`) |
| `ALEXA_SKILL_ID` | Yes | — | Alexa skill ID (`amzn1.ask.skill.XXX`) |
| `ALEXA_SKILL_STAGE` | Yes | — | `live` or `development` |
| `SKILL_ENABLEMENT_URL` | Yes | — | `https://api.amazonalexa.com/v1/users/~current/skills` |
| `LWA_HTTP_TIMEOUT` | No | `10` | HTTP timeout (seconds) for LWA calls |
| `TOKEN_EXPIRY_BUFFER_SECONDS` | No | `60` | Subtract from `expires_in` to account for clock skew |
| `MAX_REQUEST_BODY_BYTES` | No | `4096` | Maximum request body size |
| `MAX_AUTH_CODE_LEN` | No | `2048` | Maximum authorization code length |
| `KMS_KEY_ARN` | No | `""` | KMS key for encrypting tokens and decrypting `codeVerifier` |
| `COGNITO_USER_POOL_ID` | No | `""` | JWT signature verification |
| `COGNITO_REGION` | No | `DATA_REGION` | Cognito region |
| `LWA_REVOKE_URL` | No | `""` | Amazon revoke endpoint (used for logging warning only in this Lambda) |
| `LOG_LEVEL` | No | `INFO` | Log verbosity |

### `alexa_callback`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_SCHEME` | Yes | — | Flutter app custom URI scheme (e.g., `digilux`) |
| `LOG_LEVEL` | No | `INFO` | Log verbosity |

### `alexa_unlink`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATA_REGION` | Yes | — | AWS region |
| `LWA_TOKENS_TABLE` | Yes | — | Tokens DynamoDB table |
| `LWA_SECRET_ARN` | Yes | — | Secrets Manager ARN for LWA credentials |
| `LWA_SECRET_REGION` | Yes | — | Region of the Secrets Manager secret |
| `LWA_REVOKE_URL` | No | `""` | Amazon token revocation endpoint |
| `SKILL_ENABLEMENT_URL` | No | `""` | Alexa Skill API base URL |
| `ALEXA_SKILL_ID` | No | `""` | Skill ID for disablement call |
| `KMS_KEY_ARN` | No | `""` | KMS key for decrypting stored tokens |
| `COGNITO_USER_POOL_ID` | No | `""` | JWT signature verification |
| `COGNITO_REGION` | No | `DATA_REGION` | Cognito region |
| `LWA_HTTP_TIMEOUT` | No | `10` | HTTP timeout for Amazon API calls |
| `LOG_LEVEL` | No | `INFO` | Log verbosity |

---

## 11. CloudWatch Audit Log Events

All audit events are prefixed with `[AUDIT]` and logged at `INFO` level. They appear in CloudWatch Logs and can be searched with Logs Insights.

| Event | Lambda | When emitted | Key fields |
|-------|--------|-------------|-----------|
| `[AUDIT] SESSION_CREATED` | start | New PENDING session written to DDB | `userId`, `state`, `created_at`, `expires_at`, `redirect_uri` |
| `[AUDIT] SESSION_USED` | complete | State atomically marked USED | `userId`, `state`, `used_at`, `session_age_seconds` |
| `[AUDIT] SKILL_ENABLED` | complete | Alexa skill enablement API returned 201 | `userId`, `skill_id`, `stage` |
| `[AUDIT] ACCOUNT_LINKED` | complete | Tokens stored; full flow successful | `userId`, `state`, `skill_id`, `stage`, `link_method`, `linked_at`, `token_expires_at` |
| `[AUDIT] LINKING_DENIED` | callback | User denied consent in Alexa | `error`, `description`, `state` |
| `[AUDIT] CALLBACK_SUCCESS` | callback | Auth code delivered to app via deep link | `state`, `code_prefix`, `deep_link_scheme` |
| `[AUDIT] ACCOUNT_UNLINKED` | unlink | Token record deleted from DDB | `userId`, `skill_id`, `skill_disabled`, `token_revoked` |

### Example CloudWatch Logs Insights query — all events for a user

```
fields @timestamp, @message
| filter @message like /AUDIT/
| filter @message like /userId=abc-123-def/
| sort @timestamp asc
```

### Security alarms configured

| Alarm | Pattern | Threshold |
|-------|---------|-----------|
| `alexa-session-already-used` | `SESSION_ALREADY_USED` | ≥ 1 in 5 min |
| `alexa-session-owner-mismatch` | `SESSION_OWNER_MISMATCH` | ≥ 1 in 5 min |
| `alexa-lwa-exchange-failed` | `LWA_EXCHANGE_FAILED` | ≥ 1 in 5 min |
| `alexa-skill-enable-failed` | `SKILL_ENABLE_FAILED` | ≥ 1 in 5 min |
| `alexa-auth-failed` | `AUTH_FAILED` | ≥ 5 in 5 min |
| `alexa-rate-limit-exceeded` | `RATE_LIMIT_EXCEEDED` | ≥ 1 in 5 min |
| `alexa-config-error-*` | `CONFIG_STARTUP_ERROR` | ≥ 1 in 5 min |

---

## 12. Error Catalogue

### `startAppToApp`

| HTTP | Body | Cause |
|------|------|-------|
| 401 | `{"error": "Unauthorized"}` | Missing or invalid Cognito token |
| 429 | `{"error": "Too many pending sessions..."}` | User has ≥ 5 pending sessions in the TTL window |
| 500 | `{"error": "Server configuration error"}` | Required env var missing (startup failure) |

### `completeAppToApp`

| HTTP | Body | Cause |
|------|------|-------|
| 400 | `{"error": "Request body too large"}` | Body exceeds `MAX_REQUEST_BODY_BYTES` |
| 400 | `{"error": "Invalid JSON body"}` | Body is not valid JSON |
| 400 | `{"error": "code is required"}` | `code` field missing or empty |
| 400 | `{"error": "state is required"}` | `state` field missing or empty |
| 400 | `{"error": "Invalid state format"}` | `state` does not match UUID4 pattern |
| 400 | `{"error": "Invalid authorization code"}` | `code` length exceeds `MAX_AUTH_CODE_LEN` |
| 400 | `{"error": "Invalid state"}` | State not found in DDB, or belongs to a different user |
| 400 | `{"error": "Linking session expired — please start again"}` | `expiresAt < now` |
| 400 | `{"error": "Session already used"}` | Session status is `USED` — replay attempt |
| 401 | `{"error": "Unauthorized"}` | Missing or invalid Cognito token |
| 502 | `{"error": "Failed to complete Alexa account linking"}` | Amazon LWA token exchange returned non-2xx |
| 502 | `{"error": "Failed to enable Alexa skill"}` | Alexa Skill Enablement API returned non-2xx |
| 500 | `{"error": "Server configuration error"}` | Required env var missing |

### `alexa_callback`

| HTTP | Body/Page | Cause |
|------|-----------|-------|
| 200 | HTML success page | `code` and `state` present, deep link built |
| 200 | HTML error page | `error` parameter present (user denied) |
| 200 | HTML error page | Missing `code` or `state` parameter |
| 500 | HTML error page | `APP_SCHEME` env var not configured |

### `unlink`

| HTTP | Body | Cause |
|------|------|-------|
| 200 | `{"unlinked": true}` | Success |
| 401 | `{"error": "Unauthorized"}` | Missing or invalid Cognito token |
| 404 | `{"error": "No linked Alexa account found for this user"}` | User has no token record in DDB |
| 500 | `{"error": "Server configuration error"}` | Required env var missing |
