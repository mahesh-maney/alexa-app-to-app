# Google Home — Lambda Reference

All Lambda functions in the Google Home Cloud-to-Cloud integration, what each one does,
who calls it, and which ones depend on Google's servers.

---

## Overview

```
Flutter App                    Our Lambdas                    Google's Servers
───────────                    ───────────                    ────────────────
Link Account  ──────────────►  google_home_start
                               google_home_oauth_authorize  ◄── GH app (browser / App Flip)
                               google_home_oauth_token      ◄── Google (token exchange)
Poll status   ──────────────►  google_home_status
Complete flow ──────────────►  google_home_complete
Unlink        ──────────────►  google_home_unlink
                               google_home_fulfillment      ◄── Google (voice commands)
```

---

## 1. `google_home_start`

**Endpoint:** `POST /api/v1/voice/google-home/account-linking/deep-link/start`
**Called by:** Flutter app when user taps "Link Account"
**Waits for Google?** No

### What it does
1. Authenticates the Flutter user via the API Gateway Cognito authorizer
2. Creates a session record in `google_home_link_sessions` DynamoDB with `status=PENDING` and a 1800s TTL
3. Builds two URLs and returns them to Flutter:
   - `homeAppDeepLink` — `https://home.google.com/home-app/?deeplink=setup%2Fha_linking%3Fagent_id%3D<AGENT_ID>`
   - `webFallbackUrl` — our `/authorize` Lambda URL with `pre_auth_state` embedded

### Flutter behaviour after receiving the response
Flutter opens `homeAppDeepLink` to launch the Google Home app. If the GH app cannot handle
the deep link, Flutter falls back to opening `webFallbackUrl` directly in the browser.

### DynamoDB writes
| Table | Key | Fields written |
|---|---|---|
| `google_home_link_sessions` | `state` (UUID) | `userId`, `status=PENDING`, `ttl=now+1800` |

---

## 2. `google_home_oauth_authorize`

**Endpoint:** `POST /google-home/oauth/authorize` (also accepts GET)
**Called by:** Three different callers depending on the path
**Waits for Google?** No — it receives requests and responds

### Path A — Web OAuth (GET, browser)
Google Home app opens this URL in a Chrome Custom Tab when App Flip is disabled.

1. Validates `client_id` and `redirect_uri` from query params
2. Checks for `pre_auth_state` — if valid session exists, auto-authorizes without showing the form
3. Otherwise renders an HTML login form (email + password)

### Path B — Web OAuth (POST, form submit)
User submits the login form.

1. Authenticates username + password against Cognito via `USER_PASSWORD_AUTH`
2. On success: generates auth code, stores in `google_home_auth_codes` DynamoDB
3. Redirects to `https://oauth-redirect.googleusercontent.com/r/<AGENT_ID>?code=<CODE>&state=<STATE>`

### Path C — App Flip (POST, Bearer token)
`GoogleHomeAppFlipActivity` (Android) sends the user's Cognito access token instead of a form.

1. Validates the Bearer token via Cognito `get_user` (falls back to JWT decode)
2. Generates auth code, stores in `google_home_auth_codes` DynamoDB
3. Returns `{"code": "<CODE>", "state": ""}` as JSON
4. Android Activity relays this to Google Home via `setResult(RESULT_OK)`

### DynamoDB writes
| Table | Key | Fields written |
|---|---|---|
| `google_home_auth_codes` | `code` | `userId`, `redirectUri`, `scope`, `ttl=now+900` |

---

## 3. `google_home_oauth_token`

**Endpoint:** `POST /google-home/oauth/token`
**Called by:** Google's servers — automatically after receiving an auth code
**Waits for Google?** YES — this Lambda sits idle until Google calls it

### What it does

#### `grant_type=authorization_code` (account linking)
1. Validates `client_id` + `client_secret` against Secrets Manager
2. Looks up the auth code in `google_home_auth_codes` DynamoDB
3. Checks TTL (code expires in 900s)
4. Deletes the code immediately (single-use)
5. Generates opaque `access_token` and `refresh_token`
6. Writes both to `google_home_tokens` DynamoDB — this is the `TOKENS_STORED` event
7. Returns `{"access_token":..., "token_type":"Bearer", "expires_in":3600, "refresh_token":...}`

#### `grant_type=refresh_token` (token renewal)
1. Validates `client_id` + `client_secret`
2. Looks up `refresh_token` in `google_home_tokens` via GSI
3. Issues a new `access_token`, keeps the same `refresh_token`
4. Updates `google_home_tokens` with the new access token

### DynamoDB reads/writes
| Operation | Table | Detail |
|---|---|---|
| Read + delete | `google_home_auth_codes` | Validates and consumes the auth code |
| Write | `google_home_tokens` | Stores `userId`, `accessToken`, `refreshToken`, `expiresAt`, `agentId` |

### Important — token types are opaque
The `access_token` and `refresh_token` stored in `google_home_tokens` are **random opaque strings
we generated** — they are not Google OAuth tokens. When Google calls `oauth2.googleapis.com/revoke`
it expects tokens Google itself issued; calling it with our opaque tokens does nothing. The tokens
Google holds are credentials to call our fulfillment endpoint, not Google API credentials.

---

## 4. `google_home_complete`

**Endpoint:** `POST /api/v1/voice/google-home/account-linking/deep-link/complete`
**Called by:** Flutter app — after the user returns from the browser OAuth flow
**Waits for Google?** No

### What it does
Updates the session in `google_home_link_sessions` from `status=PENDING` to `status=LINKED`
once Flutter detects the user has completed the browser flow and returned to the app.

---

## 5. `google_home_status`

**Endpoint:** `GET /api/v1/voice/google-home/account-linking`
**Called by:** Flutter app — polls every 2 seconds for up to 10 minutes after linking starts
**Waits for Google?** No — but depends on `google_home_oauth_token` having run first

### What it does
1. Reads `google_home_tokens` DynamoDB for the current user's `userId`
2. If a record exists → returns `{"linked": true}`
3. If no record → returns `{"linked": false}`

This is how Flutter knows when Google has completed the token exchange. The UI transitions
from "linking in progress" to "linked" as soon as this returns `true`.

---

## 6. `google_home_fulfillment`

**Endpoint:** `POST /google-home/fulfillment`
**Called by:** Google's servers — every time the user issues a voice command
**Waits for Google?** YES — always listening for Google to send Smart Home intents

### What it does
Handles four Google Smart Home intents:

| Intent | Trigger | What the Lambda does |
|---|---|---|
| `SYNC` | User says "Hey Google, sync my devices" or after linking | Returns list of user's devices from `digilux_user_device_mapping` DynamoDB |
| `QUERY` | "Hey Google, is [device] on?" | Returns current state of the requested devices |
| `EXECUTE` | "Hey Google, turn on [device]" | **STUB — not yet implemented.** Returns success but does not control the device |
| `DISCONNECT` | User unlinks from within Google Home app | Deletes tokens from `google_home_tokens` |

### Important
`EXECUTE` has a `# TODO: send command to Digilux device control API` placeholder.
Voice commands will not physically control devices until this is implemented.

---

## 7. `google_home_unlink`

**Endpoint:** `DELETE /api/v1/voice/google-home/account-linking`
**Called by:** Flutter app when user taps "Unlink Google Home"
**Waits for Google?** No

### What it does
1. Authenticates the Flutter user via the API Gateway Cognito authorizer
2. Fetches the user's token record from `google_home_tokens` DynamoDB (returns 200 if already unlinked — idempotent)
3. **Best-effort:** calls the [Google Home Graph API `deleteAgentUser`](https://developers.home.google.com/cloud-to-cloud/api/reference/rest/v1/agentUsers/delete) endpoint so the GH app immediately reflects the unlinked state
4. Deletes the user's record from `google_home_tokens` DynamoDB
5. Returns `{"linked": false}`

### Why the Home Graph API call is needed
Without step 3, Google's servers retain the link until our issued access token expires (~1 hour).
The GH app will still show "Digilux" as linked for up to one hour after the Digilux app unlinks.
`deleteAgentUser` tells Google to remove the link immediately, which also triggers a `DISCONNECT`
intent to our fulfillment Lambda (which is already handled).

### Home Graph API setup (one-time prerequisite)
Requires `GOOGLE_SA_KEY_SECRET_ARN` env var pointing to a Secrets Manager secret containing a
Google service account JSON key (must have `homegraph.agentUsers.delete` permission).

When `GOOGLE_SA_KEY_SECRET_ARN` is empty the lambda still unlinks on our side — GH will sync
within ~1 hour when token expiry triggers a refresh attempt that fails with `invalid_grant`.

### DynamoDB reads/writes
| Operation | Table | Detail |
|---|---|---|
| Read | `google_home_tokens` | Fetch record to check if linked |
| Delete | `google_home_tokens` | Remove user's token record on unlink |

---

## Summary Table

| Lambda | Called by | Sample Request | Sample Response | Description | Waiting for Google? |
|---|---|---|---|---|---|
| `google_home_start` | Flutter app (user taps "Link Account") | `POST /api/v1/voice/google-home/account-linking/deep-link/start`<br>Header: `Authorization: Bearer <cognito_access_token>` | `{"state":"ec58cec7-...","agentId":"digilux-ai-pro","homeAppDeepLink":"https://home.google.com/home-app/?deeplink=setup%2Fha_linking%3Fagent_id%3Ddigilux-ai-pro","webFallbackUrl":"https://iot.digilux.co.in/smarthome/google-home/oauth/authorize?client_id=...&state=..."}` | Creates a `PENDING` session in DynamoDB and returns the deep link to open the Google Home app. Flutter opens `homeAppDeepLink`; falls back to `webFallbackUrl` if GH app unavailable. | No |
| `google_home_oauth_authorize` (GET — web flow) | Google Home app opens in browser / Chrome Custom Tab | `GET /google-home/oauth/authorize?client_id=75gmuum6...&redirect_uri=https://oauth-redirect.googleusercontent.com/r/digilux-ai-pro&state=abc123&response_type=code&scope=profile` | HTTP 200 — HTML login page (email + password form) | Serves the login form to the user when App Flip is disabled. Validates `client_id` and `redirect_uri` before rendering. | No |
| `google_home_oauth_authorize` (POST — form submit) | User submits login form in browser | `POST /google-home/oauth/authorize`<br>Body (form-encoded): `username=user@example.com&password=secret&client_id=75gmuum6...&redirect_uri=https://oauth-redirect...&state=abc123` | HTTP 302 redirect to `https://oauth-redirect.googleusercontent.com/r/digilux-ai-pro?code=fCuRNruS...&state=abc123` | Authenticates the user against Cognito (`USER_PASSWORD_AUTH`). On success, generates an auth code, stores it in `google_home_auth_codes` DynamoDB (TTL 900s), and redirects to Google's redirect URI with the code. | No |
| `google_home_oauth_authorize` (POST — App Flip) | `GoogleHomeAppFlipActivity` (Android) | `POST /google-home/oauth/authorize`<br>Header: `Authorization: Bearer <cognito_access_token>`<br>Body (JSON): `{"client_id":"75gmuum6...","redirect_uri":"https://oauth-redirect...","state":"","scope":"profile"}` | `{"code":"fCuRNruS...","state":""}` | App Flip path. Validates the Cognito access token via `get_user`, issues an auth code into DynamoDB, and returns it as JSON. The Android Activity delivers this code to Google Home via `setResult(RESULT_OK)`. | No |
| `google_home_oauth_token` | **Google's servers** (after receiving auth code) | `POST /google-home/oauth/token`<br>Body (form-encoded): `grant_type=authorization_code&code=fCuRNruS...&client_id=75gmuum6...&client_secret=g41e4h2i...&redirect_uri=https://oauth-redirect...` | `{"access_token":"Xk9mP2...","token_type":"Bearer","expires_in":3600,"refresh_token":"Yz7nQ4..."}` | Validates Google's `client_id`+`client_secret` against Secrets Manager. Looks up and deletes the auth code from DynamoDB (single-use). Issues opaque `access_token` + `refresh_token` and stores them in `google_home_tokens` DynamoDB. Also handles `grant_type=refresh_token` to issue a new access token. | **Yes — blocked by Google backoff since Sept 18** |
| `google_home_complete` | Flutter app (after user returns from browser) | `POST /api/v1/voice/google-home/account-linking/deep-link/complete`<br>Header: `Authorization: Bearer <cognito_access_token>`<br>Body: `{"state":"ec58cec7-..."}` | `{"linked":true}` | Updates the session in `google_home_link_sessions` from `PENDING` to `LINKED` once Flutter detects the user has returned from the OAuth browser flow. | No |
| `google_home_status` | Flutter app (polls every 2s, up to 10 min) | `GET /api/v1/voice/google-home/account-linking`<br>Header: `Authorization: Bearer <cognito_access_token>` | Linked: `{"linked":true,"linkedAt":"1789991528"}`<br>Not linked: `{"linked":false}` | Reads `google_home_tokens` DynamoDB for the current user. Returns `linked: true` as soon as Google has completed the token exchange. This is how Flutter knows to update the UI to "Google Home linked". | No |
| `google_home_fulfillment` | **Google's servers** (voice commands) | `POST /google-home/fulfillment`<br>Body (JSON): `{"requestId":"ff36...","inputs":[{"intent":"action.devices.SYNC"}],"user":{"userId":"f123dd2a..."}}` | SYNC: `{"requestId":"ff36...","payload":{"agentUserId":"f123dd2a...","devices":[{"id":"device-1","type":"action.devices.types.LIGHT",...}]}}` | Handles all Google Smart Home intents: **SYNC** (return device list), **QUERY** (return device state), **EXECUTE** (control device — stub, not yet implemented), **DISCONNECT** (user unlinked from GH side). | **Yes — always listening for voice commands** |
| `google_home_unlink` | Flutter app (user taps "Unlink") | `DELETE /api/v1/voice/google-home/account-linking`<br>Header: `Authorization: Bearer <cognito_access_token>` | `{"linked":false}` | Calls Google Home Graph API `deleteAgentUser` (best-effort, requires `GOOGLE_SA_KEY_SECRET_ARN`) so GH app immediately shows unlinked, then deletes the record from `google_home_tokens` DynamoDB. Idempotent — returns 200 even if already unlinked. Without the SA key, GH syncs within ~1h on token expiry. | No |

---

## DynamoDB Tables

| Table | Written by | Read by | TTL |
|---|---|---|---|
| `google_home_link_sessions` | `google_home_start` | `google_home_oauth_authorize`, `google_home_complete` | 1800s |
| `google_home_auth_codes` | `google_home_oauth_authorize` | `google_home_oauth_token` (then deleted) | 900s |
| `google_home_tokens` | `google_home_oauth_token` | `google_home_status`, `google_home_fulfillment`, `google_home_unlink` | None (permanent until unlink) |

---

## Known Issues / TODO

| Item | Lambda | Detail |
|---|---|---|
| EXECUTE not implemented | `google_home_fulfillment` | `# TODO: send command to Digilux device control API` — voice commands do not control devices yet |
| Home Graph unlink not configured | `google_home_unlink` | `GOOGLE_SA_KEY_SECRET_ARN` is empty in QA env — GH app shows linked for up to 1h after unlink. Fix: create service account with `homegraph.agentUsers.delete`, store JSON key in Secrets Manager, set the env var, redeploy |
