# Honeywell Alexa Smart Home — eu-west-1 Deployment Checklist

**Version:** 1.0  
**Date:** 2026-08-14  
**Purpose:** Complete, nothing-missed checklist for deploying the Alexa Smart Home Lambda in eu-west-1 within Honeywell's AWS account  
**Scope:** This document covers ONLY the eu-west-1 Smart Home Lambda. The 6 App-to-App Lambdas (ap-south-1) are covered in HONEYWELL_DEPLOYMENT_GUIDE.md.

---

## Overview — What Is Being Deployed

The Alexa Smart Home Lambda is a single Python 3.12 Lambda function that:
1. Receives directives from Alexa (Discovery, TurnOn, TurnOff, SetBrightness, etc.)
2. Reads device data from DynamoDB (in Honeywell's data region)
3. Reads device metadata from S3
4. Reads the AES encryption key from Secrets Manager (eu-west-1)
5. Publishes MQTT control commands to AWS IoT Core

The Lambda must be in `eu-west-1` because Amazon routes EN-IN Alexa directives to this region.

---

## Phase 1 — Pre-Deployment (Before touching AWS)

### 1.1 Alexa Developer Console Setup

| # | Action | Detail | Owner | Done |
|---|--------|--------|-------|------|
| 1.1.1 | Register Honeywell Amazon Developer Account | Go to developer.amazon.com | Honeywell | ☐ |
| 1.1.2 | Create Alexa Smart Home Skill | Type: Smart Home, Locale: English (IN) | Honeywell | ☐ |
| 1.1.3 | Note the Skill ID | Format: `amzn1.ask.skill.xxxxxxxx-xxxx-...` | Honeywell | ☐ |
| 1.1.4 | Configure Account Linking in Developer Console | Auth URL = Honeywell's `/alexa/startAppToApp` endpoint | Honeywell + Digilux | ☐ |
| 1.1.5 | Note LWA `client_id` and `client_secret` | Shown in Account Linking section after save | Honeywell | ☐ |
| 1.1.6 | Note all three Alexa Redirect URLs | Format: `https://pitangui.amazon.com/api/skill/link/...` | Honeywell | ☐ |

---

## Phase 2 — AWS eu-west-1 Resources

These resources live in `eu-west-1` (the Smart Home Lambda's region).

### 2.1 Secrets Manager — eu-west-1

| # | Resource | Action | Detail | Done |
|---|----------|--------|--------|------|
| 2.1.1 | LWA Secret | Create | Name: e.g. `honeywell/alexa/lwa`<br>Value: `{"client_id":"...","client_secret":"..."}` (from step 1.1.5) | ☐ |
| 2.1.2 | Endpoint Encryption Key Secret | Create | Name: e.g. `honeywell/alexa/endpoint_key`<br>Value: `{"key":"<32-byte AES-256 base64>"}` | ☐ |
| 2.1.3 | Generate AES-256 key | Run: `python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"` | Store output in 2.1.2 | ☐ |
| 2.1.4 | Note both Secret ARNs | Format: `arn:aws:secretsmanager:eu-west-1:<account-id>:secret:...` | Required for Lambda env vars | ☐ |

### 2.2 Lambda Layer — eu-west-1

| # | Resource | Action | Detail | Done |
|---|----------|--------|--------|------|
| 2.2.1 | `pycrypto-py312` Layer | Publish | Upload the layer zip to eu-west-1<br>Compatible runtime: python3.12 | ☐ |
| 2.2.2 | Note Layer ARN | Format: `arn:aws:lambda:eu-west-1:<account-id>:layer:pycrypto-py312:1` | Required for Lambda config | ☐ |

### 2.3 IAM Role — eu-west-1 Smart Home Lambda

The IAM role is global (not region-specific) but must include permissions for eu-west-1 resources.

| # | Permission | Resource ARN Pattern | Done |
|---|-----------|---------------------|------|
| 2.3.1 | `secretsmanager:GetSecretValue` | `arn:aws:secretsmanager:eu-west-1:<account>:secret:honeywell/alexa/lwa*` | ☐ |
| 2.3.2 | `secretsmanager:GetSecretValue` | `arn:aws:secretsmanager:eu-west-1:<account>:secret:honeywell/alexa/endpoint_key*` | ☐ |
| 2.3.3 | `dynamodb:GetItem`, `Query`, `Scan` | `arn:aws:dynamodb:<DATA_REGION>:<account>:table/user_device_details` | ☐ |
| 2.3.4 | `dynamodb:GetItem`, `Query`, `Scan` | `arn:aws:dynamodb:<DATA_REGION>:<account>:table/device_state` | ☐ |
| 2.3.5 | `dynamodb:GetItem`, `Query`, `Scan` | `arn:aws:dynamodb:<DATA_REGION>:<account>:table/digilux_scene_data` (and GSI) | ☐ |
| 2.3.6 | `dynamodb:GetItem`, `Query`, `Scan` | `arn:aws:dynamodb:<DATA_REGION>:<account>:table/digilux_honeywell_user_device_mapping` (and GSI) | ☐ |
| 2.3.7 | `dynamodb:GetItem`, `PutItem`, `UpdateItem` | `arn:aws:dynamodb:<DATA_REGION>:<account>:table/digilux_honeywell_alexa_lwa_tokens` | ☐ |
| 2.3.8 | `s3:GetObject`, `s3:ListBucket` | `arn:aws:s3:::honeywell-metadata-bucket` and `/*` | ☐ |
| 2.3.9 | `iot:Publish` | `arn:aws:iot:<DATA_REGION>:<account>:topic/iot/device/*` | ☐ |
| 2.3.10 | `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `arn:aws:logs:eu-west-1:<account>:*` | ☐ |

### 2.4 Lambda Function — eu-west-1

| # | Config Item | Value | Done |
|---|------------|-------|------|
| 2.4.1 | Function name | `alexa` (or Honeywell's naming convention) | ☐ |
| 2.4.2 | Runtime | `python3.12` | ☐ |
| 2.4.3 | Handler | `lambda_function.lambda_handler` | ☐ |
| 2.4.4 | Memory | `256 MB` | ☐ |
| 2.4.5 | Timeout | `15 seconds` | ☐ |
| 2.4.6 | Layer | `pycrypto-py312` ARN from step 2.2.2 | ☐ |
| 2.4.7 | IAM Role | Role from step 2.3 | ☐ |
| 2.4.8 | Code | Upload deployment zip | ☐ |

### 2.5 Lambda Environment Variables — Complete List

Every variable below must be set. None have safe defaults in Honeywell's account.

| Variable | Value to Set | Source |
|----------|-------------|--------|
| `DATA_REGION` | Honeywell's DynamoDB region (e.g. `ap-south-1`) | Honeywell infra team |
| `USER_DEVICE_DETAILS_TABLE` | Honeywell's table name | Honeywell infra team |
| `DEVICE_STATE_TABLE` | Honeywell's table name | Honeywell infra team |
| `USER_DEVICE_MAPPING_TABLE` | Honeywell's table name | Honeywell infra team |
| `LWA_TOKENS_TABLE` | Honeywell's table name | Honeywell infra team |
| `SCENE_TABLE` | Honeywell's scene table name | Honeywell infra team |
| `METADATA_BUCKET` | Honeywell's S3 bucket name | Honeywell infra team |
| `LWA_SECRET_ARN` | ARN from step 2.1.1 | ☐ |
| `LWA_SECRET_REGION` | `eu-west-1` | Fixed |
| `ENDPOINT_KEY_SECRET_ARN` | ARN from step 2.1.2 | ☐ |
| `ENDPOINT_KEY_REGION` | `eu-west-1` | Fixed |
| `ALEXA_GATEWAY_URL` | `https://api.eu.amazonalexa.com/v3/events` | Fixed — EU endpoint |
| `COGNITO_USER_POOL_ID` | Honeywell's Cognito pool ID | Honeywell infra team |
| `COGNITO_REGION` | Honeywell's Cognito region | Honeywell infra team |
| `IOT_DATA_ENDPOINT` | Honeywell's IoT Core endpoint URL | AWS Console → IoT Core → Settings |
| `LOG_LEVEL` | `INFO` (use `DEBUG` during initial testing only) | ☐ |

### 2.6 Lambda Resource Policy (Alexa Permission)

This step is mandatory. Without it, Alexa's servers cannot invoke the Lambda.

| # | Action | Command / Detail | Done |
|---|--------|-----------------|------|
| 2.6.1 | Add resource policy | `aws lambda add-permission --function-name alexa --region eu-west-1 --statement-id alexa-smart-home --action lambda:InvokeFunction --principal alexa-connectedhome.amazon.com --event-source-token <SKILL_ID>` | ☐ |
| 2.6.2 | Verify policy | `aws lambda get-policy --function-name alexa --region eu-west-1` — confirm principal is `alexa-connectedhome.amazon.com` and EventSourceToken matches Skill ID | ☐ |

---

## Phase 3 — Alexa Developer Console — Connect Skill to Lambda

| # | Action | Detail | Done |
|---|--------|--------|------|
| 3.1 | Set Smart Home endpoint | Developer Console → Skill → Smart Home → Default Endpoint: paste Lambda ARN from step 2.4 | ☐ |
| 3.2 | Save and build | Click Save in Developer Console | ☐ |

---

## Phase 4 — Code Fixes (Apply Before Deployment)

Two known bugs in the current codebase will cause failures in Honeywell's account if not fixed first. Full details in HONEYWELL_DEPLOYMENT_GUIDE.md Section 3.

| # | Bug | File | Fix | Done |
|---|-----|------|-----|------|
| 4.1 | `eu-west-1` hardcoded in `_get_lwa_secret()` | `lambdas/alexa_link_status/lambda_function.py` | Add `LWA_SECRET_REGION` env var, update code to use it | ☐ |
| 4.2 | `LWA_SECRET_ARN` default is Digilux's ARN | `lambdas/alexa_link_status/lambda_function.py` | Remove hardcoded default, add to `_REQUIRED_VARS` | ☐ |
| 4.3 | `deploy.sh` has hardcoded regions | `infrastructure/deploy.sh` | Parameterise region variables | ☐ |

---

## Phase 5 — Verification Tests

Run these in order. Do not skip any.

| # | Test | Expected Result | Done |
|---|------|----------------|------|
| 5.1 | Link Alexa account from Honeywell app | `GET /alexa/status` returns `{"linked": true}` | ☐ |
| 5.2 | Lambda invoked for AcceptGrant | CloudWatch (eu-west-1) shows `AcceptGrant` log with correct `userId` | ☐ |
| 5.3 | "discover my devices" (Alexa simulator, EN-IN) | Lambda invoked for `Discover`, CloudWatch shows device count > 0 | ☐ |
| 5.4 | "turn on [device]" (Alexa simulator) | CloudWatch shows `TurnOn` log + IoT MQTT `{"onOff": 1}` published | ☐ |
| 5.5 | "turn off [device]" (Alexa simulator) | CloudWatch shows `TurnOff` log + IoT MQTT `{"onOff": 0}` published | ☐ |
| 5.6 | Disable skill from Alexa app | `GET /alexa/status` returns `{"linked": false}` (auto-detected via LWA token validation) | ☐ |
| 5.7 | Unlink from Honeywell app | `DELETE /alexa/unlink` returns 200, status returns `{"linked": false}` | ☐ |
| 5.8 | Echo device test (if available in India) | Physical Echo device says "Alexa, discover my devices" → devices found | ☐ |
| 5.9 | Audit logs present | CloudWatch (eu-west-1) shows `[AUDIT]` entries for all actions | ☐ |

---

## Phase 6 — Nothing-Missed Cross-Check

Read each row and confirm before go-live sign-off.

| # | Question | Answer Expected | Confirmed |
|---|----------|----------------|-----------|
| 6.1 | Is `ALEXA_GATEWAY_URL` set to `api.eu.amazonalexa.com` (not `api.amazonalexa.com`)? | Yes | ☐ |
| 6.2 | Is the Skill endpoint in Developer Console set to the eu-west-1 Lambda ARN? | Yes | ☐ |
| 6.3 | Does the Lambda resource policy `EventSourceToken` match the Skill ID exactly? | Yes | ☐ |
| 6.4 | Are both Secrets Manager secrets in eu-west-1? | Yes | ☐ |
| 6.5 | Does the IAM role allow `secretsmanager:GetSecretValue` on eu-west-1 ARNs? | Yes | ☐ |
| 6.6 | Is `LWA_SECRET_REGION` set to `eu-west-1`? | Yes | ☐ |
| 6.7 | Is `ENDPOINT_KEY_REGION` set to `eu-west-1`? | Yes | ☐ |
| 6.8 | Does the AES endpoint key in Secrets Manager exactly 32 bytes (base64 encoded)? | Yes | ☐ |
| 6.9 | Are all DynamoDB table names set to Honeywell's actual table names (not Digilux's)? | Yes | ☐ |
| 6.10 | Is `COGNITO_USER_POOL_ID` set to Honeywell's pool (not Digilux's `ap-south-1_h1o8s7257`)? | Yes | ☐ |
| 6.11 | Is `IOT_DATA_ENDPOINT` set to Honeywell's IoT Core endpoint? | Yes | ☐ |
| 6.12 | Is `LOG_LEVEL` set to `INFO` (not `DEBUG`) for production? | Yes | ☐ |
| 6.13 | Have all 3 code bugs (Section 4) been fixed and deployed? | Yes | ☐ |
| 6.14 | Has the pycrypto-py312 Layer been published in eu-west-1 and attached to the Lambda? | Yes | ☐ |
| 6.15 | Has 5.3 (Discovery from EN-IN simulator) been verified with a real log screenshot? | Yes | ☐ |

---

## Sign-off

All Phase 5 tests must pass and all Phase 6 answers must be confirmed before go-live.

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Engineering Lead | | | |
| Cloud Architect | | | |
| QA / Verification | | | |

