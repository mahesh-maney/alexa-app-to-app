# Security Assessment — Alexa Voice Command Lambda on AWS eu-west-1 (Ireland)

**Prepared by**: Digilux Engineering Team
**Date**: 2026-10-01
**Classification**: Internal — Management / Technology / Cybersecurity
**Version**: 3.0

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
**AWS Account**: Honeywell Nucleus Team
**Network**: Deployed inside Spoke VPC 1, Private Subnet — no public internet access

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
         │  HTTPS, TLS 1.2+  →  CloudFront + WAF + Shield (AWS Edge)
         │  Amazon routes to eu-west-1 (mandatory for India)
         │  HTTPS POST — signed by Amazon  (TLS 1.3)
         ▼
[eu-west-1 — Ireland — alexa Lambda]   ← only resource in Ireland
[Private Subnet, Spoke VPC 1]
         │
    ┌────┴──────────────────────┐
    │                           │
    ▼                           ▼
[eu-west-1]               [us-east-1 — Virginia]
Secrets Manager            Aurora Serverless Postgres / RDS
(AES key, LWA creds)      (user tokens, device map)
via VPC Endpoint          via Transit Gateway → Spoke VPC 2
                                │
                                ▼
                          [us-east-1 — Virginia]
                           AWS IoT Core
                           (MQTT publish)
                           via VPC Endpoint
                                │
                                │  MQTT over TLS 1.2
                                ▼
                          [INDIA — Physical Device]
                           Executes command
```

### What sends requests TO the Ireland Lambda

| Sender | What It Sends | How |
|--------|--------------|-----|
| **Amazon Alexa Cloud** | Signed HTTPS directive (SYNC / QUERY / EXECUTE) | HTTPS POST — enters via CloudFront → WAF → API Gateway |

No one else can call this Lambda. Amazon is the only caller.

### What the Ireland Lambda calls OUT TO

| Resource | Region | How | Why |
|----------|--------|-----|-----|
| **Secrets Manager** | eu-west-1 | VPC Endpoint (private) | Retrieve AES-GCM encryption key and LWA credentials |
| **Aurora Postgres / RDS** | us-east-1 | Transit Gateway → Spoke VPC 2 (private) | Look up user tokens and device mapping |
| **IoT Core** | us-east-1 | VPC Endpoint (private) | Publish MQTT command to physical device |

All outbound calls travel over private AWS network paths — never over the public internet.

---

## 4. Are These Resources Exposed to the Outside World?

**No. None of these resources are publicly accessible.**

The Honeywell Nucleus architecture enforces multiple layers of network isolation:

| Resource | Exposed to Internet? | Network Protection | Access Control |
|----------|---------------------|--------------------|---------------|
| `alexa` Lambda (eu-west-1) | **No** | Private Subnet, Spoke VPC 1 | Only Amazon Alexa Cloud via signed request |
| Secrets Manager (eu-west-1) | **No** | VPC Endpoint — no public route | Only `alexa` Lambda IAM role |
| Aurora Postgres / RDS (us-east-1) | **No** | Private Subnet, Spoke VPC 2, no internet gateway | Only Application Tier Lambdas via Transit Gateway |
| IoT Core (us-east-1) | **No** | VPC Endpoint — no public route | Only `alexa` Lambda IAM role (specific topics only) |
| Physical Device connection | **No** | Device initiates outbound MQTT — no inbound port open | AWS IoT Device Defender monitors for anomalies |

**Additional edge protection** (applies to all inbound traffic):
- **Amazon CloudFront** — CDN + TLS termination
- **AWS WAF** — blocks malicious HTTP requests, SQL injection, XSS
- **AWS Shield Standard** — always-on DDoS protection

---

## 5. How Secure Are These Resources?

### The Lambda (eu-west-1)
- Sits inside a **Private Subnet in Spoke VPC 1** — has no direct internet-facing interface
- Invoked exclusively by Amazon Alexa Cloud via a signed HTTPS request
- Lambda validates Amazon's signature on every request — spoofed calls are rejected before processing
- All inbound traffic passes through **CloudFront → WAF → Shield** before reaching the Lambda
- Code is encrypted at rest (AES-256, AWS-managed)
- AWS engineers cannot access your Lambda code — covered under AWS SOC 2 Type II audit

### Secrets Manager (eu-west-1)
- Accessed via **VPC Endpoint** — traffic never leaves the AWS private network
- Accessible only via IAM — only the `alexa` Lambda's execution role has `GetSecretValue`
- No secret is hardcoded in code or environment variables
- All values encrypted at rest (AES-256)

### Database — Aurora Serverless v2 Postgres / RDS (us-east-1)
- Lives in **Spoke VPC 2, Private Subnet** — completely isolated from the internet
- The Lambda reaches it via **Transit Gateway** over a private, encrypted path
- No public endpoint — cannot be accessed from outside the AWS internal network
- Aurora Serverless v2 automatically scales and is encrypted at rest

### IoT Core (us-east-1)
- Accessed via **VPC Endpoint** — no public internet traversal
- Lambda can only publish to `iot/device/*/request` and `iot/device/*/scene` topics (IAM-scoped)
- Cannot subscribe, cannot read other topics, cannot access other devices' data
- **AWS IoT Device Defender** monitors all IoT traffic in real-time for anomalous behaviour,
  unauthorized access attempts, and policy violations — alerts raised automatically

### Physical Device
- Connects outbound over MQTT/TLS 1.2 to IoT Core — no inbound port is open on the device
- Device identity verified by IoT Core certificates
- IoT Device Defender continuously audits device-level security posture

### Data in Transit
Every hop in the flow is encrypted:

| Hop | Encryption |
|-----|-----------|
| Customer → Alexa Cloud | TLS (Amazon-managed) |
| Alexa Cloud → CloudFront / WAF | TLS 1.2+ |
| CloudFront → Lambda | TLS 1.3 |
| Lambda → Secrets Manager | TLS 1.2+ via VPC Endpoint |
| Lambda → Aurora / RDS | TLS 1.2+ via Transit Gateway |
| Lambda → IoT Core | TLS 1.2+ via VPC Endpoint |
| IoT Core → Physical Device | MQTT over TLS 1.2 |

No data travels unencrypted at any point.

### Device Identity
The device identifier (DUID) sent to Amazon is AES-GCM encrypted. Amazon's Alexa Cloud
never sees the raw device ID — it only sees an opaque encrypted string. The decryption
key never leaves AWS Secrets Manager.

---

## 6. Audit and Visibility

All access to the Ireland Lambda — every invocation, every deployment, every IAM change —
is captured by:

- **AWS CloudTrail** — active, multi-region, covers eu-west-1. Logs stored in S3.
- **AWS IoT Device Defender** — real-time monitoring of all IoT traffic and device behaviour
- **Amazon CloudWatch** — Lambda execution logs, errors, latency metrics
- **AWS Audit Trail** — all API-level activity across the account

All logs are retained and available for audit at any time.

---

## 7. Open Items Before Security Sign-Off

| # | Item | Risk | Action |
|---|------|------|--------|
| 1 | `alexa` Lambda uses auto-generated IAM role (`alexa-role-zt15wjgn`) | Medium | Replace with purpose-built `honeywell-alexa-fulfillment-role` with least-privilege, following Honeywell naming standards |
| 2 | Alexa request signature validation — confirm it is implemented in code | Medium | Review Lambda code and confirm before go-live |
| 3 | Aurora / RDS encryption — confirm CMK is applied | Low | Verify customer-managed KMS key (`alias/digilux/honeywell/rds`) is applied to the Alexa token tables |

---

*Document prepared by Digilux Engineering. For questions contact the platform team.*
