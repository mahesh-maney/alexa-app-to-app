# Alexa App-to-App Account Linking — Integration Guide

**Version:** 1.2
**Date:** 2026-08-03
**Audience:** Flutter mobile team, backend leads

---

## 1. Overview

This feature allows a Digilux user to link their Alexa account directly from within the Digilux mobile app — without leaving the app to go through a browser. Once linked, the user can control their Digilux devices using Alexa voice commands.

The backend is fully deployed. The mobile team needs to implement the Flutter-side integration described in this document.

---

## 2. How It Works — End-to-End Flow

```
Flutter App                 Digilux Backend              Amazon (Alexa / LWA)
    |                             |                              |
    | User taps "Link Alexa"      |                              |
    |-- POST /startAppToApp ----->|                              |
    |<-- { state, codeChallenge,  |                              |
    |      redirectUri }          |                              |
    |                             |                              |
    | Build Alexa URL and open    |                              |
    | externally (url_launcher)   |                              |
    |-------------------------------------------------------------->|
    |                             |     User sees consent screen  |
    |                             |     User taps "Allow"         |
    |                             |                               |
    |         Amazon redirects to https://iot.digilux.co.in/     |
    |                    smarthome/alexa/callback?code=X&state=Y  |
    |                             |                              |
    | Android App Links intercepts BEFORE browser loads           |
    | Flutter app comes to foreground with code + state           |
    | (iOS Universal Links does the same on iPhone)               |
    |                             |                              |
    | App verifies state, then:   |                              |
    |-- POST /completeAppToApp -->|                              |
    |   { code, state }           |-- POST /auth/o2/token ------>|
    |                             |<-- { access_token, ... }     |
    |                             |-- PUT  /.../enablement ------>|
    |                             |<-- 201 Created               |
    |<-- { "linked": true } ------|                              |
    |                             |                              |
    | Show "Alexa Connected!" screen — user never left the app   |
```

**Step breakdown:**

| Step | Who | What |
|------|-----|-------|
| 1 | Flutter | Call `POST /startAppToApp` to initialize a linking session |
| 2 | Flutter | Build the Alexa companion URL using the returned values |
| 3 | Flutter | Open the Alexa companion URL externally (`LaunchMode.externalApplication`) |
| 4 | User | Sees the Alexa consent screen and taps "Allow" |
| 5 | Amazon | Redirects to `https://iot.digilux.co.in/smarthome/alexa/callback?code=X&state=Y` |
| 6 | Android / iOS | **App Links / Universal Links intercept the URL before the browser loads it** — Flutter app returns to foreground |
| 7 | Flutter | Extract `code` and `state` from the URL; verify `state` matches saved value |
| 8 | Flutter | Call `POST /completeAppToApp` with `code` and `state` |
| 9 | Backend | Exchanges code with Amazon LWA, enables Alexa skill, stores tokens |
| 10 | Flutter | Show "Alexa Connected!" success screen |

> **Fallback (if App Links are not yet configured):** The browser loads the Lambda callback page which fires `digilux://alexa/callback?code=...&state=...` via JavaScript. The user sees a brief browser page then returns to the app. Full App Links setup eliminates this browser step entirely.

---

## 3. API Endpoints

**Base URL:** `https://iot.digilux.co.in/smarthome`

All authenticated endpoints require the Cognito access token in the `Authorization` header:
```
Authorization: Bearer <cognito_access_token>
```

---

### 3.1 Start Account Linking

Initializes a linking session. Call this when the user taps "Connect Alexa".

**Request**
```
POST /api/v1/alexa/startAppToApp
Authorization: Bearer <cognito_access_token>
Content-Type: application/json
```

No request body required.

**Response — 200 OK**
```json
{
  "state":         "a3f2c1d0-e4b5-4678-9abc-def012345678",
  "codeChallenge": "nLyVoqHFjLnz2hO_0tRq5c5wBpHCeFAB8Ys9t6XQTXM",
  "redirectUri":   "https://iot.digilux.co.in/smarthome/alexa/callback"
}
```

| Field | Description |
|-------|-------------|
| `state` | UUID4 — CSRF token. Store this in memory; you must send it back in step 2. |
| `codeChallenge` | PKCE code challenge (BASE64URL-SHA256). Include in the Alexa companion URL. |
| `redirectUri` | Fixed redirect URI registered with Alexa. Include in the companion URL exactly as-is. |

**Error responses**

| HTTP | Meaning |
|------|---------|
| 401 | Missing or invalid Cognito token |
| 429 | Too many pending sessions (max 5 per user per 10 min) |
| 500 | Server configuration error |

---

### 3.2 Complete Account Linking

Call this after extracting `code` and `state` from the deep link callback.

**Request**
```
POST /api/v1/alexa/completeAppToApp
Authorization: Bearer <cognito_access_token>
Content-Type: application/json

{
  "code":  "ANKkDFxyz...",
  "state": "a3f2c1d0-e4b5-4678-9abc-def012345678"
}
```

| Field | Description |
|-------|-------------|
| `code` | Authorization code extracted from the deep link `?code=` parameter |
| `state` | The exact `state` value returned by `startAppToApp` |

**Response — 200 OK**
```json
{
  "linked": true
}
```

**Error responses**

| HTTP | Meaning | Action |
|------|---------|--------|
| 400 | `"Invalid state"` | State not found, wrong user, or expired (> 10 min) — restart the flow |
| 400 | `"Session already used"` | This `state` was already completed — do not retry |
| 400 | `"Linking session expired"` | Session timed out — restart the flow |
| 400 | `"code is required"` / `"state is required"` | Missing field in request body |
| 401 | Unauthenticated | Cognito token missing or invalid |
| 502 | Amazon token exchange failed | Amazon-side error — show error to user, allow retry |

---

### 3.3 Unlink Alexa Account

Unlinks the user's Alexa account. Call this when the user taps "Disconnect Alexa".

**Request**
```
DELETE /api/v1/alexa/unlink
Authorization: Bearer <cognito_access_token>
```

No request body required.

**Response — 200 OK**
```json
{
  "unlinked": true
}
```

**Error responses**

| HTTP | Meaning |
|------|---------|
| 401 | Missing or invalid Cognito token |
| 404 | No linked Alexa account found for this user |

---

## 4. Flutter Integration — Step-by-Step

### 4.1 Dependencies

Add to `pubspec.yaml`:
```yaml
dependencies:
  uni_links: ^0.5.1          # deep link handling
  url_launcher: ^6.2.0       # open Alexa app / browser
```

Add **both** intent-filters to `AndroidManifest.xml` inside your `<activity>` tag:

```xml
<!-- PRIMARY: Android App Links — intercepts redirect BEFORE browser loads.
     Requires assetlinks.json to be verified (done automatically on install). -->
<intent-filter android:autoVerify="true">
  <action android:name="android.intent.action.VIEW" />
  <category android:name="android.intent.category.DEFAULT" />
  <category android:name="android.intent.category.BROWSABLE" />
  <data android:scheme="https"
        android:host="iot.digilux.co.in"
        android:pathPrefix="/smarthome/alexa/callback" />
</intent-filter>

<!-- FALLBACK: Custom scheme — handles digilux:// URI fired by Lambda HTML page
     if App Links verification has not yet completed. -->
<intent-filter>
  <action android:name="android.intent.action.VIEW" />
  <category android:name="android.intent.category.DEFAULT" />
  <category android:name="android.intent.category.BROWSABLE" />
  <data android:scheme="digilux" android:host="alexa" />
</intent-filter>
```

> `assetlinks.json` is already live at `https://iot.digilux.co.in/.well-known/assetlinks.json`. Android verifies it automatically on app install — no manual step needed.

For iOS, two things are required:

**1. `Info.plist`** — register the `digilux://` fallback scheme:
```xml
<key>CFBundleURLTypes</key>
<array>
  <dict>
    <key>CFBundleURLSchemes</key>
    <array><string>digilux</string></array>
  </dict>
</array>
```

**2. Xcode → Signing & Capabilities → Associated Domains** — add:
```
applinks:iot.digilux.co.in
```

> The `apple-app-site-association` file is hosted at `https://iot.digilux.co.in/.well-known/apple-app-site-association` — **pending iOS Team ID and Bundle ID from the mobile team**. Once provided and the AASA is updated, iOS Universal Links will work the same as Android App Links.

---

### 4.2 Step 1 — Start Linking Session

```dart
Future<AlexaStartResponse> startAlexaLinking() async {
  final response = await http.post(
    Uri.parse('$baseUrl/api/v1/alexa/startAppToApp'),
    headers: {
      'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
      'Content-Type': 'application/json',
    },
  );

  if (response.statusCode == 200) {
    return AlexaStartResponse.fromJson(jsonDecode(response.body));
  } else if (response.statusCode == 429) {
    throw Exception('Too many attempts. Please wait a moment and try again.');
  } else {
    throw Exception('Failed to start Alexa linking');
  }
}

class AlexaStartResponse {
  final String state;
  final String codeChallenge;
  final String redirectUri;

  AlexaStartResponse({
    required this.state,
    required this.codeChallenge,
    required this.redirectUri,
  });

  factory AlexaStartResponse.fromJson(Map<String, dynamic> json) =>
      AlexaStartResponse(
        state:         json['state'],
        codeChallenge: json['codeChallenge'],
        redirectUri:   json['redirectUri'],
      );
}
```

---

### 4.3 Step 2 — Build and Open the Alexa Companion URL

```dart
Future<void> openAlexaLinking(AlexaStartResponse session) async {
  // This is the Alexa companion app URL format
  final alexaCompanionUrl = Uri.https(
    'alexa.amazon.com',
    '/app-to-app-linking/default-provider',
    {
      'response_type':         'code',
      'client_id':             'YOUR_LWA_CLIENT_ID',   // from Alexa Developer Console
      'scope':                 'alexa::skills:account_linking',
      'redirect_uri':          session.redirectUri,
      'state':                 session.state,
      'code_challenge':        session.codeChallenge,
      'code_challenge_method': 'S256',
    },
  );

  // Try to open Alexa app first; fall back to browser
  if (await canLaunchUrl(Uri.parse('alexa://'))) {
    await launchUrl(alexaCompanionUrl, mode: LaunchMode.externalApplication);
  } else {
    await launchUrl(alexaCompanionUrl, mode: LaunchMode.externalApplication);
  }
}
```

> **Note:** Replace `YOUR_LWA_CLIENT_ID` with the `client_id` from the Alexa Developer Console → Account Linking settings.

---

### 4.4 Step 3 — Handle the Deep Link Callback

Set up a listener for incoming deep links when the app returns to foreground. The OS will deliver one of:

- `https://iot.digilux.co.in/smarthome/alexa/callback?code=X&state=Y` — Android App Links / iOS Universal Links (primary, no browser shown)
- `digilux://alexa/callback?code=X&state=Y` — custom scheme fallback (Lambda HTML fires this if App Links haven't been verified yet)

The handler below accepts both forms so the flow works during development and after release:

```dart
class AlexaLinkingService {
  StreamSubscription? _linkSub;
  String? _pendingState;   // stored from startAppToApp response

  void startListeningForCallback(String state) {
    _pendingState = state;

    // Also check initial URI in case app was cold-started by the deep link
    getInitialUri().then((uri) { if (uri != null) _handleDeepLink(uri); });

    _linkSub = uriLinkStream.listen(_handleDeepLink, onError: (err) {
      print('Deep link error: $err');
    });
  }

  void stopListening() {
    _linkSub?.cancel();
    _linkSub = null;
    _pendingState = null;
  }

  Future<void> _handleDeepLink(Uri? uri) async {
    if (uri == null) return;

    // Accept both App Links (HTTPS) and custom scheme fallback (digilux://)
    final isCallback =
        (uri.scheme == 'https' &&
         uri.host == 'iot.digilux.co.in' &&
         uri.path == '/smarthome/alexa/callback') ||
        (uri.scheme == 'digilux' &&
         uri.host == 'alexa' &&
         uri.path == '/callback');

    if (!isCallback) return;

    final code  = uri.queryParameters['code'];
    final state = uri.queryParameters['state'];
    final error = uri.queryParameters['error'];

    if (error != null) {
      stopListening();
      // User denied consent
      onError?.call('Alexa linking was cancelled.');
      return;
    }

    if (code == null || state == null) return;

    // SECURITY: verify state matches what we sent — prevents CSRF
    if (state != _pendingState) {
      stopListening();
      onError?.call('Security check failed. Please try again.');
      return;
    }

    stopListening();
    await completeAlexaLinking(code: code, state: state);
  }

  // Callbacks — wire these to your state management (BLoC / Provider / Riverpod)
  Function(String error)? onError;
  Function()? onSuccess;
}
```

---

### 4.5 Step 4 — Complete Linking

```dart
Future<void> completeAlexaLinking({
  required String code,
  required String state,
}) async {
  final response = await http.post(
    Uri.parse('$baseUrl/api/v1/alexa/completeAppToApp'),
    headers: {
      'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
      'Content-Type': 'application/json',
    },
    body: jsonEncode({'code': code, 'state': state}),
  );

  if (response.statusCode == 200) {
    // Success — update UI
    onSuccess?.call();
  } else {
    final body = jsonDecode(response.body);
    final message = switch (response.statusCode) {
      400 => body['error'] ?? 'Invalid linking session.',
      502 => 'Amazon is unavailable. Please try again in a moment.',
      _   => 'Linking failed. Please try again.',
    };
    onError?.call(message);
  }
}
```

---

### 4.6 Step 5 — Unlink

```dart
Future<void> unlinkAlexa() async {
  final response = await http.delete(
    Uri.parse('$baseUrl/api/v1/alexa/unlink'),
    headers: {
      'Authorization': 'Bearer ${await cognitoService.getAccessToken()}',
    },
  );

  if (response.statusCode == 200) {
    // Success — update UI to show "Not connected"
  } else if (response.statusCode == 404) {
    // Already unlinked — treat as success in UI
  } else {
    throw Exception('Failed to unlink Alexa account');
  }
}
```

---

### 4.7 Putting It All Together — Linking Flow

```dart
Future<void> onConnectAlexaTapped() async {
  try {
    // 1. Get session params from backend
    showLoadingIndicator();
    final session = await startAlexaLinking();

    // 2. Start listening for callback BEFORE opening Alexa
    alexaLinkingService.startListeningForCallback(session.state);
    alexaLinkingService.onSuccess = () => showSuccessScreen();
    alexaLinkingService.onError   = (msg) => showErrorScreen(msg);

    // 3. Open Alexa
    hideLoadingIndicator();
    await openAlexaLinking(session);

    // 4. completeAlexaLinking is called automatically by the deep link listener
    //    when the user returns from Alexa

  } catch (e) {
    hideLoadingIndicator();
    showErrorScreen('Could not start Alexa linking. Please try again.');
  }
}
```

---

## 5. Important Rules

### State must be passed back exactly
The `state` value returned by `startAppToApp` is a single-use security token (UUID4). It expires after **10 minutes** and can only be used once.

- Store it in memory only (not persistent storage)
- Pass it back to `completeAppToApp` unchanged
- Always verify the state in the deep link matches the state you stored (see step 4.4 above)

### One session at a time
If the user navigates away and comes back, call `startAppToApp` again to get a fresh session. Old states expire automatically.

### Token refresh
The backend stores the LWA tokens. Your app does not need to manage Alexa tokens — just call the backend.

### Error retries
| Scenario | What to do |
|----------|-----------|
| `completeAppToApp` returns 400 `"Linking session expired"` | Restart from `startAppToApp` |
| `completeAppToApp` returns 400 `"Session already used"` | Do not retry — something went wrong; show error and let user try again |
| `completeAppToApp` returns 502 | Amazon-side issue; allow user to retry (call `startAppToApp` again) |
| User comes back to app without completing Alexa flow | State expires in 10 min; just call `startAppToApp` again on next attempt |

---

## 6. UI/UX Recommendations

### Suggested screens

1. **"Connect Alexa" button** on settings or smart home screen
2. **Loading indicator** while calling `startAppToApp` and opening Alexa
3. **"Waiting for Alexa..."** screen while user is in the Alexa app (with cancel option)
4. **Success screen** — "Alexa Connected! You can now control your Digilux devices with Alexa."
5. **Error screen** with a "Try Again" button
6. **"Disconnect Alexa"** option in settings (calls the unlink endpoint)

### Handling the "cancel" case

If the user presses back from the Alexa app without completing, the deep link listener will not fire. Add a timeout or a "I changed my mind" button that calls `stopListening()` and dismisses the waiting screen.

---

## 7. Alexa Developer Console — One-Time Setup

> This is a backend/infra task, not Flutter. Listed here for completeness.

The following must be configured in the Alexa Developer Console before the flow works end-to-end:

1. Under **Account Linking**, set the **Redirect URLs** to include:
   ```
   https://iot.digilux.co.in/smarthome/alexa/callback
   ```

2. Set **Authorization Grant Type** to `Auth Code Grant`.

3. Enable **PKCE** (Proof Key for Code Exchange).

4. The `client_id` used in step 4.3 above is the **Client ID** shown in the Account Linking settings.

---

## 8. Deep Link Setup Summary

The redirect URI is `https://iot.digilux.co.in/smarthome/alexa/callback`. The OS intercepts this before the browser loads it (App Links / Universal Links), bringing the user directly back to the app with no visible browser transition.

**What the mobile team needs to configure (one-time):**

| Platform | What to add | Where | Status |
|----------|-------------|-------|--------|
| Android | `autoVerify` intent-filter for `https://iot.digilux.co.in` | `AndroidManifest.xml` | Pending rebuild |
| Android | `digilux://alexa` intent-filter (fallback) | `AndroidManifest.xml` | Pending rebuild |
| iOS | `applinks:iot.digilux.co.in` Associated Domain | Xcode → Signing & Capabilities | Pending |
| iOS | `CFBundleURLTypes` with scheme `digilux` (fallback) | `Info.plist` | Pending |

**Server-side files (already live):**

| File | URL | Status |
|------|-----|--------|
| `assetlinks.json` | `https://iot.digilux.co.in/.well-known/assetlinks.json` | **Live** |
| `apple-app-site-association` | `https://iot.digilux.co.in/.well-known/apple-app-site-association` | Pending iOS Team ID + Bundle ID |

**How the fallback works:**
If App Links / Universal Links are not yet verified (e.g., during development before first install), the browser loads the Lambda HTML page which fires `digilux://alexa/callback?code=...&state=...`. The deep link handler in section 4.4 accepts both forms, so the flow works in all cases.

---

## 9. Testing Checklist

Before marking the integration as complete, verify the following:

- [ ] Tapping "Connect Alexa" calls `startAppToApp` and receives `state`, `codeChallenge`, `redirectUri`
- [ ] Alexa companion URL opens externally (Alexa app or browser)
- [ ] After approving in Alexa, the Flutter app returns to foreground **without showing a browser page** (App Links working)
- [ ] Deep link delivers `code` and `state` correctly
- [ ] State validation in Flutter passes (deep link `state` matches stored `state`)
- [ ] `completeAppToApp` returns `{ "linked": true }`
- [ ] Success screen is shown
- [ ] `GET /status` after linking returns `{ "linked": true, "linkedAt": <timestamp> }`
- [ ] Tapping "Disconnect Alexa" calls the unlink endpoint and returns `{ "unlinked": true }`
- [ ] After unlinking, tapping "Connect Alexa" again completes the flow successfully
- [ ] Expired session (wait 10+ minutes before completing) shows a clear error with "Try Again"
- [ ] User denies consent in Alexa → app shows a user-friendly error
- [ ] Fallback: if App Links not verified, browser fallback page opens and "Open Digilux App" button returns to app

---

## 10. Contact

For backend/API questions, contact the backend team. Do not attempt to call the LWA token endpoint or the Alexa Skill Enablement API directly from the app — these are handled entirely server-side.
