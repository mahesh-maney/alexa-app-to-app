# Security Assessment — Google Home Lambda Deployment on AWS eu-west-1 (Ireland)

**Prepared by**: Digilux Engineering Team
**Date**: 2026-10-01
**Classification**: Internal — Management / Technology / Cybersecurity
**Version**: 1.0

---

## 1. Executive Summary

Digilux has developed a Google Home Cloud-to-Cloud smart home integration. As part of scaling
this feature for Indian customers, the `google_home` fulfillment Lambda function is being deployed
to AWS eu-west-1 (Ireland). This document assesses the security posture of that deployment and
addresses the key questions a cybersecurity architect would raise.

**Bottom line**: The deployment is architecturally sound and follows AWS security best practices.
All sensitive credentials are managed via AWS Secrets Manager. No secrets are hardcoded. Audit
logging is active. The primary gaps identified — KMS key scoping and DynamoDB CMK encryption —
are low-risk, well-understood, and have clear remediation paths.

---

## 2. Why Ireland (eu-west-1)?

Google Home's Cloud-to-Cloud integration routes fulfillment requests from Google's servers to the
URL configured in the Google Actions Console. Unlike Amazon Alexa (which has strict per-region
Lambda routing), Google Home calls a fixed HTTPS endpoint regardless of the customer's geography.

The eu-west-1 deployment is motivated by:

- **Latency**: Ireland is one of AWS's most connected regions with low-latency paths to both
  Europe and Asia-Pacific
- **Compliance readiness**: eu-west-1 is SOC 1/2/3, ISO 27001, PCI DSS, and GDPR compliant
- **Proximity to Google's infrastructure**: Google's Cloud-to-Cloud servers operate from
  European data centers, reducing round-trip time for fulfillment requests
- **Alexa co-location**: The Alexa Smart Home skill Lambda is also required in eu-west-1 for
  Indian marketplace customers (Amazon routing requirement) — co-locating Google Home here
  simplifies operations

---

## 3. Architecture Overview

```
Google Home App (Customer)
        │
        │  HTTPS (TLS 1.3)
        ▼
Google Home Cloud (Google's servers, Europe)
        │
        │  HTTPS POST — signed request
        ▼
AWS API Gateway (ap-south-1) ──► google_home Lambda (eu-west-1)
                                        │
                          ┌─────────────┼─────────────┐
                          ▼             ▼             ▼
                    DynamoDB       Secrets Mgr     IoT Core
                  (ap-south-1)    (eu-west-1)   (ap-south-1)
                  [tokens/state]  [AES key]     [device cmd]
                                        │
                                        ▼
                                 Physical Device
                               (MQTT subscription)
```

**Key principle**: The Lambda is **stateless**. It holds no customer data. All persistent data
(tokens, device state) resides in DynamoDB in ap-south-1 (India). eu-west-1 is compute only.

---

## 4. Data Residency

| Data Type | Where Stored | Jurisdiction |
|-----------|-------------|--------------|
| OAuth access/refresh tokens | DynamoDB — ap-south-1 | India |
| Device state (on/off, brightness) | DynamoDB — ap-south-1 | India |
| User-device mapping | DynamoDB — ap-south-1 | India |
| AES encryption key | Secrets Manager — eu-west-1 | EU (Ireland) |
| Lambda execution (compute) | eu-west-1 (transient, no storage) | EU (Ireland) |
| Audit logs (CloudTrail) | S3 — multi-region | India + EU |

**Important**: Customer PII and device data are stored exclusively in India (ap-south-1).
The eu-west-1 Lambda processes requests in-memory only. No customer data is written to or
stored in Ireland.

---

## 5. Security Controls — Current State

### 5.1 Secrets Management

All sensitive credentials are stored in AWS Secrets Manager. No secrets are hardcoded in
Lambda code or environment variables.

| Secret | Location | Used By |
|--------|----------|---------|
| AES-GCM endpoint encryption key | Secrets Manager — eu-west-1 | `google_home` Lambda |
| Google OAuth client secret | Secrets Manager — ap-south-1 | `google_home_oauth_token` Lambda |
| Google Service Account key | Secrets Manager — ap-south-1 | `google_home_unlink` Lambda |

**Status**: PASS — No hardcoded credentials anywhere in the codebase or environment variables.

---

### 5.2 Encryption in Transit

All communication between components uses TLS:

| Path | Protocol | Status |
|------|----------|--------|
| Google Cloud → API Gateway | HTTPS / TLS 1.3 | Enforced by AWS |
| API Gateway → Lambda | AWS internal (encrypted) | Enforced by AWS |
| Lambda → DynamoDB | TLS 1.2+ (AWS SDK) | Enforced by AWS |
| Lambda → IoT Core | TLS 1.2+ (AWS SDK) | Enforced by AWS |
| Lambda → Secrets Manager | TLS 1.2+ (AWS SDK) | Enforced by AWS |
| IoT Core → Physical Device | MQTT over TLS 1.2 | Enforced by AWS IoT |

**Status**: PASS — All data in transit is encrypted end-to-end.

---

### 5.3 Encryption at Rest

| Resource | Encryption | Key Type |
|----------|-----------|----------|
| Lambda code | AES-256 (AWS managed) | AWS-owned key |
| DynamoDB tables (tokens, sessions, auth codes) | AES-256 | AWS-owned key |
| Secrets Manager values | AES-256 | AWS-owned key |
| CloudTrail logs (S3) | AES-256 | AWS-owned key |

**Current Gap**: DynamoDB tables storing OAuth tokens use AWS-owned keys rather than
Customer Managed Keys (CMK). This is functional encryption but does not give Digilux
independent key custody.

**Recommendation**: Upgrade the three Google Home DynamoDB tables to CMK using
`alias/digilux/google-home` KMS key. This is a non-breaking change with no downtime.

---

### 5.4 IAM / Access Control

The `digilux-google-home-role` IAM role follows least-privilege principles:

| Permission | Scope | Assessment |
|-----------|-------|------------|
| `dynamodb:GetItem/PutItem/UpdateItem/DeleteItem/Query` | 5 specific table ARNs only | GOOD |
| `iot:Publish` | Specific topic pattern `iot/device/*/request` and `iot/device/*/scene` only | GOOD |
| `secretsmanager:GetSecretValue` | 2 specific secret ARNs only | GOOD |
| `kms:Decrypt/GenerateDataKey` | `Resource: "*"` (wildcard) | FLAG — needs fixing |
| `dynamodb:Scan` | On device tables | FLAG — review if needed |

**Action Required**: The KMS wildcard must be scoped to specific key ARNs before the
eu-west-1 deployment. This is a 5-minute configuration change.

**No wildcard `*` permissions** exist on any high-privilege actions (IAM, S3 delete, etc.).

---

### 5.5 Audit Logging (CloudTrail)

| Trail | Region Coverage | Logging Status | Last Delivery |
|-------|----------------|---------------|---------------|
| `management-events` | Multi-region (all regions) | **Active** | 2026-10-01 |
| `digilux-honeywell-trail` | Multi-region | Active | Active |

CloudTrail captures:
- All Lambda deployments (`UpdateFunctionCode`)
- All IAM changes
- All Secrets Manager access
- All KMS key usage
- All DynamoDB admin operations

**Status**: PASS — Full audit trail is active and multi-region, covering eu-west-1.

---

### 5.6 Device Identity Protection

The `endpointId` sent to Google's servers is AES-GCM encrypted. Google never sees the
raw device DUID (Device Unique Identifier). The encryption key is stored in Secrets Manager
and rotated independently of the application.

**Status**: PASS — Device identity is protected at the API boundary.

---

### 5.7 Network Isolation (VPC)

Currently, all Google Home Lambdas run outside a VPC. Lambda-to-DynamoDB and
Lambda-to-IoT traffic travels over the AWS private backbone (not the public internet)
but without VPC-level network controls.

**Risk level**: Low — TLS encryption is enforced on all calls; no unencrypted traffic
is possible on AWS SDK calls. However, a VPC with private endpoints would add a
network-layer control for defence-in-depth.

**Recommendation**: Place the `google_home` Lambda in a VPC in eu-west-1 with:
- VPC endpoint for Secrets Manager (eu-west-1)
- Cross-region calls to ap-south-1 DynamoDB/IoT travel over AWS backbone (acceptable)

---

### 5.8 Code Visibility — Can Anyone See Our Code?

| Party | Can They See Lambda Code? | Basis |
|-------|--------------------------|-------|
| AWS engineers | **No** | AWS Shared Responsibility Model + SOC 2 Type II audit |
| Google (Alexa team) | **No** | Google only invokes via HTTPS; no code access |
| Other AWS customers | **No** | Complete tenant isolation by design |
| Digilux AWS IAM users | Only those with `lambda:GetFunction` permission | Controlled by your IAM policies |
| Public internet | **No** | Lambda is not publicly accessible |

AWS operates under a contractual and technical "no access" policy for customer code.
This is independently audited and covered under AWS's SOC 2 Type II reports, which
are available on request via AWS Artifact.

---

## 6. Compliance Posture

| Standard | AWS eu-west-1 Certification | Impact on Digilux |
|----------|---------------------------|-------------------|
| ISO 27001 | Certified | Information security management covered |
| SOC 2 Type II | Certified | Security, availability, confidentiality audited |
| PCI DSS Level 1 | Certified | Payment data standards (not applicable but shows rigour) |
| GDPR | Compliant infrastructure | Since data stays in ap-south-1 (India), GDPR impact is minimal |
| AWS Shared Responsibility | Applies | AWS secures infrastructure; Digilux secures code and config |

---

## 7. Risk Summary

| Risk | Likelihood | Impact | Mitigation | Status |
|------|-----------|--------|-----------|--------|
| AWS staff accessing Lambda code | Very Low | High | AWS contractual + SOC 2 controls | Mitigated |
| Secrets exposed via hardcoded values | None | Critical | All secrets in Secrets Manager | Mitigated |
| Unauthorised Lambda invocation | Very Low | Medium | API Gateway + Google request signing | Mitigated |
| KMS wildcard over-permission | Low | Medium | Fix Resource to specific ARN | **Open — fix before deploy** |
| DynamoDB tokens unprotected by CMK | Low | Low | Upgrade to CMK | Open — scheduled |
| No VPC network isolation | Low | Low | TLS on all calls; VPC optional | Open — low priority |
| Cross-region data flow (eu-west-1 → ap-south-1) | N/A | Low | Documented; data stays in India | Documented |

---

## 8. Recommended Actions Before eu-west-1 Deployment

### Must Do (blocks deployment sign-off)
1. **Fix KMS wildcard** — Change `Resource: "*"` to specific KMS key ARN in `digilux-google-home-role`
2. **Verify IAM trust policy** — Confirm the role is assumable by `lambda.amazonaws.com` in eu-west-1

### Should Do (within 30 days)
3. **Move Google OAuth secret to eu-west-1** — Reduces cross-region secret retrieval from Ireland Lambda
4. **Create `alias/digilux/google-home` KMS CMK** — Apply to Google Home DynamoDB tables
5. **Enable explicit CloudTrail data events** for Google Home DynamoDB tables

### Nice to Have (next quarter)
6. **Deploy Lambda inside a VPC** in eu-west-1 with Secrets Manager VPC endpoint
7. **Remove `dynamodb:Scan`** from IAM role if not required by application logic

---

## 9. Conclusion

Deploying the `google_home` Lambda to AWS eu-west-1 (Ireland) is technically safe and
follows industry-standard practices used by enterprises globally. The core security
foundations — no hardcoded secrets, encrypted data in transit and at rest, active
audit logging, scoped IAM permissions, and protected device identities — are all in place.

Two items require remediation before deployment sign-off (KMS wildcard scope and IAM trust
policy verification). These are configuration-level changes with no application impact and
can be completed within a single working day.

Customer data remains in India (ap-south-1) at all times. eu-west-1 is used exclusively
for stateless compute. This architecture is defensible from a data residency perspective
and appropriate for a cybersecurity architecture review.

---

*Document prepared by Digilux Engineering. For questions contact the platform team.*
