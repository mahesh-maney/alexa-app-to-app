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

The `alexa` Smart Home fulfillment Lambda is already deployed in eu-west-1 as of August 2026.
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

## 3. Which Lambdas Go Where

This is the most important architectural clarification:

| Lambda | Region | Invoked By | Must Be in eu-west-1? |
|--------|--------|-----------|----------------------|
| `alexa` (Smart Home fulfillment) | **eu-west-1** | Amazon Alexa Cloud | **Yes — Amazon requirement** |
| `alexa_start_app_to_app` | us-east-1 | Flutter app (via API Gateway) | No |
| `alexa_complete_app_to_app` | us-east-1 | Flutter app (via API Gateway) | No |
| `alexa_link_status` | us-east-1 | Flutter app (via API Gateway) | No |
| `alexa_unlink` | us-east-1 | Flutter app (via API Gateway) | No |
| `alexa_callback` | us-east-1 | Browser redirect (via API Gateway) | No |
| `alexa_skill_events` | us-east-1 | Amazon Alexa skill events | No |

Only the `alexa` Smart Home fulfillment Lambda (handles SYNC/QUERY/EXECUTE/DISCONNECT directives)
must be in eu-west-1. The App-to-App account linking Lambdas are called by the Digilux Flutter
app through API Gateway in us-east-1 — they are correctly placed in US.

**Current status**: The `alexa` Lambda is already deployed in eu-west-1 (last updated
2026-08-22). This requirement is already met.

---

## 4. Architecture Overview

```
Customer speaks to Alexa (India)
         │
         │  Amazon routes to eu-west-1 (mandatory for IN marketplace)
         ▼
Amazon Alexa Cloud (routes to eu-west-1)
         │
         │  HTTPS POST — signed directive (SYNC / QUERY / EXECUTE)
         ▼
  alexa Lambda (eu-west-1)  ←── stateless, no data stored here
         │
    ┌────┴────────────────────────┐
    ▼                             ▼
Secrets Manager              DynamoDB / IoT Core
(eu-west-1)                  (us-east-1 — US)
[LWA tokens, AES key]        [user tokens, device state]
                                    │
                                    ▼
                            Physical Device
                          (MQTT via IoT Core)


Flutter App (Account Linking — separate flow)
         │
         │  HTTPS via API Gateway (us-east-1)
         ▼
App-to-App Lambdas (us-east-1) — alexa_start, alexa_complete, etc.
```

**Key principle**: The eu-west-1 Lambda is **stateless**. It reads data from us-east-1 and
publishes commands to IoT Core in us-east-1. No customer data is written to or stored in Ireland.

---

## 5. Data Residency

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

## 6. Security Controls — Current State

### 6.1 Secrets Management — PASS

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

### 6.2 Encryption in Transit — PASS

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

### 6.3 Encryption at Rest — PARTIAL

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

### 6.4 IAM / Access Control — NEEDS ATTENTION

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

### 6.5 Alexa Request Signature Validation

Amazon signs every Smart Home skill directive sent to the Lambda. The Lambda must validate
this signature to prevent spoofed requests (anyone calling your Lambda endpoint pretending
to be Amazon).

**Action**: Confirm signature validation is implemented in the eu-west-1 `alexa` Lambda code.
This is a mandatory security control that Alexa's own skill certification requires.

---

### 6.6 Audit Logging (CloudTrail) — PASS

| Trail | Coverage | Status |
|-------|---------|--------|
| `management-events` | Multi-region (includes eu-west-1) | **Active — logging today** |
| `digilux-honeywell-trail` | Multi-region | Active |

All Lambda deployments, IAM changes, and Secrets Manager access in eu-west-1 are captured
automatically by the multi-region `management-events` trail.

**Status**: PASS — eu-west-1 is covered.

---

### 6.7 Device Identity Protection — PASS

The `endpointId` sent to Amazon's Alexa Cloud is AES-GCM encrypted. Amazon never sees the
raw Device Unique Identifier (DUID). The encryption key is in Secrets Manager (eu-west-1)
and is never transmitted outside AWS.

**Status**: PASS

---

### 6.8 Network Isolation (VPC) — NOT CONFIGURED

The `alexa` Lambda in eu-west-1 runs without a VPC. Lambda-to-DynamoDB and Lambda-to-IoT
traffic travels over the AWS private backbone (not the public internet) but without
network-layer controls.

**Risk**: Low — all SDK calls enforce TLS. No unencrypted traffic is possible.

**Recommendation**: For defence-in-depth, place the `alexa` Lambda in a VPC in eu-west-1
with a VPC endpoint for Secrets Manager. Cross-region calls to us-east-1 DynamoDB/IoT
will continue over the AWS backbone (no VPC endpoint possible cross-region — this is acceptable).

---

### 6.9 Can Anyone See Our Lambda Code?

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

## 7. Compliance Posture

| Standard | AWS eu-west-1 Status | Relevance |
|----------|---------------------|-----------|
| ISO 27001 | Certified | Information security management |
| SOC 2 Type II | Certified | Security, availability, confidentiality |
| PCI DSS Level 1 | Certified | Shows infrastructure rigour |
| GDPR | Compliant infrastructure | LWA service credentials in Ireland — not customer PII |
| AWS Shared Responsibility | Applies | AWS secures infrastructure; Digilux secures code + config |

---

## 8. Risk Summary

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

## 9. Recommended Actions

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

## 10. Conclusion

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
