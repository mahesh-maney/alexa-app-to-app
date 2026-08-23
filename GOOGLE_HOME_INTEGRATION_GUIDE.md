# Google Home Account Linking — Integration Guide

**Version:** 1.0
**Date:** 2026-08-23
**Audience:** Flutter mobile team, backend leads

---

## 1. Overview

This feature allows a Digilux user to link their Google Home account directly from within the Digilux mobile app. Once linked, the user can control their Digilux devices using Google Home voice commands and the Google Home app.

The backend is fully built and unit tested (205/205 passing). **The backend is not yet deployed to AWS** — deployment is pending. The mobile team can review this guide and prepare the Flutter-side implementation in parallel.

---

## 2. How It Works — End-to-End Flow

```
Flutter App                  Digilux Backend               Google
    |                               |                          |
    | User taps "Link Google Home"  |                          |
    |-- POST /start ---------------->|                          |
    |<-- { state, homeAppDeepLink,  |                          |
    |      webFallbackUrl }         |                          |
    |                               |                          |
    | Open homeAppDeepLink          |                          |
    | (Google Home app opens)       |                          |
    |------------------------------------------------------>   |
    |              Google Home shows account linking screen     |
    |              Google Home fetches our OAuth page           |
    |              (pre_auth_state baked into URL — auto-auth)  |
    |                               |<-- GET /oauth/authorize --|
    |                               |--- 302 → Google ----------|
    |                               |<-- POST /oauth/token -----|
    |                               |--- { access_token } ----->|
    |                               |  (token stored in DDB)    |
    |                               |                           |
    | Google Home closes            |                          |
    | App returns to foreground     |                          |
    | (AppLifecycleState.resumed)   |                          |
    |                               |                          |
    |-- POST /complete (state) ----->|                          |
    |<-- { "linked": true }         |                          |
    |                               |                          |
    | Show "Google Home Connected!" |                          |
```

**Step breakdown:**

| Step | Who | What |
|------|-----|------|
| 1 | Flutter | Call `POST /start` to create a session and get linking URLs |
| 2 | Flutter | Open `homeAppDeepLink` to launch Google Home app at account linking screen |
| 3 | Google Home | Opens our OAuth server URL (with `pre_auth_state` for seamless auto-auth) |
| 4 | Backend | Auto-authorizes (user already authenticated in Flutter), issues auth code to Google |
| 5 | Google | Exchanges auth code with our token endpoint, stores our tokens |
| 6 | Google Home | Closes — user returns to Flutter app |
| 7 | Flutter | Detect app foreground resume (`AppLifecycleState.resumed`) |
| 8 | Flutter | Call `POST /complete` with the `state` from step 1 |
| 9 | Backend | Verifies session, checks token record exists → returns `{ "linked": true }` |
| 10 | Flutter | Show success screen |

> **Key difference from Alexa:** There is no deep link callback delivering a `code` back to the Flutter app. The OAuth exchange happens entirely between Google and our backend. Flutter just calls `/complete` with the original `state` after the app resumes — the backend checks whether the token record was written by the OAuth flow.

> **Web fallback:** If the Google Home app is not installed, open `webFallbackUrl` instead. The user logs in via a browser-hosted OAuth page and the flow completes the same way.

---

## 3. API Endpoints

**Base URL:** `https://iot.digilux.co.in/smarthome`

All Flutter-facing endpoints require the Cognito access token:
```
Authorization: Bearer <cognito_access_token>
```

---

### 3.1 Start Account Linking

Initializes a session. Call this when the user taps "Connect Google Home".

**Request**
```
POST /api/v1/voice/google-home/account-linking/deep-link/start
Authorization: Bearer <cognito_access_token>
```

No request body required.

**Response — 200 OK**
```json
{
  "state":           "a3f2c1d0-e4b5-4678-9abc-def012345678",
  "agentId":         "digilux-smarthome",
  "homeAppDeepLink": "https://madeby.google.com/home-app/?deeplink=setup%2Fha_linking%3Fagent_id%3Ddigilux-smarthome",
  "webFallbackUrl":  "https://iot.digilux.co.in/smarthome/google-home/oauth/authorize?pre_auth_state=a3f2c1d0-...&response_type=code&client_id=..."
}
```

| Field | Description |
|-------|-------------|
| `state` | UUID4 session CSRF token. Store in memory — you must send it back in `/complete`. Expires in **10 minutes**. |
| `agentId` | Google Home agent ID for this integration. |
| `homeAppDeepLink` | Open this URL to launch the Google Home app at the account linking screen. |
| `webFallbackUrl` | Use this if the Google Home app is not installed — opens our OAuth login page in the browser. |

**Error responses**

| HTTP | Meaning |
|------|---------|
| 401 | Missing or invalid Cognito token |
| 500 | Server configuration error |

---

### 3.2 Complete Account Linking

Call this after the app returns to foreground following the Google Home / browser OAuth flow.

**Request**
```
POST /api/v1/voice/google-home/account-linking/deep-link/complete
Authorization: Bearer <cognito_access_token>
Content-Type: application/json

{ "state": "a3f2c1d0-e4b5-4678-9abc-def012345678" }
```

| Field | Description |
|-------|-------------|
| `state` | The exact `state` returned by `/start` |

**Response — 200 OK (linked)**
```json
{
  "linked":   true,
  "agentId":  "digilux-smarthome",
  "linkedAt": "2026-08-23T10:15:30Z"
}
```

**Response — 200 OK (not yet linked)**
```json
{
  "linked": false
}
```

> `linked: false` means the user did not complete the Google Home OAuth flow before calling `/complete`. This is not an error — it means the flow was not finished. You can show a "Linking not completed" message with an option to try again.

**Error responses**

| HTTP | Meaning | Action |
|------|---------|--------|
| 400 | `"state is required"` | Missing field in body |
| 400 | `"Invalid or expired session state"` | State not found or wrong user — restart from `/start` |
| 400 | `"Session expired — start a new linking flow"` | 10-minute timeout — restart from `/start` |
| 401 | Unauthenticated | Cognito token missing or invalid |

---

### 3.3 Get Linking Status

Check the current Google Home linking status for the authenticated user. Use this on app launch or settings screen to show the correct state.

**Request**
```
GET /api/v1/voice/google-home/account-linking
Authorization: Bearer <cognito_access_token>
```

**Response — 200 OK**
```json
{
  "linked":   true,
  "agentId":  "digilux-smarthome",
  "linkedAt": "2026-08-23T10:15:30Z"
}
```

When not linked:
```json
{
  "linked": false
}
```

**Error responses**

| HTTP | Meaning |
|------|---------|
| 401 | Missing or invalid Cognito token |

---

### 3.4 Unlink Google Home Account

Unlinks the user's Google Home account. Idempotent — returns 200 even if already unlinked. Also revokes the access token with Google (best-effort, non-fatal if it fails).

**Request**
```
DELETE /api/v1/voice/google-home/account-linking
Authorization: Bearer <cognito_access_token>
```

**Response — 200 OK**
```json
{ "linked": false }
```

**Error responses**

| HTTP | Meaning |
|------|---------|
| 401 | Missing or invalid Cognito token |

---

## 4. Flutter Integration — Step-by-Step

### 4.1 Dependencies

Add to `pubspec.yaml`:
```yaml
dependencies:
  url_launcher: ^6.2.0    # open Google Home app / browser
```

No `uni_links` needed — Google Home has no deep link callback to the Flutter app. Detection is done via app lifecycle.

---

### 4.2 Step 1 — Start Linking Session

```dart
class GoogleHomeLinkingService with WidgetsBindingObserver {
  String? _pendingState;
  bool    _waitingForReturn = false;

  Future<void> startLinking() async {
    final response = await http.post(
      Uri.parse('$baseUrl/api/v1/voice/google-home/account-linking/deep-link/start'),
      headers: {
        'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
        'Content-Type':  'application/json',
      },
    );

    if (response.statusCode != 200) {
      throw Exception('Failed to start Google Home linking');
    }

    final data = jsonDecode(response.body);
    _pendingState = data['state'] as String;

    // Register lifecycle observer BEFORE opening Google Home
    WidgetsBinding.instance.addObserver(this);

    // Open Google Home app (or web fallback)
    final deepLink = Uri.parse(data['homeAppDeepLink'] as String);
    final fallback = Uri.parse(data['webFallbackUrl'] as String? ?? '');

    if (await canLaunchUrl(deepLink)) {
      _waitingForReturn = true;
      await launchUrl(deepLink, mode: LaunchMode.externalApplication);
    } else if (fallback.hasScheme && await canLaunchUrl(fallback)) {
      _waitingForReturn = true;
      await launchUrl(fallback, mode: LaunchMode.externalApplication);
    } else {
      WidgetsBinding.instance.removeObserver(this);
      throw Exception('Could not open Google Home. Please install the Google Home app and try again.');
    }
  }

  // Called when app returns to foreground
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && _waitingForReturn) {
      _waitingForReturn = false;
      WidgetsBinding.instance.removeObserver(this);
      _completeLinking();
    }
  }

  void cancel() {
    _waitingForReturn = false;
    _pendingState = null;
    WidgetsBinding.instance.removeObserver(this);
  }

  // Callbacks — wire to your state management (BLoC / Provider / Riverpod)
  Function(bool linked, String? linkedAt)? onComplete;
  Function(String error)? onError;
}
```

---

### 4.3 Step 2 — Complete Linking (Called Automatically on App Resume)

```dart
Future<void> _completeLinking() async {
  if (_pendingState == null) return;

  try {
    final response = await http.post(
      Uri.parse('$baseUrl/api/v1/voice/google-home/account-linking/deep-link/complete'),
      headers: {
        'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
        'Content-Type':  'application/json',
      },
      body: jsonEncode({'state': _pendingState}),
    );

    _pendingState = null;

    if (response.statusCode == 200) {
      final data   = jsonDecode(response.body);
      final linked = data['linked'] as bool;

      if (linked) {
        onComplete?.call(true, data['linkedAt'] as String?);
      } else {
        // User returned without completing Google Home linking
        onError?.call('Google Home linking was not completed. Please try again.');
      }
    } else {
      final body = jsonDecode(response.body);
      final msg = switch (response.statusCode) {
        400 => body['error'] ?? 'Session expired. Please try again.',
        _   => 'Linking failed. Please try again.',
      };
      onError?.call(msg);
    }
  } catch (e) {
    _pendingState = null;
    onError?.call('Something went wrong. Please try again.');
  }
}
```

---

### 4.4 Step 3 — Check Status on App Launch

```dart
Future<bool> getGoogleHomeLinkStatus() async {
  final response = await http.get(
    Uri.parse('$baseUrl/api/v1/voice/google-home/account-linking'),
    headers: {
      'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
    },
  );

  if (response.statusCode == 200) {
    return (jsonDecode(response.body)['linked'] as bool?) ?? false;
  }
  return false;
}
```

---

### 4.5 Step 4 — Unlink

```dart
Future<void> unlinkGoogleHome() async {
  final response = await http.delete(
    Uri.parse('$baseUrl/api/v1/voice/google-home/account-linking'),
    headers: {
      'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
    },
  );

  if (response.statusCode == 200) {
    // Success — update UI to show "Not connected"
    // { "linked": false } is returned even if already unlinked (idempotent)
  } else {
    throw Exception('Failed to unlink Google Home');
  }
}
```

---

### 4.6 Putting It All Together

```dart
Future<void> onConnectGoogleHomeTapped() async {
  try {
    showLoadingIndicator();

    // Wire up callbacks
    googleHomeLinkingService.onComplete = (linked, linkedAt) {
      hideLoadingIndicator();
      if (linked) {
        showSuccessScreen('Google Home Connected!');
      } else {
        showErrorScreen('Linking was not completed. Please try again.');
      }
    };
    googleHomeLinkingService.onError = (msg) {
      hideLoadingIndicator();
      showErrorScreen(msg);
    };

    // Start — opens Google Home app, registers lifecycle observer
    await googleHomeLinkingService.startLinking();

    // Show "waiting" UI with cancel option
    hideLoadingIndicator();
    showWaitingScreen(
      message: 'Complete linking in Google Home, then return here.',
      onCancel: () {
        googleHomeLinkingService.cancel();
        dismissWaitingScreen();
      },
    );

    // _completeLinking() is called automatically when app resumes

  } catch (e) {
    hideLoadingIndicator();
    showErrorScreen('Could not start Google Home linking. Please try again.');
  }
}
```

---

## 5. Important Rules

### State is a single-use session token
The `state` returned by `/start` is a UUID4 that:
- Expires after **10 minutes**
- Is bound to the authenticated user — another user cannot use it
- Cannot be reused once `/complete` has been called

Always store it in memory (not persistent storage) and discard after use.

### No deep link callback
Unlike Alexa, Google Home does not redirect back to the Flutter app with a `code`. The app detects completion purely by lifecycle (user returning to foreground). This means:

- Always register the lifecycle observer **before** opening Google Home
- Call `/complete` **once** on the first resume after opening Google Home
- Do not call `/complete` on subsequent resumes (use the `_waitingForReturn` flag)

### User may not complete the flow
`/complete` returning `{ "linked": false }` is not a server error — it means the user returned without finishing. Show a user-friendly message and allow them to try again (call `/start` again for a fresh session).

### Unlink is idempotent
Calling unlink when already unlinked returns 200 with `{ "linked": false }` — treat it as success in the UI.

### Token refresh
The backend manages the Google OAuth tokens. Your app does not handle Google tokens — just the Cognito token for authenticating with our API.

---

## 6. Error Handling Reference

| Scenario | What to do |
|----------|-----------|
| `/start` returns 500 | Server error — show generic error, allow retry |
| Google Home app not installed | `canLaunchUrl` returns false for `homeAppDeepLink` — open `webFallbackUrl` instead |
| Web fallback URL also fails | Show message: "Please install the Google Home app to link your account" |
| App resumes, `/complete` returns `linked: false` | User did not complete Google Home flow — show retry option |
| `/complete` returns 400 `"Session expired"` | 10-minute timeout — call `/start` again for a fresh session |
| User taps Cancel on waiting screen | Call `cancel()` to stop listening; no need to call any backend endpoint |
| User force-quits app mid-flow | On next launch, call `GET /status` to check if linking completed in background |

---

## 7. UI/UX Recommendations

### Suggested screens

1. **"Connect Google Home" button** on settings or smart home screen (show current status from `GET /status`)
2. **Loading indicator** while calling `/start`
3. **"Waiting for Google Home..."** screen while the user is in Google Home (with a "Cancel" button)
4. **Success screen** — "Google Home Connected! You can now control your Digilux devices with Google Home."
5. **Error screen** with a "Try Again" button
6. **"Disconnect Google Home"** option in settings (calls `DELETE`)

### Handling the "user didn't finish" case

Show a bottom sheet or snackbar: _"Didn't finish? Open Google Home again to complete the setup."_ Include a retry button that calls `startLinking()` again.

### On app launch / settings screen

Always call `GET /status` to refresh the displayed state — don't rely on local cache.

---

## 8. Platform Setup — Android

No additional deep link intent-filter is needed (unlike Alexa). However, if you want the Google Home app to return users smoothly, ensure your app is in the recent apps stack.

**No changes required to `AndroidManifest.xml` for the Google Home flow.**

---

## 9. Platform Setup — iOS

No URL scheme or Associated Domains changes are required for the Google Home account linking flow on iOS.

**No changes required to `Info.plist` or Xcode project for the Google Home flow.**

---

## 10. Backend Architecture (For Reference)

> This section is for backend leads. Flutter team does not need to implement any of this.

Seven Lambda functions handle the full flow:

| Lambda | Endpoint | Description |
|--------|----------|-------------|
| `google_home_start` | `POST /api/v1/voice/google-home/account-linking/deep-link/start` | Creates session in DynamoDB, returns linking URLs |
| `google_home_complete` | `POST /api/v1/voice/google-home/account-linking/deep-link/complete` | Verifies session state, checks token record |
| `google_home_status` | `GET /api/v1/voice/google-home/account-linking` | Returns current linking status |
| `google_home_unlink` | `DELETE /api/v1/voice/google-home/account-linking` | Revokes Google token, deletes DDB record |
| `google_home_oauth_authorize` | `GET/POST /google-home/oauth/authorize` | Our OAuth authorization server (Google calls this) |
| `google_home_oauth_token` | `POST /google-home/oauth/token` | Our OAuth token endpoint (Google exchanges auth codes here) |
| `google_home_fulfillment` | `POST /google-home/fulfillment` | Handles SYNC / QUERY / EXECUTE / DISCONNECT intents |

**DynamoDB tables:**

| Table | PK | Description |
|-------|-----|-------------|
| `google_home_link_sessions` | `state` | 10-minute TTL sessions |
| `google_home_auth_codes` | `code` | 5-minute TTL OAuth auth codes |
| `google_home_tokens` | `userId` | Access + refresh tokens from Google |

**Note:** Backend deployment (`infrastructure/google_home_deploy.sh`) is pending. All code is written and tested (205/205 unit tests passing on `mahesh-google-app-integration` branch).

---

## 11. Testing Checklist

Before marking the integration as complete:

- [ ] Tapping "Connect Google Home" calls `/start` and receives `state`, `agentId`, `homeAppDeepLink`
- [ ] Google Home app opens at the account linking screen
- [ ] After completing linking in Google Home, the Flutter app returns to foreground
- [ ] `/complete` is called automatically on resume with the correct `state`
- [ ] `/complete` returns `{ "linked": true }` after successful linking
- [ ] Success screen is shown
- [ ] `GET /status` after linking returns `{ "linked": true, "linkedAt": <timestamp> }`
- [ ] Tapping "Disconnect Google Home" calls `DELETE` and returns `{ "linked": false }`
- [ ] After unlinking, `GET /status` returns `{ "linked": false }`
- [ ] Tapping "Connect Google Home" again after unlinking completes the flow successfully
- [ ] Expired session (wait 10+ min before returning to app) shows a clear error with "Try Again"
- [ ] User returns to app without completing Google Home flow → `linked: false` → user-friendly message shown
- [ ] Cancel button on waiting screen stops the listener, no stale call to `/complete`
- [ ] Web fallback: `homeAppDeepLink` fails to open → `webFallbackUrl` opens in browser → linking completes
- [ ] On app launch: `GET /status` correctly reflects linked/unlinked state

---

## 12. Environment Configuration

> This section is for the backend/infra team. Flutter team does not need to configure any of these.

Each Lambda has its own set of environment variables. Set these in the AWS Lambda console or via the deploy script.

### Shared across all 7 Lambdas

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATA_REGION` | **Yes** | — | AWS region for DynamoDB (`ap-south-1`) |
| `LOG_LEVEL` | No | `INFO` | Lambda log verbosity (`DEBUG` / `INFO` / `WARNING` / `ERROR`) |

---

### `google_home_start`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_AGENT_ID` | **Yes** | — | Google Home agent/project ID from Google Cloud Console |
| `OAUTH_BASE_URL` | **Yes** | — | Base URL of our OAuth server (e.g. `https://iot.digilux.co.in/smarthome`) |
| `GH_SESSIONS_TABLE` | No | `google_home_link_sessions` | DynamoDB table for linking sessions |
| `SESSION_TTL_SECONDS` | No | `600` | Session expiry in seconds (10 min) |
| `GOOGLE_CLIENT_ID` | No | — | Google OAuth client ID — used in `webFallbackUrl` query param |
| `GOOGLE_REDIRECT_URI` | No | — | OAuth redirect URI registered in Google Cloud Console |
| `GOOGLE_SCOPE` | No | `profile` | OAuth scopes requested |

---

### `google_home_complete`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_SESSIONS_TABLE` | No | `google_home_link_sessions` | DynamoDB sessions table |
| `GH_TOKENS_TABLE` | No | `google_home_tokens` | DynamoDB tokens table |

---

### `google_home_status`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_TOKENS_TABLE` | No | `google_home_tokens` | DynamoDB tokens table |
| `GOOGLE_AGENT_ID` | No | — | Included in response when token record does not have `agentId` |

---

### `google_home_unlink`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_TOKENS_TABLE` | No | `google_home_tokens` | DynamoDB tokens table |
| `GOOGLE_CLIENT_SECRET_ARN` | No | — | Secrets Manager ARN for Google OAuth credentials — enables token revocation on unlink. If not set, unlink still deletes the DDB record but skips Google revocation. |
| `GOOGLE_SECRET_REGION` | No | `ap-south-1` | Region of the Secrets Manager secret |
| `HTTP_TIMEOUT` | No | `10` | Timeout in seconds for Google revocation HTTP call |

---

### `google_home_oauth_authorize`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_ID` | **Yes** | — | Google OAuth client ID — validated against incoming `client_id` param |
| `COGNITO_CLIENT_ID` | **Yes** | — | Cognito app client ID for user password auth (form login fallback) |
| `COGNITO_USER_POOL_ID` | No | — | Cognito user pool ID |
| `COGNITO_REGION` | No | `DATA_REGION` | Region of the Cognito user pool |
| `ALLOWED_REDIRECT_URIS` | No | — | Comma-separated list of allowed OAuth redirect URIs. If empty, all Google redirect URIs are accepted (not recommended for production). |
| `GH_SESSIONS_TABLE` | No | `google_home_link_sessions` | DynamoDB sessions table (for `pre_auth_state` lookup) |
| `GH_AUTH_CODES_TABLE` | No | `google_home_auth_codes` | DynamoDB auth codes table |
| `AUTH_CODE_TTL_SECONDS` | No | `300` | Auth code expiry in seconds (5 min) |
| `APP_NAME` | No | `Digilux Smart Home` | App name shown on the OAuth login HTML page |

---

### `google_home_oauth_token`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOOGLE_CLIENT_SECRET_ARN` | **Yes** | — | Secrets Manager ARN containing `{ "client_id": "...", "client_secret": "..." }` |
| `GOOGLE_AGENT_ID` | **Yes** | — | Google Home agent ID — stored in token record |
| `GH_AUTH_CODES_TABLE` | No | `google_home_auth_codes` | DynamoDB auth codes table |
| `GH_TOKENS_TABLE` | No | `google_home_tokens` | DynamoDB tokens table |
| `GOOGLE_SECRET_REGION` | No | `ap-south-1` | Region of the Secrets Manager secret |
| `ACCESS_TOKEN_TTL_SECONDS` | No | `3600` | Access token TTL (1 hour) |
| `REFRESH_TOKEN_TTL_SECONDS` | No | `15552000` | Refresh token TTL (180 days) |

---

### `google_home_fulfillment`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GH_TOKENS_TABLE` | No | `google_home_tokens` | DynamoDB tokens table (used to authenticate Google's requests via `accessToken-index` GSI) |
| `USER_DEVICE_MAPPING_TABLE` | No | `digilux_honeywell_user_device_mapping` | DynamoDB table for user→site→device mapping (used for SYNC intent) |

---

### Secrets Manager Secret Format

The secret at `GOOGLE_CLIENT_SECRET_ARN` must be a JSON string:
```json
{
  "client_id":     "your-google-oauth-client-id",
  "client_secret": "your-google-oauth-client-secret"
}
```

Create it with:
```bash
aws secretsmanager create-secret \
  --name digilux-google-home-oauth \
  --region ap-south-1 \
  --secret-string '{"client_id":"YOUR_CLIENT_ID","client_secret":"YOUR_CLIENT_SECRET"}'
```

---

### Sample `.env` (for local testing / deploy script reference)

```bash
# Shared
DATA_REGION=ap-south-1
LOG_LEVEL=INFO

# Google OAuth credentials (from Google Cloud Console)
GOOGLE_AGENT_ID=digilux-smarthome
GOOGLE_CLIENT_ID=123456789-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET_ARN=arn:aws:secretsmanager:ap-south-1:986906626244:secret:digilux-google-home-oauth
GOOGLE_REDIRECT_URI=https://iot.digilux.co.in/smarthome/google-home/oauth/authorize
GOOGLE_SCOPE=profile

# OAuth server
OAUTH_BASE_URL=https://iot.digilux.co.in/smarthome
ALLOWED_REDIRECT_URIS=https://oauth-redirect.googleusercontent.com/r/YOUR_PROJECT_ID,https://oauth-redirect-sandbox.googleusercontent.com/r/YOUR_PROJECT_ID

# Cognito (for form-login fallback in oauth_authorize)
COGNITO_USER_POOL_ID=ap-south-1_h1o8s7257
COGNITO_CLIENT_ID=q7189jitfkk4ttesepkgls491
COGNITO_REGION=ap-south-1

# DynamoDB tables (defaults shown — only set if using non-default names)
GH_SESSIONS_TABLE=google_home_link_sessions
GH_AUTH_CODES_TABLE=google_home_auth_codes
GH_TOKENS_TABLE=google_home_tokens
USER_DEVICE_MAPPING_TABLE=digilux_honeywell_user_device_mapping

# TTLs (defaults shown — only set to override)
SESSION_TTL_SECONDS=600
AUTH_CODE_TTL_SECONDS=300
ACCESS_TOKEN_TTL_SECONDS=3600
REFRESH_TOKEN_TTL_SECONDS=15552000
HTTP_TIMEOUT=10
```

---

## 13. Contact

For backend/API questions, contact the backend team. Do not call the Google OAuth endpoints (`/google-home/oauth/authorize`, `/google-home/oauth/token`, `/google-home/fulfillment`) directly from the app — these are called by Google, not by Flutter.
