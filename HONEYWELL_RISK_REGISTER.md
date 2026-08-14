# Risk Register
## Honeywell Alexa Smart Home — eu-west-1 Deployment

**Version:** 1.0  
**Date:** 2026-08-14  
**Purpose:** Identify every known risk in the eu-west-1 deployment approach, with mitigations, so management has complete visibility before approval  

Risk Severity: 🔴 High — 🟠 Medium — 🟡 Low  
Likelihood: **H** = High, **M** = Medium, **L** = Low

---

## Section 1 — Architectural Risks

### R-001 — Wrong ALEXA_GATEWAY_URL Used

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | M |
| **Description** | There are two Alexa proactive event gateway URLs: `api.amazonalexa.com` (US/global) and `api.eu.amazonalexa.com` (EU). The EU endpoint must be used when the Lambda is in eu-west-1. Using the US URL will cause proactive notifications (e.g. "light turned on" status updates) to fail with 401/403 errors. Voice control (TurnOn/TurnOff) is not affected — only proactive events sent FROM the Lambda TO Alexa are affected. |
| **Detection** | Proactive events silently drop; CloudWatch shows HTTP 4xx from `api.amazonalexa.com` |
| **Mitigation** | Set `ALEXA_GATEWAY_URL=https://api.eu.amazonalexa.com/v3/events` (note: `.eu.`) in the Lambda env vars. This is already set correctly in the Digilux production configuration and is included in the deployment checklist (item 2.5). |
| **Verification** | After deployment, trigger a device state change and confirm no HTTP errors in CloudWatch |

---

### R-002 — Skill Endpoint Points to Wrong Region or ARN

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | L |
| **Description** | If the Alexa Developer Console skill endpoint is set to the wrong Lambda ARN (e.g. a test account's ARN or the wrong region), all voice commands are silently discarded. The user gets "I couldn't find your device" with no error in Honeywell's CloudWatch. |
| **Detection** | Zero invocations in CloudWatch after voice command. Alexa returns generic "something went wrong". |
| **Mitigation** | After setting the endpoint in Developer Console, verify it using the ASK CLI: `ask smapi get-skill --skill-id <ID> --stage development`. Confirm the ARN contains `eu-west-1` and Honeywell's AWS account ID. Verified in checklist item 3.1. |

---

### R-003 — Lambda Resource Policy Missing or Incorrect Skill ID

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | M |
| **Description** | The Lambda must have a resource policy granting `alexa-connectedhome.amazon.com` the right to invoke it, scoped to the specific Skill ID via `EventSourceToken`. If this is missing, or if the Skill ID in the policy doesn't match the actual Skill ID, Alexa will receive `AccessDeniedException` when trying to invoke the Lambda. Voice commands fail silently. |
| **Detection** | Lambda is never invoked. AWS will log the access denial at the service level (not in Lambda CloudWatch). |
| **Mitigation** | Run the `aws lambda add-permission` command (checklist item 2.6.1) and verify with `aws lambda get-policy`. The Skill ID in `EventSourceToken` must exactly match the Skill ID from the Developer Console. |

---

## Section 2 — Configuration Risks

### R-004 — Secrets in Wrong Region or Inaccessible

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | M |
| **Description** | The Lambda creates a Secrets Manager client using `LWA_SECRET_REGION` and `ENDPOINT_KEY_REGION` env vars. If these point to the wrong region, or if the IAM role does not have `GetSecretValue` on the eu-west-1 ARNs, the Lambda will throw `AccessDeniedException`. This was directly observed and resolved during the PoC on 2026-08-13. |
| **Detection** | CloudWatch shows: `AccessDeniedException: User is not authorized to perform secretsmanager:GetSecretValue`. Discovery returns 0 devices. |
| **Mitigation** | Both secrets must be in eu-west-1. IAM role must include `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:eu-west-1:<account>:secret:honeywell/alexa/*`. Set `LWA_SECRET_REGION=eu-west-1` and `ENDPOINT_KEY_REGION=eu-west-1`. Verified in checklist items 2.1, 2.3.1, 2.3.2. |

---

### R-005 — DynamoDB Table Names Not Updated

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | M |
| **Description** | The Lambda code references table names via env vars (`USER_DEVICE_DETAILS_TABLE`, `LWA_TOKENS_TABLE`, etc.). If these are left as Digilux defaults (e.g. `digilux_honeywell_alexa_lwa_tokens`), the Lambda will either query Digilux's tables (wrong account — access denied) or Honeywell's tables under the wrong names (not found). Both result in 0 devices discovered. |
| **Detection** | CloudWatch shows: `ResourceNotFoundException` or `AccessDeniedException` from DynamoDB. |
| **Mitigation** | All table name env vars must be updated to Honeywell's actual table names before deployment. Verified in checklist item 6.9. |

---

### R-006 — Cognito User Pool ID Not Updated

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | M |
| **Description** | The Lambda validates the Alexa bearer token against a Cognito User Pool. If `COGNITO_USER_POOL_ID` still points to Digilux's pool (`ap-south-1_h1o8s7257`), all token validations will fail for Honeywell users. Every directive will return `Invalid JWT format` or `Token validation failed`. |
| **Detection** | CloudWatch shows: `Invalid JWT format` or `TokenValidationError` for every invocation. |
| **Mitigation** | Set `COGNITO_USER_POOL_ID` and `COGNITO_REGION` to Honeywell's Cognito pool. Verified in checklist item 6.10. |

---

### R-007 — AES Endpoint Key Wrong Length or Format

| Field | Detail |
|-------|--------|
| **Severity** | 🔴 High |
| **Likelihood** | L |
| **Description** | The endpoint encryption key must be exactly 32 bytes (AES-256), base64-encoded. If the key is generated incorrectly (wrong length, wrong encoding), endpoint ID encryption/decryption will fail. Discovery will return 0 endpoints. TurnOn/TurnOff will fail to decrypt the endpoint ID and throw an error. |
| **Detection** | CloudWatch shows: `ValueError: AES key must be 16, 24, or 32 bytes long` during Discovery or control. |
| **Mitigation** | Generate the key using: `python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"`. This produces exactly 32 random bytes, base64-encoded. Store under key `"key"` in the JSON secret. Verified in checklist item 2.1.3. |

---

## Section 3 — Known Code Bugs (Must Fix Before Honeywell Deployment)

### R-008 — `eu-west-1` Hardcoded in `alexa_link_status`

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | H (will definitely trigger if not fixed) |
| **Description** | `lambdas/alexa_link_status/lambda_function.py` creates its Secrets Manager client with `region_name="eu-west-1"` hardcoded. For Digilux, this is correct by coincidence. For Honeywell, if the LWA secret is in a different region, this will cause `AccessDeniedException`. Even if Honeywell also uses eu-west-1, relying on hardcoded values is brittle. |
| **Mitigation** | Fix the code to use `os.environ.get("LWA_SECRET_REGION", "eu-west-1")`. Safe fix sequence documented in HONEYWELL_DEPLOYMENT_GUIDE.md Bug 1. This fix has already been tested on Digilux. |
| **Status** | ⚠️ Fix not yet deployed to production — must be applied before Honeywell deployment |

---

### R-009 — Digilux AWS Account ID in `LWA_SECRET_ARN` Default

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | H (will definitely trigger if not fixed) |
| **Description** | `lambdas/alexa_link_status/lambda_function.py` has Digilux's secret ARN (`arn:aws:secretsmanager:eu-west-1:986906626244:...`) as the default for `LWA_SECRET_ARN`. In Honeywell's account, if this env var is accidentally omitted, the Lambda will silently try to read Digilux's secret and receive `AccessDeniedException`. |
| **Mitigation** | Remove the hardcoded default and add `LWA_SECRET_ARN` to `_REQUIRED_VARS` so the Lambda fails at startup with a clear error if not set. Fix sequence in HONEYWELL_DEPLOYMENT_GUIDE.md Bug 2. |
| **Status** | ⚠️ Fix not yet deployed to production — must be applied before Honeywell deployment |

---

## Section 4 — Operational Risks

### R-010 — Cold Start Latency on First Invocation

| Field | Detail |
|-------|--------|
| **Severity** | 🟡 Low |
| **Likelihood** | H (always happens after idle period) |
| **Description** | The first invocation after a period of inactivity triggers a Lambda cold start, adding 400–600ms to the response time. Alexa has a timeout of ~8 seconds for skill responses, so cold starts do not cause failures. Users may perceive a slight delay on the first command after the skill is idle. |
| **Mitigation** | Option A: Accept the cold start (same behaviour as Digilux production). Option B: Enable Lambda Provisioned Concurrency (additional cost ~$15-20/month) to eliminate cold starts entirely. Recommendation: start without Provisioned Concurrency, enable only if Honeywell's SLA requires it. |

---

### R-011 — Cross-Region Latency (eu-west-1 Lambda → ap-south-1 DynamoDB)

| Field | Detail |
|-------|--------|
| **Severity** | 🟡 Low |
| **Likelihood** | H (inherent in architecture) |
| **Description** | The Lambda in eu-west-1 calls DynamoDB and IoT Core in ap-south-1. This cross-region hop adds ~80–120ms per API call. A single TurnOn command makes ~2-3 DynamoDB calls, adding ~200–350ms total. Measured warm invocation time was 440–750ms end-to-end — within Alexa's acceptable response window. |
| **Mitigation** | Option A: Accept cross-region latency (same as Digilux production which has been running since July 2026). Option B: Co-locate DynamoDB in eu-west-1 (eliminates cross-region hop, requires DynamoDB migration). Recommendation: Option A unless Honeywell's SLA requires sub-200ms. |

---

### R-012 — LWA Token Expiry / Refresh Failure

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | L |
| **Description** | LWA access tokens expire every 1 hour. The Lambda stores a refresh token and obtains new access tokens automatically. If the refresh fails (e.g. user revokes access from Amazon account, network issue), proactive events will fail. Voice control is unaffected — it uses the Alexa-provided bearer token, not the LWA token. |
| **Detection** | CloudWatch shows HTTP 401 from `api.eu.amazonalexa.com` during proactive event publishing. |
| **Mitigation** | LWA token refresh is implemented in the existing Lambda code. The `alexa_link_status` Lambda also validates and refreshes tokens on every status check. If refresh permanently fails, `GET /alexa/status` will return `linked: false` and the user is prompted to re-link. No action needed — this is handled automatically. |

---

### R-013 — Alexa Skill Certification Requirements

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | M |
| **Description** | Amazon requires Smart Home skills to pass a certification review before being published to end users. Development/beta testing works without certification, but going live requires submission. Certification review typically takes 5–10 business days and may require changes to the skill description, example phrases, or privacy policy URL. |
| **Mitigation** | Start the certification submission process in parallel with the Honeywell AWS deployment approval process. Do not block deployment approval on certification outcome — they are independent processes. The Lambda code itself does not change for certification — only the Developer Console metadata. |

---

### R-014 — Echo Device Registered to Different Amazon Account

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | M |
| **Description** | If a test Echo device is registered to a different Amazon account than the one that linked the Digilux/Honeywell skill, voice commands from that device will not reach the Lambda. The device sends directives as its registered Amazon account, which has no linked skill. This was observed during testing on 2026-08-13. |
| **Detection** | Echo device says "I don't know that device" or "I couldn't find [device name]" despite successful account linking. |
| **Mitigation** | Ensure the Echo device used for testing is registered to the same Amazon account that linked the skill. Verify by asking the device: "Alexa, what account am I registered to?" |

---

## Section 5 — Security Risks

### R-015 — AES Endpoint Key Rotation

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | L |
| **Description** | The AES-256 key used to encrypt device endpoint IDs is stored in Secrets Manager. If this key is rotated without updating all existing encrypted endpoint IDs in the system, previously discovered devices will fail to decrypt and voice commands will return `NO_SUCH_ENDPOINT`. |
| **Mitigation** | Do not rotate the AES key unless all endpoint IDs are re-encrypted simultaneously. Key rotation is a planned operation requiring a coordinated release. The key does not expire automatically. Honeywell should treat it as a long-lived secret and rely on Secrets Manager access controls (IAM) and KMS encryption at rest for protection. |

---

### R-016 — IAM Role Too Permissive

| Field | Detail |
|-------|--------|
| **Severity** | 🟠 Medium |
| **Likelihood** | M |
| **Description** | If the IAM role is configured with `*` wildcards on DynamoDB or Secrets Manager resources (for simplicity during setup), the Lambda will have broader access than required. This violates Honeywell's least-privilege security policy. |
| **Mitigation** | Scope all IAM permissions to specific resource ARNs as listed in checklist section 2.3. The IAM policy in HONEYWELL_DEPLOYMENT_GUIDE.md uses fully qualified ARNs — do not replace these with wildcards. Cyber Security team should review the IAM policy before go-live. |

---

## Summary Table

| ID | Risk | Severity | Likelihood | Status |
|----|------|----------|------------|--------|
| R-001 | Wrong ALEXA_GATEWAY_URL | 🔴 High | M | Mitigated by checklist |
| R-002 | Wrong skill endpoint ARN | 🔴 High | L | Mitigated by checklist |
| R-003 | Lambda resource policy missing | 🔴 High | M | Mitigated by checklist |
| R-004 | Secrets in wrong region | 🔴 High | M | Mitigated by checklist |
| R-005 | DynamoDB table names wrong | 🔴 High | M | Mitigated by checklist |
| R-006 | Wrong Cognito pool | 🔴 High | M | Mitigated by checklist |
| R-007 | AES key wrong format | 🔴 High | L | Mitigated by checklist |
| R-008 | eu-west-1 hardcoded bug | 🟠 Medium | H | ⚠️ Code fix required first |
| R-009 | Digilux ARN as default bug | 🟠 Medium | H | ⚠️ Code fix required first |
| R-010 | Cold start latency | 🟡 Low | H | Accepted / optional mitigation |
| R-011 | Cross-region latency | 🟡 Low | H | Accepted — same as production |
| R-012 | LWA token refresh failure | 🟠 Medium | L | Handled by existing code |
| R-013 | Alexa skill certification | 🟠 Medium | M | Parallel track to deployment |
| R-014 | Echo on wrong Amazon account | 🟠 Medium | M | Mitigated by verification step |
| R-015 | AES key rotation risk | 🟠 Medium | L | Policy control |
| R-016 | IAM overly permissive | 🟠 Medium | M | Mitigated by IAM review checklist |

**All High severity risks are fully mitigated by the deployment checklist. Two Medium risks (R-008, R-009) require code fixes that must be applied before deployment — these are documented and the fixes are already written.**

