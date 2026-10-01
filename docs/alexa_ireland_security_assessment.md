# Security Assessment — Alexa Voice Command Lambda on AWS eu-west-1 (Ireland)

**Prepared by**: Digilux Engineering Team
**Date**: 2026-10-01
**Classification**: Internal — Management / Technology / Cybersecurity
**Version**: 2.0

---

## 1. Why One Lambda Must Be in Ireland

Amazon routes Alexa Smart Home voice commands based on the customer's marketplace region.
For Indian customers, Amazon's infrastructure **mandates** eu-west-1 (Ireland) as the
destination — this is not a choice, it is Amazon's enforced routing rule.

| Customer Region | Amazon Routes Voice Commands To |
|----------------|--------------------------------|
| India (IN) | **eu-west-1 — Ireland** |
| Europe | eu-west-1 — Ireland |
| North America | us-east-1 — Virginia |
| Far East | us-west-2 — Oregon |

If the Lambda does not exist in eu-west-1, Indian customers' voice commands silently fail —
the device never receives the command.

---

## 2. The Lambda in Ireland — What It Is

**Lambda name**: `alexa` (Smart Home fulfillment)
**Region**: eu-west-1 (Ireland)
**AWS Account**: Honeywell

This is the only AWS resource deployed in Ireland. Every other resource stays in us-east-1.

This Lambda handles three voice command intents:

| Intent | Triggered When Customer Says |
|--------|------------------------------|
| SYNC | "Alexa, discover my devices" |
| QUERY | "Alexa, is the fan on?" |
| EXECUTE | "Alexa, turn on the light" |

---

## 3. Voice Command Flow — Region at Every Hop

```
[INDIA — Customer]
  └─► "Alexa, turn on the light"
         │
         │  Amazon routes to eu-west-1 (mandatory for India)
         │  HTTPS POST — signed by Amazon  (TLS 1.3)
         ▼
[eu-west-1 — Ireland — alexa Lambda]   ← only resource in Ireland
         │
    ┌────┴──────────────────────┐
    │                           │
    ▼                           ▼
[eu-west-1]               [us-east-1 — Virginia]
Secrets Manager            DynamoDB
(AES key, LWA creds)      (user tokens, device map)
                                │
                                ▼
                          [us-east-1 — Virginia]
                           AWS IoT Core
                           (MQTT publish)
                                │
                                │  MQTT over TLS 1.2
                                ▼
                          [INDIA — Physical Device]
                           Executes command
```

### What sends requests TO the Ireland Lambda

| Sender | What It Sends | How |
|--------|--------------|-----|
| **Amazon Alexa Cloud** | Signed HTTPS directive (SYNC / QUERY / EXECUTE) | HTTPS POST to Lambda URL |

No one else can call this Lambda. Amazon is the only caller.

### What the Ireland Lambda calls OUT TO

| Resource | Region | Why |
|----------|--------|-----|
| **Secrets Manager** | eu-west-1 | Retrieve AES-GCM encryption key and LWA credentials |
| **DynamoDB** | us-east-1 | Look up user's device mapping and tokens |
| **IoT Core** | us-east-1 | Publish MQTT command to physical device |

---

## 4. Are These Resources Exposed to the Outside World?

**No. None of these resources are publicly accessible.**

| Resource | Exposed to Internet? | Who Can Access It |
|----------|---------------------|------------------|
| `alexa` Lambda (eu-west-1) | No | Only Amazon Alexa Cloud (enforced by Lambda resource policy) |
| Secrets Manager (eu-west-1) | No | Only the `alexa` Lambda IAM role |
| DynamoDB (us-east-1) | No | Only the `alexa` Lambda IAM role (specific table ARNs only) |
| IoT Core (us-east-1) | No | Only the `alexa` Lambda IAM role (specific topic pattern only) |
| Physical Device connection | No | Device initiates outbound MQTT to IoT Core — no inbound exposure |

---

## 5. How Secure Are These Resources?

### The Lambda (eu-west-1)
- Invoked exclusively by Amazon Alexa Cloud via a signed HTTPS request
- Lambda validates Amazon's signature on every request — spoofed calls are rejected
- No public URL accessible to anyone other than Amazon's Alexa backend
- Code is encrypted at rest (AES-256, AWS-managed)
- AWS engineers cannot access your Lambda code — covered under AWS SOC 2 Type II audit

### Secrets Manager (eu-west-1)
- Stores the AES-GCM encryption key and LWA client credentials
- Accessible only via IAM — only the `alexa` Lambda's execution role has `GetSecretValue`
- No secret is hardcoded in code or environment variables
- All values encrypted at rest (AES-256)

### DynamoDB (us-east-1)
- IAM policy grants access only to the specific tables needed — no wildcard permissions
- The Lambda only reads from these tables during a voice command — no write access except for token refresh
- Not accessible from the public internet — AWS VPC-level isolation

### IoT Core (us-east-1)
- Lambda can only publish to `iot/device/*/request` and `iot/device/*/scene` topics
- Cannot subscribe, cannot read other topics, cannot access other devices' data
- Physical device connects outbound over MQTT/TLS — no inbound port is open on the device

### Data in Transit
Every hop in the flow is encrypted:

| Hop | Encryption |
|-----|-----------|
| Customer → Alexa Cloud | TLS (Amazon-managed) |
| Alexa Cloud → Lambda | TLS 1.3 |
| Lambda → Secrets Manager | TLS 1.2+ (AWS SDK) |
| Lambda → DynamoDB | TLS 1.2+ (AWS SDK) |
| Lambda → IoT Core | TLS 1.2+ (AWS SDK) |
| IoT Core → Device | MQTT over TLS 1.2 |

No data travels unencrypted at any point.

### Device Identity
The device identifier (DUID) sent to Amazon is AES-GCM encrypted. Amazon's Alexa Cloud
never sees the raw device ID — it only sees an opaque encrypted string. The decryption
key never leaves AWS Secrets Manager.

---

## 6. Audit and Visibility

All access to the Ireland Lambda — every invocation, every deployment, every IAM change —
is captured by AWS CloudTrail which is active and multi-region (covers eu-west-1).

Logs are retained in S3 and available for audit at any time.

---

## 7. Open Items Before Security Sign-Off

| # | Item | Risk | Action |
|---|------|------|--------|
| 1 | `alexa` Lambda uses auto-generated IAM role (`alexa-role-zt15wjgn`) | Medium | Replace with purpose-built `digilux-alexa-fulfillment-role` with least-privilege |
| 2 | Alexa request signature validation — confirm it is implemented | Medium | Review Lambda code and confirm |
| 3 | DynamoDB tables use AWS-owned encryption keys, not CMK | Low | Create customer-managed KMS key and apply to token tables |

---

*Document prepared by Digilux Engineering. For questions contact the platform team.*
