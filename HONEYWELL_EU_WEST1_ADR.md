# Architecture Decision Record
## ADR-001: Alexa Smart Home Lambda Must Be Deployed in eu-west-1 for Indian Users

**Status:** Accepted  
**Date:** 2026-08-14  
**Authors:** Digilux Engineering  
**Audience:** Honeywell Technology Architecture, Cyber Security, Cloud Architecture  

---

## 1. Context

Honeywell is deploying an Alexa Smart Home integration that allows Indian users (locale: English (IN)) to control smart home devices via Alexa voice commands. The question under review is:

> **In which AWS region must the Alexa Smart Home Lambda function be deployed?**

This is not a preference or a performance optimisation. It is a hard routing requirement imposed by Amazon's Alexa infrastructure.

---

## 2. Amazon's Alexa Smart Home Routing Architecture

Amazon operates separate Alexa infrastructure clusters per geographic region. When a user issues a voice command, Alexa routes the resulting directive to the Lambda endpoint that is registered for the **locale that matches the user's device**.

Amazon's own documentation defines the following mandatory routing:

| Alexa Skill Locale | Required Lambda Region |
|--------------------|------------------------|
| en-US, en-CA | `us-east-1` |
| **en-IN, en-GB** | **`eu-west-1`** |
| ja-JP | `ap-northeast-1` |

This routing is enforced at the Alexa platform level. It is not configurable. If the Lambda is deployed in `us-east-1` and the skill targets `en-IN`:

- `AcceptGrant` (account linking) MAY still arrive at `us-east-1` — because this originates from the Alexa mobile app, not from an Echo device
- `Discover` (device discovery) will NOT arrive at `us-east-1` — because this originates from an Alexa device in the EN-IN locale
- `TurnOn`, `TurnOff`, and all other voice directives will NOT arrive at `us-east-1`

**Result: Indian users will never be able to control devices via voice if the Lambda is in `us-east-1`.**

---

## 3. Decision

**Deploy the Alexa Smart Home Lambda in `eu-west-1`.**

This is not optional. It is the only region that Amazon accepts for EN-IN locale Smart Home skills.

---

## 4. Evidence

### 4.1 Production Reference (Digilux)

The same Alexa Smart Home Lambda codebase has been running in `eu-west-1` in the Digilux production environment (AWS account 986906626244) since **2026-07-20**. Indian Digilux users use it daily.

Production Lambda ARN:
```
arn:aws:lambda:eu-west-1:986906626244:function:alexa
```

Production Lambda configuration (from backup dated 2026-08-08):
```json
{
  "ALEXA_GATEWAY_URL": "https://api.eu.amazonalexa.com/v3/events",
  "IOT_DATA_ENDPOINT": "https://a2yxnt6tjmcgb1-ats.iot.ap-south-1.amazonaws.com",
  "DATA_REGION": "ap-south-1",
  "LWA_SECRET_REGION": "eu-west-1",
  "ENDPOINT_KEY_REGION": "eu-west-1"
}
```

Note: `ALEXA_GATEWAY_URL` points to `api.eu.amazonalexa.com` — the EU Alexa event gateway. This confirms Amazon expects EU-region endpoints for EN-IN locale.

### 4.2 Controlled Experiment (2026-08-13)

A controlled experiment was conducted on an isolated private AWS account (715245063010) to prove the routing behaviour.

**Experiment setup:**
- Alexa Developer Console logged in as Indian user (`mahesh.maney@yahoo.com`)
- Simulator locale set to `English (IN)`
- Test A: Lambda deployed in `us-east-1`
- Test B: Lambda deployed in `eu-west-1`

**Results:**

| Directive | us-east-1 Lambda invocations | eu-west-1 Lambda invocations |
|-----------|------------------------------|------------------------------|
| AcceptGrant (account link) | 2 ✅ | 0 |
| Discover (find devices) | **0 ❌** | **3 ✅** |
| TurnOn | **0 ❌** | **2 ✅** |
| TurnOff | **0 ❌** | **1 ✅** |

**Conclusion:** With `us-east-1`, voice commands from Indian users produce zero Lambda invocations. With `eu-west-1`, all directives arrive correctly.

CloudWatch evidence is preserved in Section 4.3.

### 4.3 CloudWatch Log Evidence

**eu-west-1 — Discovery (2026-08-13T18:13:15Z):**
```
START RequestId: add7c1d5-6ba3-4a89-ab26-000f16967752
[INFO] Alexa directive: Alexa.Discovery/Discover
[INFO] Discover for userId=d4b8b448-9011-70e0-a876-462bccd647cf
[INFO] Discover: 1 total endpoints for userId=d4b8b448-9011-70e0-a876-462bccd647cf
REPORT Duration: 1450.09 ms  Memory: 115 MB
```

**eu-west-1 — TurnOn (2026-08-13T18:14:29Z):**
```
START RequestId: 4b26e015-19cc-45ba-b1fa-b36c67b9b6a4
[INFO] Alexa directive: Alexa.PowerController/TurnOn
[INFO] IoT publish topic=iot/device/duid-lightswitch-001/request
       payload={"cmdType": 131, "msgPayload": {"state": {"onOff": 1}}}
[INFO] PowerController TurnOn duid=duid-lightswitch-001 mac=0xaabbccddee112233 epid=1
REPORT Duration: 748.62 ms  Memory: 115 MB
```

**eu-west-1 — TurnOff (2026-08-13T18:15:12Z):**
```
START RequestId: c173301d-922f-4273-9488-2c0c9478ba08
[INFO] Alexa directive: Alexa.PowerController/TurnOff
[INFO] IoT publish topic=iot/device/duid-lightswitch-001/request
       payload={"cmdType": 132, "msgPayload": {"state": {"onOff": 0}}}
[INFO] PowerController TurnOff duid=duid-lightswitch-001 mac=0xaabbccddee112233 epid=1
REPORT Duration: 440.63 ms  Memory: 115 MB
```

**us-east-1 — Zero invocations for voice commands (same session):**
```
Last log event: 2026-08-13T17:45:05Z (AcceptGrant only)
No Discovery, TurnOn, or TurnOff directives ever received.
```

---

## 5. Consequences

### What changes
- The Alexa Smart Home Lambda is deployed in `eu-west-1` in Honeywell's AWS account
- Secrets Manager secrets used by this Lambda (LWA credentials, endpoint encryption key) are created in `eu-west-1`
- IAM role permissions cover `eu-west-1` Secrets Manager ARNs
- The Alexa Developer Console skill endpoint points to the `eu-west-1` Lambda ARN
- CloudWatch logs for voice commands are visible in `eu-west-1`

### What does NOT change
- DynamoDB tables remain in whatever region Honeywell chooses (ap-south-1 recommended) — controlled by the `DATA_REGION` env var
- IoT Core remains in the device's home region — controlled by `IOT_DATA_ENDPOINT` env var
- Cognito User Pool remains in Honeywell's primary region — controlled by `COGNITO_REGION` env var
- The 6 App-to-App Lambdas (start, complete, callback, unlink, status, skill_events) are not affected — they remain in ap-south-1 or Honeywell's preferred region

### Performance
Cross-region latency between `eu-west-1` (Lambda) and `ap-south-1` (DynamoDB/IoT) is 80–120ms per call. This is identical to the existing Digilux production setup and has been in use since July 2026 without any reported latency issues.

---

## 6. Alternatives Considered

### Alternative A: us-east-1
Rejected. Amazon does not route EN-IN Alexa directives to `us-east-1`. Voice commands from Indian users will fail silently. Proven by experiment on 2026-08-13.

### Alternative B: Custom Alexa Skill (non-Smart Home)
Rejected by Honeywell management. Smart Home skill is tried, tested, and receives automatic Amazon updates. Custom skill requires manual implementation of all device control logic and does not benefit from Amazon's Smart Home ecosystem updates.

### Alternative C: ap-south-1
Not supported by Amazon for Smart Home skills. Only `us-east-1`, `eu-west-1`, and `ap-northeast-1` are valid Smart Home Lambda regions.

---

## 7. Sign-off

| Role | Name | Signature | Date |
|------|------|-----------|------|
| Technology Architect | | | |
| Cloud Architect | | | |
| Cyber Security | | | |
| Engineering Lead | | | |

