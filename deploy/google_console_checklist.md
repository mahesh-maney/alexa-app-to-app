# Google Home Developer Console — Deployment Checklist

Use this checklist **after** running `deploy.sh` and noting its output values.
All placeholder values in `<angle brackets>` must come from your env config file
(e.g. `env.honeywell.prod.json`) or from the deploy script's printed summary.

---

## Prerequisites

- [ ] Google account with access to https://console.home.google.com
- [ ] `deploy.sh` has been run successfully and printed the deployment summary
- [ ] You have the following values from `deploy.sh` output / env config:
  - `API_BASE_URL`         e.g. `https://api.example.com/smarthome`
  - `GOOGLE_CLIENT_ID`     (Cognito App Client ID used as OAuth client_id)
  - `GOOGLE_CLIENT_SECRET` (value stored in Secrets Manager, retrieved manually)
  - `GOOGLE_AGENT_ID`      (project slug, e.g. `honeywell-smarthome-prod`)
  - `GOOGLE_REDIRECT_URI`  e.g. `https://oauth-redirect.googleusercontent.com/r/<GOOGLE_AGENT_ID>`
  - Android app package name (from Flutter build config)
  - Android app SHA-256 signing certificate fingerprint

---

## 1. Create or Open a Google Home Project

- [ ] Go to https://console.home.google.com
- [ ] Click **New project** (first deployment) or open the existing project
- [ ] Set project name: `<APP_NAME> Smart Home`  (e.g. `Honeywell Smart Home`)
- [ ] Note the **Project ID** — this becomes `GOOGLE_AGENT_ID`

---

## 2. Add a Smart Home Action

- [ ] Inside the project, click **Develop** > **Actions**
- [ ] Click **Add action** > **Smart Home**
- [ ] Set **Fulfillment URL**:
  ```
  <API_BASE_URL>/google-home/fulfillment
  ```
  Example: `https://api.example.com/smarthome/google-home/fulfillment`
- [ ] Save

---

## 3. Configure OAuth 2.0 Account Linking

Navigate to **Develop** > **Account linking**

### 3a. OAuth client settings

| Field | Value |
|---|---|
| Client ID | `<GOOGLE_CLIENT_ID>` (Cognito App Client ID) |
| Client secret | `<GOOGLE_CLIENT_SECRET>` (from Secrets Manager) |
| Authorization URL | `<API_BASE_URL>/google-home/authorize` |
| Token exchange URL | `<API_BASE_URL>/google-home/token` |

- [ ] Client ID entered correctly (must match `GOOGLE_CLIENT_ID` in env config and in Cognito)
- [ ] Client secret entered correctly (must match the value in Secrets Manager — **see critical warning below**)
- [ ] Authorization URL entered correctly
- [ ] Token exchange URL entered correctly

### 3b. Scopes

- [ ] Add scope: `profile`
- [ ] Add scope: `openid`  *(if applicable)*

### 3c. Testing instructions (optional but recommended)

Fill in the account linking test instructions with a test user email and password
so Google reviewers can verify linking during certification.

- [ ] Save Account linking configuration

---

## 4. Configure App Flip (Android Deep-Link Account Linking)

App Flip allows the Google Home app to open your Android app directly for
account linking instead of showing a browser-based login page.

Navigate to **Develop** > **Account linking** > **App Flip** (Android tab)

| Field | Value |
|---|---|
| Android package name | `<ANDROID_APP_PACKAGE_NAME>` |
| Android app SHA-256 fingerprint | `<SHA256_SIGNING_CERT_FINGERPRINT>` |
| App Flip intent | `com.google.android.gms.auth.account.linking.START_ACCOUNT_LINKING` |

- [ ] Package name entered — must exactly match the production APK's `applicationId`
- [ ] SHA-256 fingerprint entered — must match the keystore used to sign the release APK
- [ ] Save

> **How to get SHA-256 fingerprint**:
> ```
> keytool -list -v -keystore <your-release.keystore> -alias <alias-name>
> ```
> Use the SHA-256 line from the output (colon-separated hex).

---

## 5. Configure Android App Deep-Link Intent Handling

These steps are in the **Android app code**, not the Google Console — confirm they are done.

- [ ] `AndroidManifest.xml` has an Activity with intent filter:
  ```xml
  <action android:name="com.google.android.gms.auth.account.linking.START_ACCOUNT_LINKING" />
  <category android:name="android.intent.category.DEFAULT" />
  ```
- [ ] That Activity extracts `client_id`, `redirect_uri`, and `state` from the intent extras
- [ ] On successful Cognito authentication, the Activity calls:
  ```
  POST <API_BASE_URL>/voice/google-home/account-linking/deep-link/start
  ```
  and then:
  ```
  POST <GOOGLE_REDIRECT_URI>?code=<auth_code>&state=<state>
  ```
- [ ] Redirect URI in the intent callback matches `GOOGLE_REDIRECT_URI` in env config exactly

---

## 6. Validate OAuth Redirect URI

The redirect URI that Google uses after the user authenticates must be whitelisted.

- [ ] Confirm `GOOGLE_REDIRECT_URI` in env config is:
  ```
  https://oauth-redirect.googleusercontent.com/r/<GOOGLE_AGENT_ID>
  ```
- [ ] Confirm this exact URI is what the authorize Lambda returns in its redirect response
- [ ] Confirm the Lambda's `GOOGLE_REDIRECT_URI` env var matches this value

---

## 7. Enable Google Home Graph API (if using notifications / report state)

- [ ] Go to https://console.cloud.google.com
- [ ] Select the same Google Cloud project linked to your Home action
- [ ] APIs & Services > Enable APIs > search **HomeGraph API** > Enable
- [ ] Create a service account key (JSON) if `reportState` will be used later

---

## 8. Test Account Linking in Google Home App

- [ ] On a real Android device (not emulator — GMS required), open the Google Home app
- [ ] Settings > Works with Google > Search for `<APP_NAME>`
- [ ] Tap Link
- [ ] If App Flip is configured: your app opens directly — log in with Cognito credentials
- [ ] If App Flip is not triggered: browser opens the authorization URL — log in there
- [ ] After login, Google Home app shows the account as linked
- [ ] Verify in CloudWatch: `TOKENS_STORED` log line appears in `google_home_oauth_token` Lambda

---

## 9. Test Voice Commands

After linking:

- [ ] "Hey Google, sync my devices" — triggers SYNC intent
- [ ] Check CloudWatch `google_home_fulfillment` logs for `GH_FULFILLMENT_SYNC` event
- [ ] "Hey Google, turn on [device name]" — triggers EXECUTE intent
- [ ] "Hey Google, is [device name] on?" — triggers QUERY intent

> **Note**: EXECUTE is a stub in the current codebase (`# TODO: send command to device control API`).
> Devices will not physically respond to voice until EXECUTE is implemented.

---

## 10. Submit for Google Review (production launch)

- [ ] All test cases passing in the Google Home test suite
- [ ] Account linking test instructions filled in console
- [ ] Fulfillment URL is publicly accessible (not behind a VPN)
- [ ] OAuth endpoints return correct error codes (`invalid_client`, `invalid_grant`, etc.)
- [ ] Click **Submit** in the Google Home Developer Console for certification review

---

## CRITICAL WARNINGS

### Client Secret Mismatch — 24 to 48 Hour Backoff

**This is the most common failure mode and hardest to debug.**

When Google calls the `/token` endpoint with a `client_secret` that does not match what is
stored in Secrets Manager, the Lambda returns `{"error": "invalid_client"}`.

After repeated `invalid_client` responses, **Google applies a project-level backoff**:
- Google stops calling the `/token` endpoint entirely for 24–48 hours
- The OAuth flow appears to work up to the auth code redirect — but Google silently drops the token exchange
- CloudWatch shows no `/token` Lambda invocations at all during the backoff period
- There is no way to clear this backoff — you must wait it out

**How to prevent this:**
1. Retrieve the actual secret value from Secrets Manager **before** entering it in the Google Console:
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id <GOOGLE_SECRET_ARN> \
     --region <GOOGLE_SECRET_REGION> \
     --query SecretString --output text | python3 -m json.tool
   ```
2. Copy the `client_secret` field value exactly — no leading/trailing whitespace
3. Paste it into the Google Console **Account linking > Client secret** field
4. If you ever rotate the secret in Secrets Manager, update the Google Console immediately
5. Test with a single link attempt before repeated retries

### Never Reuse a Client Secret Across Google Projects

Each Google Home project (Digilux, Honeywell, etc.) must have its own Secrets Manager entry
and its own corresponding entry in the Google Console. Reusing secrets across projects will
cause `invalid_client` errors.

### SHA-256 Fingerprint Must Match Release Build

The App Flip fingerprint in the Google Console must match the keystore that signed the APK
installed on the test device. Debug builds use a different keystore — App Flip will not
trigger for debug APKs unless the debug fingerprint is registered instead.

---

## Quick Reference — Deploy Script Output Values

After running `deploy.sh`, copy these values here before entering them in the console:

```
API_BASE_URL:           _______________________________________
GOOGLE_AGENT_ID:        _______________________________________
GOOGLE_CLIENT_ID:       _______________________________________
GOOGLE_CLIENT_SECRET:   (retrieve from Secrets Manager — see above)
GOOGLE_REDIRECT_URI:    _______________________________________
Authorization URL:      <API_BASE_URL>/google-home/authorize
Token URL:              <API_BASE_URL>/google-home/token
Fulfillment URL:        <API_BASE_URL>/google-home/fulfillment
Android package name:   _______________________________________
SHA-256 fingerprint:    _______________________________________
```
