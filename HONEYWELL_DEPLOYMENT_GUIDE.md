# Alexa App-to-App — Honeywell Deployment Guide

**Prepared by:** Digilux Engineering
**Date:** 2026-08-09
**Applies to:** Migration of Alexa App-to-App account linking backend from Digilux AWS account to Honeywell AWS account

---

## Overview

The Alexa App-to-App integration consists of:
- 6 AWS Lambda functions (ap-south-1) handling the linking flow, status, unlink, and skill events
- 1 AWS Lambda function (eu-west-1) handling the Alexa Smart Home skill protocol
- API Gateway with custom domain
- DynamoDB tables, KMS keys, IAM roles, Secrets Manager, Cognito

This document outlines everything that must be created, recreated, or changed when deploying to Honeywell's AWS account.

---

## 1. Things That Belong to Digilux — Honeywell Must Create Their Own

These cannot be transferred or copied. Honeywell needs to set each one up from scratch.

### Amazon / Alexa Developer Account

- The Alexa skill is registered under Digilux's Amazon Developer account. Honeywell needs their **own Amazon Developer account** and a new **Alexa Smart Home skill** registration.
- The **LWA app** (`client_id` / `client_secret` stored in AWS Secrets Manager) is tied to Digilux's Alexa skill. Honeywell receives their own `client_id` and `client_secret` when they configure Account Linking on their registered skill.
- The **Skill ID** (`ALEXA_SKILL_ID` Lambda env var) will be different for Honeywell's skill.

### Domain

`iot.digilux.co.in` is Digilux's domain. Every URL in the system flows from it:

| What uses the domain | Detail |
|---|---|
| OAuth Redirect URI | `https://iot.digilux.co.in/alexa/callback` — configured in Alexa Account Linking |
| Android App Links | `assetlinks.json` served from this domain; contains app signing cert SHA256 fingerprints |
| iOS App Site Association | AASA served from this domain |
| API Gateway custom domain | All Lambda API endpoints sit behind this domain |

Honeywell needs:
- Their own domain (e.g. `iot.honeywell.com`)
- ACM certificate for that domain in `ap-south-1` (for API Gateway) and `us-east-1` (if using CloudFront)
- API Gateway custom domain mapping
- DNS records pointing the domain to API Gateway

### Flutter / Mobile App

Honeywell's app will have a different:
- **Bundle ID / App Package** — e.g. `com.honeywell.smarthome` instead of `com.digiluxai.smarthomepro`
- **Signing certificate SHA256 fingerprints** — needed for Android App Links (`assetlinks.json`)
- **`APP_SCHEME`** — the custom URL scheme used for iOS deep links (e.g. `honeywell` instead of `digilux`)

All three feed into Android App Links, iOS deep links, and the `alexa_callback` Lambda.

---

## 2. AWS Infrastructure to Recreate in Honeywell's Account

All of the following must be created fresh in Honeywell's AWS account.

### DynamoDB Tables

Three tables required. Use the same schema, GSIs, and SSE configuration as Digilux's tables:

| Table | Primary Key | Sort Key | GSIs | Notes |
|---|---|---|---|---|
| `alexa_app_linking_sessions` | `state` (String) | — | — | TTL field: `ttl`. Used for OAuth PKCE sessions. |
| `digilux_honeywell_alexa_lwa_tokens` | `userId` (String) | — | `amazonUserId-index` (PK: `amazonUserId`), `alexaCustomerId-index` (PK: `alexaCustomerId`) | Stores encrypted LWA tokens. SSE with KMS. |
| `digilux_honeywell_user_device_mapping` | `userId` (String) | `siteId` (String) | — | Stores `alexaLinked`, `alexaLinkedAt` per site. SSE with KMS. |

**Note:** Table names are configurable via Lambda env vars — Honeywell can rename them.

### KMS Keys

Two separate KMS keys are required. They serve different purposes and **both** must be present:

| Key | Purpose | Which Lambdas use it |
|---|---|---|
| **App-level field encryption key** | Encrypts `accessToken` and `refreshToken` field values before storing in DDB | `alexa_complete_app_to_app` (encrypt), `alexa_unlink` (decrypt), `alexa_link_status` (decrypt) |
| **DynamoDB SSE key** | Table-level server-side encryption on `lwa_tokens` and `user_device_mapping` tables | All Lambdas that read/write those tables |

Both key ARNs must be updated in:
1. Lambda environment variables (`KMS_KEY_ARN`)
2. IAM role policies for each Lambda execution role

### IAM Lambda Execution Roles

Six roles must be recreated. Each role needs:
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` on the account's CloudWatch
- `xray:PutTraceSegments`, `xray:PutTelemetryRecords`
- DynamoDB permissions scoped to the specific tables in Honeywell's account
- KMS `kms:Decrypt` / `kms:DescribeKey` on both KMS keys (where applicable)
- Secrets Manager `secretsmanager:GetSecretValue` on the LWA secret (where applicable)

| Role | Lambda | Key DynamoDB Permissions |
|---|---|---|
| `*-alexa-start-role` | `alexa_start_app_to_app` | `sessions`: GetItem + PutItem; KMS encrypt |
| `*-alexa-complete-role` | `alexa_complete_app_to_app` | `sessions`: GetItem + UpdateItem; `tokens`: PutItem; `user_device_mapping`: UpdateItem; LWA secret; KMS encrypt |
| `*-alexa-callback-role` | `alexa_callback` | Logs only |
| `*-alexa-unlink-role` | `alexa_unlink` | `tokens`: GetItem + DeleteItem; `user_device_mapping`: UpdateItem; LWA secret; KMS decrypt |
| `*-alexa-status-role` | `alexa_link_status` | `tokens`: GetItem + UpdateItem + DeleteItem; `user_device_mapping`: GetItem + UpdateItem; `user_device_details`: GetItem; LWA secret; KMS decrypt on **both** keys |
| `*-alexa-skill-events-role` | `alexa_skill_events` | `tokens`: Query + DeleteItem + GSI access; `user_device_mapping`: Query + UpdateItem; KMS decrypt |

### API Gateway

- Create a new REST API with the same routes as Digilux's gateway
- Attach a Cognito authorizer pointing to Honeywell's Cognito User Pool
- Create a custom domain using Honeywell's ACM certificate
- Map the custom domain to the deployment stage

### Cognito

Honeywell already has their own Cognito User Pool for their users. No new pool needed — just update these two Lambda env vars:
- `COGNITO_USER_POOL_ID`
- `COGNITO_REGION`

The Cognito authorizer on API Gateway must also be updated to point to Honeywell's pool.

### Secrets Manager

Create a new secret in Honeywell's AWS account containing:
```json
{
  "client_id": "<Honeywell Alexa skill LWA client_id>",
  "client_secret": "<Honeywell Alexa skill LWA client_secret>"
}
```

These values come from the Alexa Developer Console → Account Linking section of Honeywell's registered skill.

The secret can be stored in any region. Update `LWA_SECRET_ARN` and `LWA_SECRET_REGION` Lambda env vars accordingly.

### eu-west-1 Smart Home Lambda

The Alexa Smart Home skill protocol requires a Lambda in an Amazon-supported region. The region is determined by the skill's target locale — **this is dictated by Amazon, not a free choice**:

| Skill Locale | Required Lambda Region |
|---|---|
| EN-US, EN-CA | `us-east-1` |
| EN-GB, EN-IN | `eu-west-1` |
| JA-JP | `ap-northeast-1` |

Honeywell must:
1. Deploy the Smart Home Lambda to the correct region in their AWS account
2. Update the Alexa Developer Console skill endpoint to point to the new Lambda ARN
3. Grant `lambda:InvokeFunction` permission to Alexa's service principal (`alexa-connectedhome.amazon.com`)

The Smart Home Lambda must be able to reach DynamoDB in Honeywell's chosen data region. Update `DATA_REGION` env var on this Lambda accordingly.

---

## 3. Code Changes Required Before Honeywell Deployment

The following bugs exist in the current codebase. They do not affect Digilux's deployment (hardcoded values happen to be correct for Digilux) but **will break Honeywell's deployment** if not fixed.

> **IMPORTANT for Digilux engineers:** Do not fix these in isolation. Follow the safe sequence below for each bug to avoid breaking the live Digilux deployment.

### Bug 1: `eu-west-1` hardcoded in `alexa_link_status`

**File:** `lambdas/alexa_link_status/lambda_function.py` — `_get_lwa_secret()` function

```python
# Current (broken for other accounts)
sm = boto3.client("secretsmanager", region_name="eu-west-1")

# Fix: use env var, same pattern as alexa_complete_app_to_app and alexa_unlink
_LWA_SECRET_REGION = os.environ.get("LWA_SECRET_REGION", "eu-west-1")
sm = boto3.client("secretsmanager", region_name=_LWA_SECRET_REGION)
```

**Safe fix sequence:**
1. Add `LWA_SECRET_REGION=eu-west-1` to the Lambda env vars (preserves existing behaviour)
2. Verify status endpoint still works
3. Then update the code to use the env var
4. Redeploy and verify again

### Bug 2: `LWA_SECRET_ARN` default contains Digilux's AWS account ID

**File:** `lambdas/alexa_link_status/lambda_function.py`

```python
# Current (will silently use Digilux's ARN in Honeywell's account → AccessDeniedException)
_LWA_SECRET_ARN = os.environ.get("LWA_SECRET_ARN",
    "arn:aws:secretsmanager:eu-west-1:986906626244:secret:digilux/alexa/lwa-7RDOUm")

# Fix: no default, add to _REQUIRED_VARS so startup fails loudly if not set
_LWA_SECRET_ARN = os.environ.get("LWA_SECRET_ARN", "")
_REQUIRED_VARS = {
    "DATA_REGION":      _REGION,
    "LWA_TOKENS_TABLE": _TOKENS_TABLE,
    "LWA_SECRET_ARN":   _LWA_SECRET_ARN,   # add this
}
```

**Safe fix sequence:**
1. Add `LWA_SECRET_ARN=arn:aws:secretsmanager:eu-west-1:986906626244:secret:digilux/alexa/lwa-7RDOUm` to Lambda env vars
2. Verify status endpoint still works
3. Then remove the hardcoded default from code and add to `_REQUIRED_VARS`
4. Redeploy and verify again

### Bug 3: `deploy.sh` has hardcoded region values

**File:** `infrastructure/deploy.sh`

```bash
# Current (hardcoded)
REGION="ap-south-1"
LWA_SECRET_REGION=eu-west-1   # hardcoded inside Lambda env vars block

# Fix: accept as script arguments or clearly documented variables at the top
REGION="${1:-ap-south-1}"
LWA_SECRET_REGION="${2:-eu-west-1}"
```

---

## 4. Architecture Decision — Region Strategy

Current Digilux architecture: **Smart Home Lambda (eu-west-1) → DynamoDB (ap-south-1)**

This cross-region pattern works within a single AWS account. For Honeywell, the region decision has the following constraints:

- The **Smart Home Lambda region is fixed by Amazon** based on the skill's target market (see table in Section 2)
- The **DynamoDB region is fully flexible** — it is controlled by the `DATA_REGION` env var on all Lambdas
- Placing DynamoDB in the **same region as the Smart Home Lambda** eliminates cross-region latency (~10–80ms depending on regions) and simplifies IAM (no cross-region policies)
- The **6 app-to-app Lambdas** (start, complete, callback, unlink, status, skill_events) can be in any region — align with Honeywell's existing infrastructure or with the DynamoDB region

**Recommended approach for Honeywell:**
- Determine Alexa skill locale → that sets the Smart Home Lambda region
- Put DynamoDB in the same region
- Put all 6 app-to-app Lambdas in the same region

---

## 5. Lambda Environment Variables Reference

All configuration is via env vars. The table below shows every variable, its current Digilux value, and what Honeywell must change.

| Env Var | Digilux Value | Honeywell Action |
|---|---|---|
| `DATA_REGION` | `ap-south-1` | Set to Honeywell's DynamoDB region |
| `LWA_TOKENS_TABLE` | `digilux_honeywell_alexa_lwa_tokens` | Set to Honeywell's table name |
| `USER_DEVICE_MAPPING_TABLE` | `digilux_honeywell_user_device_mapping` | Set to Honeywell's table name |
| `USER_DEVICE_DETAILS_TABLE` | `user_device_details` | Set to Honeywell's table name |
| `KMS_KEY_ARN` | Digilux key ARN | Set to Honeywell's app-level encryption key ARN |
| `LWA_SECRET_ARN` | *(hardcoded default — Bug 2 above)* | Set to Honeywell's Secrets Manager secret ARN |
| `LWA_SECRET_REGION` | *(hardcoded default — Bug 1 above)* | Set to region where Honeywell stores the LWA secret |
| `ALEXA_SKILL_ID` | Digilux skill ID | Set to Honeywell's Alexa skill ID |
| `REDIRECT_URI` | `https://iot.digilux.co.in/alexa/callback` | Set to Honeywell's domain callback URL |
| `ALLOWED_REDIRECT_HOSTS` | `iot.digilux.co.in` | Set to Honeywell's domain |
| `APP_SCHEME` | `digilux` | Set to Honeywell's app URL scheme |
| `COGNITO_USER_POOL_ID` | `ap-south-1_h1o8s7257` | Set to Honeywell's Cognito pool ID |
| `COGNITO_REGION` | `ap-south-1` | Set to Honeywell's Cognito region |
| `LOG_LEVEL` | `DEBUG` | Set to `INFO` in production |

---

## 6. Android App Links (`assetlinks.json`)

The file at `https://<domain>/.well-known/assetlinks.json` must be served from Honeywell's domain and contain Honeywell's app's signing certificate SHA256 fingerprints.

```json
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "<honeywell_app_package_name>",
    "sha256_cert_fingerprints": [
      "<debug_signing_cert_sha256>",
      "<production_signing_cert_sha256>"
    ]
  }
}]
```

To get fingerprints:
```bash
# From a keystore
keytool -list -v -keystore <keystore.jks> -alias <alias>

# From an APK
apksigner verify --print-certs <app.apk>
```

This file must be accessible over HTTPS at `https://<honeywell-domain>/.well-known/assetlinks.json` with `Content-Type: application/json`.

---

## 7. iOS Apple App Site Association (AASA)

The file at `https://<domain>/.well-known/apple-app-site-association` must contain Honeywell's Apple Team ID and Bundle ID:

```json
{
  "applinks": {
    "apps": [],
    "details": [{
      "appID": "<APPLE_TEAM_ID>.<BUNDLE_ID>",
      "paths": ["/alexa/callback"]
    }]
  }
}
```

Honeywell needs to provide their Apple Team ID and iOS app Bundle ID.

---

## 8. Checklist — Honeywell Deployment

Use this as a sign-off checklist before go-live.

**Amazon / Alexa Setup**
- [ ] Amazon Developer account created for Honeywell
- [ ] Alexa Smart Home skill registered
- [ ] Account Linking configured in Alexa Developer Console (redirect URI, LWA app)
- [ ] `client_id` and `client_secret` obtained from Account Linking config
- [ ] Skill ID noted

**AWS Infrastructure**
- [ ] DynamoDB: 3 tables created with correct schema and GSIs
- [ ] KMS: 2 keys created (app-level encryption + DynamoDB SSE)
- [ ] DynamoDB tables updated to use SSE KMS keys
- [ ] Secrets Manager: LWA secret created with `client_id` and `client_secret`
- [ ] IAM: 6 Lambda execution roles created with correct permissions
- [ ] API Gateway: created, routes configured, Cognito authorizer attached
- [ ] ACM certificate: issued for Honeywell's domain
- [ ] API Gateway custom domain: configured and mapped
- [ ] DNS: domain pointing to API Gateway

**Lambda Deployments**
- [ ] 6 app-to-app Lambdas deployed with all env vars updated (see Section 5 table)
- [ ] Smart Home Lambda deployed to correct region in Honeywell's account
- [ ] Smart Home Lambda: Alexa service principal granted `lambda:InvokeFunction`
- [ ] Alexa Developer Console skill endpoint updated to new Smart Home Lambda ARN

**Code Fixes Applied**
- [ ] Bug 1 fixed: `LWA_SECRET_REGION` env var set and code updated
- [ ] Bug 2 fixed: `LWA_SECRET_ARN` env var set and hardcoded default removed
- [ ] Bug 3 fixed: `deploy.sh` parameterised for region

**Mobile App**
- [ ] `assetlinks.json` with Honeywell's app fingerprints served from Honeywell's domain
- [ ] Apple App Site Association with Honeywell's Team ID and Bundle ID served from domain
- [ ] Flutter app built with Honeywell's bundle ID, `APP_SCHEME`, and domain config

**Verification**
- [ ] End-to-end test: Link Alexa account from Honeywell's app → status returns `linked: true`
- [ ] End-to-end test: Disable skill from Alexa app → status returns `linked: false` (auto-detected)
- [ ] End-to-end test: Unlink from app → status returns `linked: false`
- [ ] Audit logs (`[AUDIT]` prefix) visible in CloudWatch for all actions
