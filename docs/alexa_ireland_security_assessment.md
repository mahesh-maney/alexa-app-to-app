# Security Assessment — Alexa Smart Home Lambda Deployment on AWS eu-west-1 (Ireland)

**Prepared by**: Digilux Engineering Team
**Date**: 2026-10-01
**Classification**: Internal — Management / Technology / Cybersecurity
**Version**: 1.0

---

## 1. Executive Summary

Digilux has developed an Alexa Smart Home skill and App-to-App account linking feature. For
Indian customers to use Alexa voice commands with Digilux devices, Amazon mandates that the
Smart Home skill Lambda function must be deployed in **AWS eu-west-1 (Ireland)**. This is an
Amazon infrastructure requirement — not a Digilux architectural choice.

The `alexa` Smart Home fulfillment Lambda shall be deployed in eu-west-1.
This document assesses the security posture of that deployment for management, technology, and
cybersecurity review.

**Bottom line**: The deployment follows AWS security best practices. All sensitive credentials
are in AWS Secrets Manager. Audit logging is active. The architecture is stateless in Ireland —
all customer data stays in US (us-east-1). The primary open items are IAM role cleanup and
DynamoDB CMK encryption — both low-risk with straightforward remediation.

---

## 2. Why eu-west-1 (Ireland) is Mandatory for Alexa

Amazon routes Alexa Smart Home skill invocations based on the customer's marketplace region:

| Customer Marketplace | Amazon Routes To | Lambda Must Be In |
|---------------------|-----------------|-------------------|
| India (IN) | eu-west-1 | **eu-west-1 (Ireland)** |
| Europe (UK, DE, FR, IT, ES) | eu-west-1 | eu-west-1 (Ireland) |
| North America (US, CA, MX) | us-east-1 | us-east-1 (Virginia) |
| Far East (JP, AU) | us-west-2 | us-west-2 (Oregon) |

**If the Lambda is only in us-east-1 (US)**: Indian customers' voice commands ("Alexa,
turn on the fan") are routed to eu-west-1 by Amazon's infrastructure and find no Lambda there.
The skill silently fails — the device does not respond.

This routing is enforced by Amazon and is not configurable. eu-west-1 is the only valid
deployment region for Indian Alexa customers.

---

## 3. Which Lambdas Go Where (Honeywell AWS Account)

This is the most important architectural clarification. In Honeywell's AWS account, all
infrastructure is deployed in **us-east-1 (Virginia)** — except for one Lambda which Amazon
forces to eu-west-1.

| Lambda | AWS Region | Invoked By | Must Be in eu-west-1? |
|--------|-----------|-----------|----------------------|
| `alexa` (Smart Home fulfillment) | **eu-west-1 — Ireland** | Amazon Alexa Cloud | **Yes — Amazon mandatory** |
| `alexa_start_app_to_app` | us-east-1 — Virginia | Flutter app via API Gateway | No |
| `alexa_complete_app_to_app` | us-east-1 — Virginia | Flutter app via API Gateway | No |
| `alexa_link_status` | us-east-1 — Virginia | Flutter app via API Gateway | No |
| `alexa_unlink` | us-east-1 — Virginia | Flutter app via API Gateway | No |
| `alexa_callback` | us-east-1 — Virginia | Browser OAuth redirect via API Gateway | No |
| `alexa_skill_events` | us-east-1 — Virginia | Amazon Alexa skill lifecycle events | No |

All supporting AWS infrastructure (API Gateway, Cognito, DynamoDB, IoT Core, Secrets Manager)
stays in us-east-1. Only the Smart Home fulfillment Lambda crosses to eu-west-1 — and it
connects back to us-east-1 for all data reads and device commands.

---

## 4. Honeywell Deployment — Entity Region Map

Every entity in the architecture, its AWS region, and what data it holds:

| Entity | Type | AWS Region | Data / Purpose |
|--------|------|-----------|----------------|
| Customer (India) | End user | India (physical) | Speaks voice command to Echo / Alexa app |
| Echo Device / Alexa App | Amazon hardware/app | India (physical) | Captures voice, sends to Alexa Cloud |
| Amazon Alexa Cloud | Amazon-managed | Amazon global (US) | Processes speech, identifies intent, routes to skill |
| API Gateway | AWS | us-east-1 | HTTPS entry point for Flutter app account linking calls |
| `alexa` Lambda | AWS Lambda | **eu-west-1** | Smart Home fulfillment — SYNC / QUERY / EXECUTE |
| `alexa_start_app_to_app` | AWS Lambda | us-east-1 | Initiates account linking session |
| `alexa_complete_app_to_app` | AWS Lambda | us-east-1 | Exchanges auth code for LWA tokens |
| `alexa_callback` | AWS Lambda | us-east-1 | Receives OAuth redirect from Amazon login |
| `alexa_link_status` | AWS Lambda | us-east-1 | Returns current link status to Flutter app |
| `alexa_unlink` | AWS Lambda | us-east-1 | Revokes LWA tokens, removes link |
| `alexa_skill_events` | AWS Lambda | us-east-1 | Handles skill enable/disable lifecycle events |
| Cognito User Pool | AWS | us-east-1 | Authenticates Honeywell app users |
| DynamoDB — sessions table | AWS | us-east-1 | Temporary account linking sessions (TTL: 10 min) |
| DynamoDB — tokens table | AWS | us-east-1 | Alexa LWA access + refresh tokens per user |
| DynamoDB — device mapping | AWS | us-east-1 | Maps user ID to device DUIDs |
| DynamoDB — device state | AWS | us-east-1 | Live device state (on/off, brightness, etc.) |
| Secrets Manager — LWA | AWS | **eu-west-1** | Amazon LWA client credentials (co-located with fulfillment Lambda) |
| Secrets Manager — AES key | AWS | **eu-west-1** | AES-GCM key for endpoint ID encryption |
| Secrets Manager — other | AWS | us-east-1 | Device table encryption secret |
| IoT Core | AWS | us-east-1 | MQTT broker — delivers commands to physical devices |
| Physical Device | Hardware | Customer premises (India) | Subscribed to MQTT topic; executes command |

---

## 5. End-to-End Data Flow

There are two distinct flows: **Account Linking** (one-time setup) and **Voice Command** (every
time the customer speaks to Alexa).

---

### 5.1 Flow A — Account Linking (One-Time Setup)

The customer links their Honeywell account to Alexa once through the Flutter app.

```
[INDIA — Customer Device]
Customer opens Honeywell Flutter app → taps "Link Alexa Account"
         │
         │  HTTPS POST  (TLS 1.3)
         ▼
[us-east-1 — API Gateway]
  └─► Routes to alexa_start_app_to_app Lambda
         │  Generates KMS-encrypted session token
         ▼
[us-east-1 — DynamoDB: alexa_app_linking_sessions]
  └─► Stores session (TTL 10 min)
  └─► Returns deep link URL to Flutter app
         │
         ▼
[INDIA — Customer Device]
  └─► App opens Amazon login page in browser
         │  Customer logs in with Amazon account
         ▼
[AMAZON — Login with Amazon (LWA)]
  └─► Customer approves Alexa skill
         │  302 Redirect with auth code → callback URL
         ▼
[us-east-1 — API Gateway → alexa_callback Lambda]
  └─► Receives auth code from Amazon
  └─► Returns HTML page with JS redirect to app deep link
         │
         ▼
[INDIA — Flutter App]
  └─► App receives auth code via deep link
         │  HTTPS POST  (TLS 1.3)
         ▼
[us-east-1 — API Gateway → alexa_complete_app_to_app Lambda]
  └─► Reads session from DynamoDB (us-east-1)
  └─► Reads LWA secret from Secrets Manager (eu-west-1)  ← cross-region
         │  HTTPS POST to Amazon LWA token endpoint
         ▼
[AMAZON — LWA Token API]
  └─► Returns LWA access token + refresh token
         │  Tokens encrypted with KMS key (us-east-1)
         ▼
[us-east-1 — DynamoDB: alexa_lwa_tokens]
  └─► Stores encrypted tokens against user ID
  └─► Returns success to Flutter app
         │
         ▼
[INDIA — Customer]
  └─► Account linked. Alexa can now control devices.
```

---

### 5.2 Flow B — Voice Command (Every Alexa Request)

After account linking, every voice command follows this path:

```
[INDIA — Customer]
  └─► "Alexa, turn on the living room light"
         │  Audio stream over internet
         ▼
[AMAZON — Alexa Voice Service]
  └─► Speech recognition + intent parsing
  └─► Intent: Alexa.PowerController / TurnOn
  └─► Identifies: Honeywell Smart Home skill
  └─► Customer marketplace: India (IN)
         │
         │  Amazon routes to eu-west-1 — MANDATORY for IN marketplace
         │  HTTPS POST signed directive  (TLS 1.3)
         ▼
[eu-west-1 — alexa Lambda]  ← THE ONLY ENTITY IN IRELAND
  └─► Validates Amazon request signature
  └─► Retrieves AES-GCM key from Secrets Manager (eu-west-1) — same region
  └─► Decrypts endpointId → gets device DUID
         │  Cross-region call over AWS backbone  (TLS)
         ▼
[us-east-1 — DynamoDB: alexa_lwa_tokens]
  └─► Looks up user by access token → returns user ID + DUID
         │  Cross-region call over AWS backbone  (TLS)
         ▼
[us-east-1 — IoT Core]
  └─► Publishes MQTT message:
      Topic:   iot/device/{duid}/request
      Payload: { "action": "turn_on", "device": "light" }
         │  MQTT over TLS 1.2  (persistent connection)
         ▼
[INDIA — Physical Device]
  └─► Receives command → executes → turns on light
  └─► Publishes state update to iot/device/{duid}/response
         │
         ▼
[eu-west-1 — alexa Lambda]
  └─► Reads device response
  └─► Returns Alexa success directive to Amazon
         │
         ▼
[AMAZON — Alexa Cloud]
  └─► Speaks: "OK"
         │
         ▼
[INDIA — Customer]
  └─► Light turns on.
```

---

### 5.3 Cross-Region Data Flow Summary

```
┌──────────────────────────────────────────────────────────────────┐
│                    HONEYWELL AWS ACCOUNT                         │
│                                                                  │
│  ┌──────────────────────────┐    ┌───────────────────────────┐  │
│  │      eu-west-1           │    │       us-east-1           │  │
│  │      (Ireland)           │    │       (Virginia)          │  │
│  │                          │    │                           │  │
│  │  alexa Lambda  ──────────┼────┼──► DynamoDB (lwa_tokens)  │  │
│  │  (fulfillment) ◄─────────┼────┼─── DynamoDB (device_map)  │  │
│  │       │          AWS     │    │  ► IoT Core (MQTT pub)    │  │
│  │       │        backbone  │    │                           │  │
│  │  Secrets Mgr             │    │  API Gateway              │  │
│  │  (LWA creds)             │    │  alexa_start Lambda       │  │
│  │  (AES key)               │    │  alexa_complete Lambda    │  │
│  │                          │    │  alexa_callback Lambda    │  │
│  └──────────────────────────┘    │  alexa_status Lambda      │  │
│          ▲                       │  alexa_unlink Lambda      │  │
│          │  Amazon routes        │  alexa_skill_events       │  │
│          │  IN marketplace       │  Cognito User Pool        │  │
│          │  here (mandatory)     │  Secrets Mgr (device key) │  │
│          │                       └───────────────────────────┘  │
└──────────┼───────────────────────────────────────────────────────┘
           │
    Amazon Alexa Cloud ◄──── Indian customer (voice command)
    Flutter App (account linking) ──────────────────────────────►
                                           API Gateway (us-east-1)
```

**Key takeaway**: Only the `alexa` fulfillment Lambda and two Secrets Manager entries live in
eu-west-1. Everything else — all customer data, all device data, all account linking logic —
stays in us-east-1. The eu-west-1 Lambda is a stateless compute bridge that Amazon's routing
infrastructure requires to be in Ireland.

---

## 6. Data Residency

| Data Type | Where Stored | Jurisdiction |
|-----------|-------------|--------------|
| Alexa LWA tokens (access + refresh) | DynamoDB — us-east-1 | US |
| User-device mapping | DynamoDB — us-east-1 | US |
| Device state (on/off, brightness) | DynamoDB — us-east-1 | US |
| LWA OAuth credentials | Secrets Manager — eu-west-1 | EU (Ireland) |
| AES endpoint encryption key | Secrets Manager — eu-west-1 | EU (Ireland) |
| Lambda execution (compute) | eu-west-1 (transient, in-memory only) | EU (Ireland) |
| Audit logs (CloudTrail) | S3 — multi-region | US + EU |

**Important for the architect**: Customer PII and device data are stored exclusively in US
(us-east-1). The eu-west-1 Lambda processes requests in-memory and does not persist any
customer data in Ireland. The LWA credentials in eu-west-1 Secrets Manager are service
credentials (API keys), not customer personal data.

---

## 7. Security Controls — Current State

### 7.1 Secrets Management — PASS

All sensitive credentials use AWS Secrets Manager. No secrets are hardcoded in Lambda code
or environment variables.

| Secret | Location | Used By |
|--------|----------|---------|
| LWA OAuth credentials (Client ID + Secret) | Secrets Manager — eu-west-1 | `alexa` (eu-west-1), `alexa_complete_app_to_app`, `alexa_unlink` |
| AES-GCM endpoint encryption key | Secrets Manager — eu-west-1 | `alexa` (eu-west-1) |
| Device table encryption secret | Secrets Manager — us-east-1 | `alexa` (eu-west-1) |

The LWA secret is already in eu-west-1 (`arn:aws:secretsmanager:eu-west-1:...:digilux/alexa/lwa-7RDOUm`)
which means the eu-west-1 Lambda retrieves it with minimal cross-region latency.

**Status**: PASS — No hardcoded credentials anywhere.

---

### 7.2 Encryption in Transit — PASS

| Path | Protocol | Enforced By |
|------|----------|------------|
| Amazon Alexa Cloud → eu-west-1 Lambda | HTTPS / TLS 1.3 | AWS API Gateway |
| Lambda → DynamoDB (us-east-1) | TLS 1.2+ (AWS SDK) | AWS |
| Lambda → Secrets Manager (eu-west-1) | TLS 1.2+ (AWS SDK) | AWS |
| Lambda → IoT Core (us-east-1) | TLS 1.2+ (AWS SDK) | AWS |
| IoT Core → Physical Device | MQTT over TLS 1.2 | AWS IoT |
| Flutter App → API Gateway (account linking) | HTTPS / TLS 1.3 | AWS |

**Status**: PASS — All data in transit is encrypted end-to-end.

---

### 7.3 Encryption at Rest — PARTIAL

| Resource | Encryption | Key Type | Status |
|----------|-----------|----------|--------|
| Lambda code (eu-west-1) | AES-256 | AWS-owned key | Acceptable |
| Secrets Manager values | AES-256 | AWS-owned key | Acceptable |
| DynamoDB token tables (us-east-1) | AES-256 | AWS-owned key | **Gap — upgrade to CMK** |
| CloudTrail logs (S3) | AES-256 | AWS-owned key | Acceptable |

**Gap**: DynamoDB tables storing Alexa LWA tokens use AWS-owned keys. Encryption is active
but Digilux has no independent key custody. Upgrading to a Customer Managed Key (CMK) gives
full key rotation control and audit trail of every decrypt operation.

**Recommendation**: Create `alias/digilux/alexa-tokens` CMK in us-east-1 and apply to
`digilux_honeywell_alexa_lwa_tokens` and `alexa_app_linking_sessions` tables.

---

### 7.4 IAM / Access Control — NEEDS ATTENTION

The `alexa` Lambda in eu-west-1 uses an auto-generated service role (`alexa-role-zt15wjgn`)
rather than a purpose-built IAM role. This is a flag for a cybersecurity architect because:

- Auto-generated roles are harder to audit
- Permissions may be broader than necessary
- Naming convention does not follow Digilux standards (`digilux-*`)

The App-to-App Lambdas in us-east-1 use properly named dedicated roles:

| Lambda | IAM Role |
|--------|---------|
| `alexa_start_app_to_app` | `digilux-alexa-start-role` |
| `alexa_complete_app_to_app` | `digilux-alexa-complete-role` |
| `alexa_link_status` | `digilux-alexa-status-role` |
| `alexa_unlink` | `digilux-alexa-unlink-role` |
| `alexa_callback` | `digilux-alexa-callback-role` |
| `alexa_skill_events` | `digilux-alexa-skill-events-role` |

**Action Required**: Review and replace the `alexa-role-zt15wjgn` auto-generated role on
the eu-west-1 `alexa` Lambda with a purpose-built `digilux-alexa-fulfillment-role` following
least-privilege principles.

---

### 7.5 Alexa Request Signature Validation

Amazon signs every Smart Home skill directive sent to the Lambda. The Lambda must validate
this signature to prevent spoofed requests (anyone calling your Lambda endpoint pretending
to be Amazon).

**Action**: Confirm signature validation is implemented in the eu-west-1 `alexa` Lambda code.
This is a mandatory security control that Alexa's own skill certification requires.

---

### 7.6 Audit Logging (CloudTrail) — PASS

| Trail | Coverage | Status |
|-------|---------|--------|
| `management-events` | Multi-region (includes eu-west-1) | **Active — logging today** |
| `digilux-honeywell-trail` | Multi-region | Active |

All Lambda deployments, IAM changes, and Secrets Manager access in eu-west-1 are captured
automatically by the multi-region `management-events` trail.

**Status**: PASS — eu-west-1 is covered.

---

### 7.7 Device Identity Protection — PASS

The `endpointId` sent to Amazon's Alexa Cloud is AES-GCM encrypted. Amazon never sees the
raw Device Unique Identifier (DUID). The encryption key is in Secrets Manager (eu-west-1)
and is never transmitted outside AWS.

**Status**: PASS

---

### 7.8 Network Isolation (VPC) — NOT CONFIGURED

The `alexa` Lambda in eu-west-1 runs without a VPC. Lambda-to-DynamoDB and Lambda-to-IoT
traffic travels over the AWS private backbone (not the public internet) but without
network-layer controls.

**Risk**: Low — all SDK calls enforce TLS. No unencrypted traffic is possible.

**Recommendation**: For defence-in-depth, place the `alexa` Lambda in a VPC in eu-west-1
with a VPC endpoint for Secrets Manager. Cross-region calls to us-east-1 DynamoDB/IoT
will continue over the AWS backbone (no VPC endpoint possible cross-region — this is acceptable).

---

### 7.9 Can Anyone See Our Lambda Code?

| Party | Access | Basis |
|-------|--------|-------|
| AWS engineers | **No** | AWS Shared Responsibility Model + SOC 2 Type II audit |
| Amazon (Alexa team) | **No** | Amazon only invokes via HTTPS; zero code visibility |
| Other AWS tenants | **No** | Full tenant isolation by design |
| Digilux IAM users | Only with `lambda:GetFunction` permission | Controlled by your IAM policies |
| Public internet | **No** | Lambda is not publicly accessible |

AWS's "no access" policy is independently audited under SOC 2 Type II. Reports are
available via AWS Artifact on request.

---

## 8. Compliance Posture

| Standard | AWS eu-west-1 Status | Relevance |
|----------|---------------------|-----------|
| ISO 27001 | Certified | Information security management |
| SOC 2 Type II | Certified | Security, availability, confidentiality |
| PCI DSS Level 1 | Certified | Shows infrastructure rigour |
| GDPR | Compliant infrastructure | LWA service credentials in Ireland — not customer PII |
| AWS Shared Responsibility | Applies | AWS secures infrastructure; Digilux secures code + config |

---

## 9. Risk Summary

| Risk | Likelihood | Impact | Mitigation | Status |
|------|-----------|--------|-----------|--------|
| AWS/Amazon staff accessing Lambda code | Very Low | High | AWS SOC 2 + contractual controls | Mitigated |
| Secrets exposed via hardcoded values | None | Critical | All in Secrets Manager | Mitigated |
| Spoofed Alexa directive to Lambda | Low | Medium | Alexa signature validation | Verify implementation |
| Auto-generated IAM role (eu-west-1) | Low | Medium | Replace with purpose-built role | **Open** |
| DynamoDB tokens unprotected by CMK | Low | Low | Upgrade to CMK | Open — scheduled |
| No VPC network isolation | Low | Low | TLS on all calls; acceptable | Open — low priority |
| Cross-region data flow (eu-west-1 → us-east-1) | N/A | Low | Documented; customer data stays in US | Documented |

---

## 10. Recommended Actions

### Must Do (blocks security sign-off)
1. **Replace auto-generated IAM role** on `alexa` (eu-west-1) with `digilux-alexa-fulfillment-role`
   following least-privilege, matching the naming standard of us-east-1 roles
2. **Confirm Alexa request signature validation** is implemented in the eu-west-1 Lambda code

### Should Do (within 30 days)
3. **Create CMK** `alias/digilux/alexa-tokens` in us-east-1 and apply to Alexa DynamoDB tables
4. **Enable explicit CloudTrail data events** for Alexa DynamoDB tables (token access audit trail)

### Nice to Have (next quarter)
5. **Deploy `alexa` Lambda inside a VPC** in eu-west-1 with Secrets Manager VPC endpoint
6. **Tag all eu-west-1 resources** with `Project: digilux-alexa` for cost and security visibility

---

## 11. Conclusion

Deploying the Alexa Smart Home fulfillment Lambda to AWS eu-west-1 (Ireland) is a **mandatory
Amazon requirement** for Indian marketplace customers — not an architectural choice. Without
it, Indian customers' voice commands to Alexa-enabled Digilux devices will silently fail.

The `alexa` Lambda is already deployed in eu-west-1 as of August 2026. The core security
foundations are in place: secrets are in Secrets Manager, data in transit is encrypted, audit
logging is active and multi-region, and device identities are AES-GCM protected.

Two items require attention before formal security sign-off: replacing the auto-generated IAM
role with a purpose-built one, and confirming Alexa request signature validation. Both are
low-effort changes. Customer data remains in US (us-east-1) at all times. eu-west-1
is stateless compute only.

This architecture is standard, well-precedented, and appropriate for cybersecurity approval.

---

*Document prepared by Digilux Engineering. For questions contact the platform team.*
