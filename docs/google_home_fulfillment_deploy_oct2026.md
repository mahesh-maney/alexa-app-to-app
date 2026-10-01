# Google Home Fulfillment Lambda — Production Deploy (Oct 01, 2026)

**Author**: Mahesh Maney
**Date**: 2026-10-01
**Branch**: `mahesh-google-app-integration`
**AWS Account**: Digilux (`986906626244`) — `ap-south-1`

---

## Overview

This document records the task of committing Mrinal's original production-ready Google Home
fulfillment Lambda (`google_home`) to version control and deploying it to the Digilux AWS account.

---

## Background

The `google_home` Lambda existed on AWS (last modified 2026-09-17) but its source code was never
committed to the `app-to-app` GitHub repository. The code was recovered from a local copy at
`/Users/maheshmaney/maney/digilux/google_home_lambda/google_home/lambda_function.py`.

The existing `lambdas/google_home_fulfillment/lambda_function.py` in the repo was a stub
implementation (EXECUTE was not functional). Mrinal's original code is the full production version.

---

## What Was Done

### 1. Created Orphan Branch `mrinal_google_home_initial_code`

A new **orphan branch** (no parent branch, no commit history) was created in the
`mahesh-maney/alexa-app-to-app` GitHub repository specifically to preserve Mrinal's original code
as a standalone reference.

```bash
git checkout --orphan mrinal_google_home_initial_code
git rm -rf --cached .
# copied lambda_function.py into lambdas/google_home/
git add lambdas/google_home/lambda_function.py
git commit -m "feat(google-home): initial production Google Home fulfillment Lambda"
git push -u origin mrinal_google_home_initial_code
```

- Root commit: `00f1ea0`
- File committed: `lambdas/google_home/lambda_function.py` (874 lines)
- Branch is completely independent — no shared history with `main` or any other branch

### 2. Deployed to AWS Lambda (`google_home`)

Only the `google_home` Lambda was updated. No other Lambda was touched.

```bash
# Zip the single file
zip /tmp/google_home_deploy.zip lambda_function.py

# Deploy
aws lambda update-function-code \
  --function-name google_home \
  --region ap-south-1 \
  --zip-file fileb:///tmp/google_home_deploy.zip
```

**Result**: Lambda updated at `2026-10-01T07:37:09Z`, code size 10,099 bytes.

### 3. Published Version Snapshot

Version 2 was published immediately after deploy for rollback safety.

```bash
aws lambda publish-version \
  --function-name google_home \
  --region ap-south-1 \
  --description "Mrinal initial production code - Oct 01 2026"
```

---

## Lambda Version History (`google_home`)

| Version | Code Size | Description                           | Last Modified       |
|---------|-----------|---------------------------------------|---------------------|
| 1       | 4,057 B   | snapshot-2026-09-30 (old stub code)   | 2026-09-17          |
| 2       | 10,099 B  | Mrinal initial production code        | 2026-10-01 (active) |
| $LATEST | 10,099 B  | Same as Version 2                     | 2026-10-01          |

To roll back to the previous code: update the Lambda alias or re-deploy Version 1.

---

## What the Deployed Lambda Does

Mrinal's `google_home` Lambda is the fulfillment endpoint for the Google Home Cloud-to-Cloud
integration. It handles all four Google Home intents:

| Intent       | Description                                                                 |
|--------------|-----------------------------------------------------------------------------|
| `SYNC`       | Returns all devices for the user from `user_device_details` DDB + S3 `endpoints.json` |
| `QUERY`      | Reads live device state from `device_state` DynamoDB table                  |
| `EXECUTE`    | Publishes commands to `iot/device/{duid}/request` MQTT topic                |
| `DISCONNECT` | Removes the user's OAuth tokens from `google_home_tokens` DynamoDB table    |

### Key Implementation Details

- **Auth**: Cognito JWT validation (`_decode_jwt_sub`) — not DynamoDB token lookup
- **Encryption**: AES-GCM endpoint ID encryption (shared key from AWS Secrets Manager)
- **Device types supported**: `LIGHT`, `FAN`, `BLINDS`, `SWITCH`, `SCENE`
- **Scene activation**: Publishes to `iot/device/{duid}/scene` MQTT topic
- **S3 endpoints**: Device trait/capability definitions loaded from `endpoints.json` in S3

---

## AWS Resources Affected

| Resource         | Name / ARN                                                                 |
|------------------|----------------------------------------------------------------------------|
| Lambda function  | `google_home` (ap-south-1)                                                 |
| IAM Role         | `arn:aws:iam::986906626244:role/digilux-google-home-role`                  |
| DynamoDB (read)  | `google_home_tokens`, `user_device_details`, `device_state`                |
| DynamoDB (write) | `google_home_tokens` (DISCONNECT)                                          |
| Secrets Manager  | `digilux-google-home-oauth-tLYHCO` (AES key + OAuth secret)               |
| IoT Core (pub)   | `iot/device/{duid}/request`, `iot/device/{duid}/scene`                     |

---

## Rollback Instructions

```bash
# Re-deploy Version 1 (old stub code) if needed
aws lambda update-function-code \
  --function-name google_home \
  --region ap-south-1 \
  --zip-file fileb:///tmp/google_home_v1.zip   # retrieve from Version 1 first

# Or point $LATEST back to Version 1 via alias
aws lambda update-alias \
  --function-name google_home \
  --name live \
  --function-version 1 \
  --region ap-south-1
```
