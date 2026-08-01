# Alexa App-to-App — API Sequence Guide

**Version:** 1.0
**Date:** 2026-08-02
**Purpose:** Explains which APIs to call, in what order, with what parameters, and what to expect back. Use this alongside the Postman collection.

---

## Base URL

```
https://5sros9vjc2.execute-api.ap-south-1.amazonaws.com/prod
```

---

## Prerequisites

Before calling any API you need a **Cognito access token**. This is the token issued when the user logs into the Digilux app. It is a JWT that looks like:

```
eyJraWQiOiJY....<long base64 string>
```

Pass it in every authenticated request as:
```
Authorization: Bearer <cognito_access_token>
```

---

## Flow 1 — Link Alexa Account

This is the main linking flow. It involves 2 API calls and one step that happens outside your app (the user approving in Alexa).

```
Your App          Digilux Backend          Amazon (Alexa)
   |                    |                       |
   |── API Call 1 ─────>|                       |
   |<── state +         |                       |
   |    codeChallenge    |                       |
   |    redirectUri      |                       |
   |                    |                       |
   | (open Alexa companion URL)                  |
   |───────────────────────────────────────────>|
   |             (user taps Approve)            |
   |<── deep link: digilux://alexa/callback     |
   |              ?code=AUTH_CODE               |
   |              &state=STATE                  |
   |                    |                       |
   |── API Call 2 ─────>|                       |
   |<── { linked:true } |                       |
```

---

### API Call 1 — Start Linking Session

**When to call:** When the user taps "Connect Alexa" in your app.

```
POST /api/v1/alexa/startAppToApp
Authorization: Bearer <cognito_access_token>
```

**Request body:** None

**Example request:**
```
POST https://5sros9vjc2.execute-api.ap-south-1.amazonaws.com/prod/api/v1/alexa/startAppToApp
Authorization: Bearer eyJraWQiOiJY...
```

---

**Expected response — 200 OK:**

```json
{
    "state":         "a3f2c1d0-e4b5-4678-9abc-def012345678",
    "codeChallenge": "nLyVoqHFjLnz2hO_0tRq5c5wBpHCeFAB8Ys9t6XQTXM",
    "redirectUri":   "https://www.digilux.co.in/alexa/callback"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `state` | string (UUID4) | Your CSRF token. **Save this in memory** — you must send it back in API Call 2. It expires in 10 minutes. |
| `codeChallenge` | string (43 chars) | PKCE code challenge. Include it in the Alexa companion URL. |
| `redirectUri` | string (HTTPS URL) | Fixed redirect URI. Include it in the Alexa companion URL exactly as returned. |

---

**Possible error responses:**

| HTTP | Body | What it means | What to do |
|------|------|---------------|-----------|
| 401 | `{"error": "Unauthorized"}` | Token missing or expired | Re-authenticate the user |
| 429 | `{"error": "Too many pending sessions..."}` | User started linking 5+ times in 10 min without completing | Show "Please wait a moment and try again" |
| 500 | `{"error": "Server configuration error"}` | Backend misconfiguration | Contact backend team |

---

### Interlude — Open Alexa and Wait for Deep Link

After API Call 1, build the Alexa companion URL and open it. This step happens entirely on the device — no API call involved.

**Build the URL:**

```
https://alexa.amazon.com/app-to-app-linking/default-provider
  ?response_type=code
  &client_id=amzn1.application-oa2-client.XXXXXXXXXX
  &scope=alexa::skills:account_linking
  &redirect_uri=<redirectUri from response>
  &state=<state from response>
  &code_challenge=<codeChallenge from response>
  &code_challenge_method=S256
```

Replace `amzn1.application-oa2-client.XXXXXXXXXX` with the LWA Client ID from the Alexa Developer Console.

**Open it:** Use `url_launcher` on Flutter or `Intent`/`UIApplication.open` natively. The Alexa app (or browser fallback) will open.

**Wait for the deep link:** When the user approves (or denies) in Alexa, your app will receive a deep link:

```
Success:  digilux://alexa/callback?code=ANKkDFxyz...&state=a3f2c1d0-...
Denied:   digilux://alexa/callback?error=access_denied
```

**Extract from deep link:**
- `code` — the authorization code. You will send this to API Call 2.
- `state` — **verify this exactly matches the `state` you saved from API Call 1 response**. If it doesn't match, discard and show an error (security check).

---

### API Call 2 — Complete Linking Session

**When to call:** Immediately after your app receives the deep link with `code` and `state`, and you have verified `state` matches.

```
POST /api/v1/alexa/completeAppToApp
Authorization: Bearer <cognito_access_token>
Content-Type: application/json
```

**Request body:**

```json
{
    "code":  "ANKkDFxyz...",
    "state": "a3f2c1d0-e4b5-4678-9abc-def012345678"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | Authorization code from the deep link `?code=` parameter |
| `state` | string (UUID4) | Yes | The `state` value returned by API Call 1 — exactly as returned, no changes |

**Example request:**

```
POST https://5sros9vjc2.execute-api.ap-south-1.amazonaws.com/prod/api/v1/alexa/completeAppToApp
Authorization: Bearer eyJraWQiOiJY...
Content-Type: application/json

{
    "code":  "ANKkDFxyz123ExampleCode",
    "state": "a3f2c1d0-e4b5-4678-9abc-def012345678"
}
```

---

**Expected response — 200 OK:**

```json
{
    "linked": true
}
```

This is the only field in the response. When you see `linked: true`, account linking is complete. Show the success screen.

---

**Possible error responses:**

| HTTP | Body | What it means | What to do |
|------|------|---------------|-----------|
| 400 | `{"error": "code is required"}` | `code` field was empty or missing | Check deep link parsing |
| 400 | `{"error": "state is required"}` | `state` field was empty or missing | Check deep link parsing |
| 400 | `{"error": "Invalid state format"}` | `state` is not a valid UUID4 | Do not tamper with the state value |
| 400 | `{"error": "Invalid state"}` | State not found in DB, or belongs to a different user | Restart from API Call 1 |
| 400 | `{"error": "Linking session expired — please start again"}` | More than 10 minutes passed since API Call 1 | Show "Session expired" — restart from API Call 1 |
| 400 | `{"error": "Session already used"}` | This `state` was already used in a previous completeAppToApp call | Do NOT retry with the same code/state. Restart from API Call 1. |
| 401 | `{"error": "Unauthorized"}` | Cognito token expired or missing | Re-authenticate the user |
| 502 | `{"error": "Failed to complete Alexa account linking"}` | Amazon LWA token exchange failed | Amazon-side issue. Restart from API Call 1 to get a fresh code. |
| 502 | `{"error": "Failed to enable Alexa skill"}` | Alexa Skill Enablement API failed | Amazon-side issue. Restart from API Call 1. |

---

## Flow 2 — Unlink Alexa Account

This is a single API call. Use it when the user taps "Disconnect Alexa".

```
Your App          Digilux Backend          Amazon
   |                    |                    |
   |── API Call 3 ─────>|                    |
   |                    |── disable skill ──>| (best-effort)
   |                    |── revoke token ───>| (best-effort)
   |                    |── delete from DB   |
   |<── { unlinked:true }|                   |
```

---

### API Call 3 — Unlink Account

**When to call:** When the user taps "Disconnect Alexa" in app settings.

```
DELETE /api/v1/alexa/unlink
Authorization: Bearer <cognito_access_token>
```

**Request body:** None

**Example request:**

```
DELETE https://5sros9vjc2.execute-api.ap-south-1.amazonaws.com/prod/api/v1/alexa/unlink
Authorization: Bearer eyJraWQiOiJY...
```

---

**Expected response — 200 OK:**

```json
{
    "unlinked": true
}
```

Account is unlinked. Update your UI to show "Not connected".

---

**Possible error responses:**

| HTTP | Body | What it means | What to do |
|------|------|---------------|-----------|
| 401 | `{"error": "Unauthorized"}` | Cognito token missing or expired | Re-authenticate the user |
| 404 | `{"error": "No linked Alexa account found for this user"}` | User was already unlinked | Treat as success — update UI to "Not connected" |

---

## Flow 3 — Callback (for testing only)

The callback endpoint is public (no auth). It is the OAuth redirect URI that Amazon calls after the user approves consent. In production, Android App Links intercepts this URL before the browser loads it, so this Lambda never fires for Android users.

You would only hit this endpoint directly for:
- Testing the browser fallback HTML page
- Simulating an error callback from Amazon

```
GET /alexa/callback?code=AUTH_CODE&state=STATE
```

**Success case — redirects to deep link:**

```
GET https://5sros9vjc2.execute-api.ap-south-1.amazonaws.com/prod/alexa/callback
  ?code=ANKkDFxyz123
  &state=a3f2c1d0-e4b5-4678-9abc-def012345678
```

Response: `200 OK` HTML page that runs `window.location.href = "digilux://alexa/callback?code=...&state=..."` and shows an "Open Digilux App" button after 2 seconds.

**Error case — user denied consent:**

```
GET .../alexa/callback?error=access_denied&error_description=User+denied+the+request
```

Response: `200 OK` HTML error page with "Linking Failed" message and "Return to App" button.

---

## Quick Reference — All APIs

| # | Method | Path | Auth | Body | Success |
|---|--------|------|------|------|---------|
| 1 | POST | `/api/v1/alexa/startAppToApp` | Cognito Bearer | None | `200 {state, codeChallenge, redirectUri}` |
| 2 | POST | `/api/v1/alexa/completeAppToApp` | Cognito Bearer | `{code, state}` | `200 {linked: true}` |
| 3 | DELETE | `/api/v1/alexa/unlink` | Cognito Bearer | None | `200 {unlinked: true}` |
| — | GET | `/alexa/callback` | None (public) | Query params | `200 HTML` |

---

## Postman Setup Instructions

1. Open Postman → Import → select `Digilux_Alexa_App_to_App.postman_collection.json`

2. In the collection, go to **Variables** tab and set:
   - `cognito_token` — paste a valid Cognito access token
   - `base_url` — already set to the production API Gateway URL

3. Run **Step 1 — Start Account Linking**
   - The test script automatically saves the `state` to `alexa_state` collection variable

4. Build the Alexa companion URL using the response values and open it in a browser or the Alexa app

5. After approving, get the `code` from the redirect URL:
   - The URL will be `https://www.digilux.co.in/alexa/callback?code=...&state=...`
   - Copy the `code` value and paste it into the `alexa_code` collection variable

6. Run **Step 2 — Complete Account Linking**
   - `alexa_state` is already set from step 3
   - `alexa_code` uses what you just pasted

7. Expect `{"linked": true}`

---

## State Lifecycle Diagram

```
                        startAppToApp called
                               │
                               ▼
                    ┌─────────────────────┐
                    │  status: PENDING     │  ← Created in DynamoDB
                    │  expiresAt: now+600s │
                    └──────────┬──────────┘
                               │
                 ┌─────────────┴─────────────┐
                 │                           │
            < 10 minutes               > 10 minutes
                 │                           │
                 ▼                           ▼
    completeAppToApp called          400 Session Expired
                 │
    ┌────────────┴────────────┐
    │                         │
 2nd+ call                 1st call
    │                         │
    ▼                         ▼
400 Session            ┌──────────────┐
Already Used           │ status: USED  │  ← Atomic update
                       └──────┬───────┘
                              │
                       LWA token exchange
                              │
                       Skill enablement
                              │
                       Tokens stored
                              │
                       200 { linked: true }
```

---

## Common Mistakes

| Mistake | Result | Fix |
|---------|--------|-----|
| Using same `state` for a second link attempt | 400 `Session already used` | Always call `startAppToApp` fresh for each link attempt |
| Waiting more than 10 minutes before `completeAppToApp` | 400 `Linking session expired` | Call `startAppToApp` again |
| Sending a different Cognito token in `completeAppToApp` than in `startAppToApp` | 400 `Invalid state` | Use the same logged-in user for both calls |
| Not verifying state in deep link matches stored state | Security risk | Always compare `state` from deep link with `state` from API Call 1 |
| Calling `completeAppToApp` with the same `code` after a 502 | 400 `Session already used` | The state was already consumed; restart from `startAppToApp` |
