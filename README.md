# Alexa App-to-App Account Linking

Enables users to link their Amazon Alexa account to Digilux directly from
the mobile app — no browser redirect required.

## Architecture

```
Flutter App
    │
    │ POST /api/v1/alexa/startAppToApp   (Cognito JWT required)
    ▼
alexa_start_app_to_app (Lambda)
    │  Generates: state (UUID) + PKCE verifier/challenge
    │  Stores session in DynamoDB (10-min TTL)
    │  Returns: { state, codeChallenge, redirectUri }
    ▼
Flutter builds Alexa Companion URL and opens it
    │
    ▼
Alexa App (or Login with Amazon browser fallback)
    │  User approves
    ▼
Amazon redirects to https://www.digilux.co.in/alexa/callback?code=X&state=Y
    │
    ├─► Android App Links intercept → Flutter app opens directly
    │
    └─► Browser fallback → alexa_callback Lambda → HTML page with deep link
    │
    │ POST /api/v1/alexa/completeAppToApp   { code, state }
    ▼
alexa_complete_app_to_app (Lambda)
    │  Validates state (CSRF + expiry + single-use)
    │  Marks state USED immediately
    │  Exchanges code + PKCE verifier with Amazon LWA
    │  Stores access + refresh tokens in digilux_honeywell_alexa_lwa_tokens
    │  Returns: { linked: true }
    ▼
Alexa can now discover and control Digilux devices
```

## Lambda Functions

| Function | Trigger | Auth |
|----------|---------|------|
| `alexa_start_app_to_app` | POST /api/v1/alexa/startAppToApp | Cognito JWT |
| `alexa_complete_app_to_app` | POST /api/v1/alexa/completeAppToApp | Cognito JWT |
| `alexa_callback` | GET /alexa/callback | None (public redirect URI) |

## Deployment

```bash
cd infrastructure
chmod +x deploy.sh
./deploy.sh
```

## Testing

```bash
export COGNITO_TOKEN="<valid Cognito JWT>"
chmod +x infrastructure/test.sh
./infrastructure/test.sh
```

## Post-Deployment Checklist

### 1. Alexa Developer Console
Register the redirect URI in your Skill's Account Linking settings:
```
https://www.digilux.co.in/alexa/callback
```

### 2. Android App Links — assetlinks.json
Host the following at `https://www.digilux.co.in/.well-known/assetlinks.json`
so Android intercepts the callback URL and opens the app without the browser:

```json
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "com.digilux.app",
    "sha256_cert_fingerprints": [
      "<YOUR_APP_SIGNING_CERTIFICATE_SHA256_FINGERPRINT>"
    ]
  }
}]
```

Get your SHA-256 fingerprint:
```bash
keytool -list -v -keystore your-keystore.jks -alias your-alias
```

### 3. Flutter Integration

#### Step 1 — "Connect Alexa" button
```dart
ElevatedButton(
  onPressed: _startAlexaLinking,
  child: const Text('Connect Alexa'),
)
```

#### Step 2 — Call startAppToApp
```dart
Future<void> _startAlexaLinking() async {
  final resp = await http.post(
    Uri.parse('https://YOUR_API/api/v1/alexa/startAppToApp'),
    headers: {
      'Authorization': 'Bearer $cognitoToken',
      'Content-Type': 'application/json',
    },
  );
  final body = jsonDecode(resp.body);

  final state          = body['state'];
  final codeChallenge  = body['codeChallenge'];
  final redirectUri    = body['redirectUri'];

  // Save state for later validation
  _pendingState = state;

  // Build Alexa companion URL
  final alexaUrl = Uri.https('alexa.amazon.com', '/spa/skill-account-linking-consent', {
    'client_id':             'YOUR_ALEXA_CLIENT_ID',
    'scope':                 'alexa::skills:account_linking',
    'response_type':         'code',
    'redirect_uri':          redirectUri,
    'state':                 state,
    'code_challenge':        codeChallenge,
    'code_challenge_method': 'S256',
    'skill_stage':           'live',   // or 'development'
  });

  // Try Alexa app first, fall back to browser
  if (!await launchUrl(alexaUrl, mode: LaunchMode.externalApplication)) {
    final lwaUrl = Uri.https('www.amazon.com', '/ap/oa', {
      'client_id':             'YOUR_ALEXA_CLIENT_ID',
      'scope':                 'alexa::skills:account_linking',
      'response_type':         'code',
      'redirect_uri':          redirectUri,
      'state':                 state,
      'code_challenge':        codeChallenge,
      'code_challenge_method': 'S256',
    });
    await launchUrl(lwaUrl, mode: LaunchMode.externalApplication);
  }
}
```

#### Step 3 — Handle callback (AndroidManifest.xml)
```xml
<activity android:name=".MainActivity" ...>
  <intent-filter android:autoVerify="true">
    <action android:name="android.intent.action.VIEW" />
    <category android:name="android.intent.category.DEFAULT" />
    <category android:name="android.intent.category.BROWSABLE" />
    <data
      android:scheme="https"
      android:host="www.digilux.co.in"
      android:pathPrefix="/alexa/callback" />
  </intent-filter>
</activity>
```

#### Step 4 — Extract code + state in Flutter
```dart
// In your app's deep link / intent handler:
void _handleAlexaCallback(Uri uri) async {
  final code  = uri.queryParameters['code'];
  final state = uri.queryParameters['state'];
  final error = uri.queryParameters['error'];

  if (error != null) {
    // Show error to user
    return;
  }

  if (state != _pendingState) {
    // CSRF check failed — discard
    return;
  }

  await _completeAlexaLinking(code!, state!);
}
```

#### Step 5 — Call completeAppToApp
```dart
Future<void> _completeAlexaLinking(String code, String state) async {
  final resp = await http.post(
    Uri.parse('https://YOUR_API/api/v1/alexa/completeAppToApp'),
    headers: {
      'Authorization': 'Bearer $cognitoToken',
      'Content-Type': 'application/json',
    },
    body: jsonEncode({'code': code, 'state': state}),
  );

  if (resp.statusCode == 200) {
    // Show success UI: "Alexa Connected!"
  } else {
    // Show error
  }
}
```

## Security Design

| Mechanism | Purpose |
|-----------|---------|
| PKCE (SHA-256) | Prevents authorization code interception — verifier never leaves the backend |
| State UUID | CSRF protection — Flutter sends it back, backend verifies ownership |
| Single-use state | State marked USED *before* token exchange — prevents replay attacks |
| 10-minute TTL | DynamoDB TTL auto-deletes expired sessions |
| Cognito auth on start/complete | Only authenticated Digilux users can initiate linking |

## DynamoDB Schema

### alexa_app_linking_sessions

| Field | Type | Description |
|-------|------|-------------|
| `state` | String (PK) | UUID — CSRF token |
| `userId` | String | Cognito sub of the user |
| `codeVerifier` | String | PKCE verifier (never returned to client) |
| `status` | String | PENDING → USED |
| `createdAt` | Number | Unix timestamp |
| `expiresAt` | Number | Unix timestamp (createdAt + 600s) |
| `ttl` | Number | DynamoDB TTL attribute (same as expiresAt) |
| `usedAt` | Number | Set when status transitions to USED |
