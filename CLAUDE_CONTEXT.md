# Claude Session Context — Digilux App Integration Handoff

> **How to use this file:**
> Add this file to your Claude session at the start. Say:
> _"I'm continuing work on the Digilux app integration. Read this context file and then I'll tell you what to do next."_
> Claude will have full context and can continue without re-explaining anything.

---

## Project Overview

Two voice integrations for the Digilux smart home app:
1. **Alexa App-to-App account linking** — live and working
2. **Google Home Cloud-to-Cloud account linking** — built and tested, NOT yet deployed

Plus a separate OTA firmware update system (separate repo, separately managed).

---

## Repos & Branches

| Repo | Branch | Status |
|------|--------|--------|
| `mahesh-maney/alexa-app-to-app` | `master` | Alexa live code |
| `mahesh-maney/alexa-app-to-app` | `mahesh-google-app-integration` | All Google Home code + deploy script — **NOT merged** |
| `mahesh-maney/aws-ota-system` | `master` | OTA system — fully deployed |

**Local paths:**
- App-to-app repo: `/Users/maheshmaney/maney/digilux/app-to-app`
- OTA repo: `/Users/maheshmaney/maney/digilux/aws-cloud/ota`
- Admin web interface: `/Users/maheshmaney/maney/digilux/aws-cloud/admin-web-interface`

---

## AWS Details

| Key | Value |
|-----|-------|
| API Gateway ID | `ds6nxf8ac5` |
| Live stage | `smarthome` |
| Base URL | `https://iot.digilux.co.in/smarthome/api/v1` |
| Region | `ap-south-1` |
| AWS Account | `986906626244` |
| OTA Admin Cognito pool | `ap-south-1_jUErEu7CL` · client `2qmig1uh220ttntbl0gfvcde4f` |
| Main app Cognito pool | `ap-south-1_h1o8s7257` · client `q7189jitfkk4ttesepkgls491` |

> **Critical:** The old `infrastructure/deploy.sh` targets API `5sros9vjc2` and stage `prod` — both wrong. Do NOT use it. Use `infrastructure/deploy_all.sh` instead.

---

## What Was Done (this session)

### Google Home — 7 Lambdas written + tested (205/205 pass)
All on branch `mahesh-google-app-integration`:

| Lambda | Endpoint | Notes |
|--------|----------|-------|
| `google_home_start` | `POST /api/v1/voice/google-home/account-linking/deep-link/start` | Creates DDB session, returns deep link URLs |
| `google_home_complete` | `POST /api/v1/voice/google-home/account-linking/deep-link/complete` | Verifies session, checks token written |
| `google_home_status` | `GET /api/v1/voice/google-home/account-linking` | Returns link status |
| `google_home_unlink` | `DELETE /api/v1/voice/google-home/account-linking` | Revokes Google token, deletes DDB |
| `google_home_oauth_authorize` | `GET/POST /google-home/oauth/authorize` | Our OAuth server (Google calls this) |
| `google_home_oauth_token` | `POST /google-home/oauth/token` | Google exchanges auth codes here |
| `google_home_fulfillment` | `POST /google-home/fulfillment` | SYNC/QUERY/EXECUTE/DISCONNECT |

**DynamoDB tables** (created by deploy script):
- `google_home_link_sessions` — 10-min TTL
- `google_home_auth_codes` — 5-min TTL
- `google_home_tokens` — user access + refresh tokens

**Key design note:** No deep link callback from Google to Flutter. Flutter detects completion via `AppLifecycleState.resumed`, then calls `/complete` with the original `state`. Backend checks if token record was written during the OAuth exchange.

### SYNC works. QUERY and EXECUTE are stubs.
`google_home_fulfillment`:
- SYNC returns device list from `digilux_honeywell_user_device_mapping` table — working
- QUERY returns `online: true` placeholder — NOT connected to real device state
- EXECUTE logs the command but sends nothing to devices

QUERY/EXECUTE need to be wired to the Digilux device control API (separate task, after account linking is verified).

### Zero-touch deploy script — `infrastructure/deploy_all.sh`
```bash
./infrastructure/deploy_all.sh --alexa              # Alexa only
./infrastructure/deploy_all.sh --google             # Google Home only
./infrastructure/deploy_all.sh --all                # Both
./infrastructure/deploy_all.sh --google --dry-run   # Validate only, no changes
```
Handles: DynamoDB tables, IAM roles, Lambda packaging + deploy, API Gateway routes, CORS, stage deploy, smoke tests.

### Alexa bug fix — written but NOT redeployed
`alexa_unlink` Lambda crashes with `AttributeError` when `HTTPError` has no response body.
Fix: added `if e.fp:` guard before `e.read()`.
Fix is in source code but was never deployed to the correct API (`ds6nxf8ac5`). Old deploy.sh targeted wrong API.
**Action: run `./infrastructure/deploy_all.sh --alexa` to fix.**

### Docs written (on `mahesh-google-app-integration`):
- `GOOGLE_HOME_INTEGRATION_GUIDE.md` — full Flutter integration guide (flow, API docs, Dart code, env vars, deployment checklist)
- `infrastructure/deploy_all.sh` — zero-touch deploy script

### OTA system (separate repo `aws-ota-system`):
- New Lambda `digilux_ota_beta_users` deployed — GET/POST/DELETE `/ota/beta-users`, auto-resolves email → deviceId
- Fixed `digilux_ota_user_consent` — operationType now sent as integer
- e2e suite: 70/70 pass
- Docs updated in `OTA_INTEGRATION_GUIDE.md`

---

## Blockers Before Google Home Can Go Live

All 5 must be resolved:

### 1. Google Actions Console — nothing set up yet (most critical)
No Smart Home project exists. Need:
- Create project at console.actions.google.com
- Configure OAuth consent screen
- Generate `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`
- Note `GOOGLE_AGENT_ID`
- After deploying, register these URLs in Actions Console:
  - Auth: `https://iot.digilux.co.in/smarthome/google-home/oauth/authorize`
  - Token: `https://iot.digilux.co.in/smarthome/google-home/oauth/token`
  - Fulfillment: `https://iot.digilux.co.in/smarthome/google-home/fulfillment`

### 2. Secrets Manager secret not created
```bash
aws secretsmanager create-secret \
  --name digilux-google-home-oauth \
  --region ap-south-1 \
  --secret-string '{"client_id":"YOUR_CLIENT_ID","client_secret":"YOUR_CLIENT_SECRET"}'
```

### 3. QUERY / EXECUTE stubs (device control API integration needed)

### 4. Flutter team hasn't built Google Home linking UI yet
Guide is ready: `GOOGLE_HOME_INTEGRATION_GUIDE.md`

### 5. Branch `mahesh-google-app-integration` not merged to master

---

## Recommended Next Steps (in order)

```
1. Merge mahesh-google-app-integration → master (PR review recommended)
2. Run: ./infrastructure/deploy_all.sh --alexa        # fix Alexa unlink bug
3. Set up Google Actions Console project              # Blocker 1
4. Create Secrets Manager secret                      # Blocker 2
5. Run: ./infrastructure/deploy_all.sh --google --dry-run
6. Run: ./infrastructure/deploy_all.sh --google
7. Register URLs in Google Actions Console (post-deploy)
8. Flutter team builds linking UI from GOOGLE_HOME_INTEGRATION_GUIDE.md
9. End-to-end test: link → status → unlink
10. Wire QUERY/EXECUTE to device control API          # Blocker 3
```

---

## Open Questions

1. Who owns the Google Cloud project / Actions Console access?
2. What is the Digilux device control API endpoint + auth method? (needed for QUERY/EXECUTE)
3. Flutter team's iOS app Team ID + Bundle ID — needed for Alexa deep link AASA file on iOS.

---

## Test Counts (last run 2026-08-23)

| Suite | Count | Status |
|-------|-------|--------|
| OTA e2e | 70 | All pass |
| Alexa unit | 106 | All pass |
| Google Home unit | 205 | All pass |
