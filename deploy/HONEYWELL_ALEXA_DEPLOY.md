# Honeywell QA — Alexa Deploy Runbook

## Files to Share with Mrinal

| File | Why |
|---|---|
| `deploy/env.honeywell.qa.json` | Shows every value being deployed — he must verify correctness and fill the two blanks below |
| `deploy/deploy_alexa.py` | So he can review what the script does before giving credentials |
| `deploy/deploy_common.py` | Shared utilities imported by deploy_alexa.py |

### Two blanks Mrinal must address before proceeding

```json
"COGNITO_CLIENT_SECRET": "",   // client secret for 43jurrip0q1lrhbl1i6pgs74po — Mrinal provides
"PKCE_STATE_SECRET_ARN":  "",  // intentionally blank — auto-created by the script on first run, do NOT fill
```

> `COGNITO_CLIENT_SECRET` is not required for the Alexa deploy (Alexa uses LWA, not Cognito OAuth) so the deploy won't fail without it — but Mrinal should confirm whether it's needed in the Honeywell environment.

---

## Phase 0 — Before You Touch Anything (Mrinal's Side)

Mrinal must provide:
1. AWS Access Key + Secret Key for account `316336725141`
2. Confirm `env.honeywell.qa.json` values are correct — especially table names, Skill ID, Cognito pool

---

## Phase 1 — Configure AWS Credentials Locally

```bash
aws configure --profile honeywell-qa
# AWS Access Key ID:     <from Mrinal>
# AWS Secret Access Key: <from Mrinal>
# Default region name:   us-east-1
# Default output format: json
```

Verify it points to the right account:

```bash
aws sts get-caller-identity --profile honeywell-qa
# Must show: "Account": "316336725141"
```

---

## Phase 2 — Dry-Run (Zero Writes — Tests Permissions Only)

```bash
python3 deploy/deploy_alexa.py \
  --config deploy/env.honeywell.qa.json \
  --profile honeywell-qa \
  --dry-run
```

An HTML report opens in the browser automatically. **Do not proceed to Phase 3 unless the report shows all green for:**

- All 8 service access probes: Lambda (us-east-1), Lambda (eu-west-1), IAM, DynamoDB, KMS, API Gateway, Secrets Manager, CloudWatch Logs
- All 4 referenced resources: endpoint key secret, device table secret, `create_and_update_data_function` Lambda, `digilux-metadata-bucket-honeywell-p`

If anything is red, the report prints the exact AWS CLI command to grant the missing permission.

---

## Phase 3 — Full Deploy

```bash
python3 deploy/deploy_alexa.py \
  --config deploy/env.honeywell.qa.json \
  --profile honeywell-qa
```

This runs all 12 steps and creates everything from scratch:

| What | Detail |
|---|---|
| DDB tables (3) | `alexa_app_linking_sessions`, `honeywell_alexa_lwa_tokens`, `alexa_pkce_sessions` |
| KMS key | `alias/honeywell-alexa-tokens` |
| IAM execution roles | 10 (one per Lambda) |
| Lambda functions | 10 (9 × us-east-1, 1 × eu-west-1) |
| API Gateway routes | 6 methods + CORS on existing API `3uch7iu3k5` |
| Stage redeploy | `api` stage |
| Smoke tests | Basic reachability on each route |
| PKCE state secret | Auto-created; **copy the ARN printed in the output back into `env.honeywell.qa.json`** |

The script opens an HTML report on completion. **Note the ARN of `alexa_fulfillment` (eu-west-1) from the report — needed in Phase 4.**

---

## Phase 4 — Manual Steps (After Deploy, Before Verify)

These cannot be automated. Run in this order.

### Step 4a — Alexa Resource Policy
Allows Alexa cloud to invoke `alexa_fulfillment`. You can run this (just needs Honeywell credentials):

```bash
aws lambda add-permission \
  --function-name alexa_fulfillment \
  --statement-id alexa-smart-home \
  --action lambda:InvokeFunction \
  --principal alexa-connectedhome.amazon.com \
  --event-source-token amzn1.ask.skill.ebdd110e-1700-4eb9-9195-f7b81fd032d7 \
  --region eu-west-1 \
  --profile honeywell-qa
```

### Step 4b — DDB Streams + Event Source Mappings
Enables real-time device state push to Alexa. You can run this:

```bash
# Enable stream on user_device_details
aws dynamodb update-table \
  --table-name user_device_details \
  --stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES \
  --region us-east-1 --profile honeywell-qa

# Get the stream ARN
STREAM_ARN=$(aws dynamodbstreams list-streams \
  --table-name user_device_details \
  --region us-east-1 --profile honeywell-qa \
  --query 'Streams[0].StreamArn' --output text)

# Wire user_device_details → alexa_proactive_discovery
aws lambda create-event-source-mapping \
  --event-source-arn $STREAM_ARN \
  --function-name alexa_proactive_discovery \
  --starting-position LATEST \
  --batch-size 10 \
  --region us-east-1 --profile honeywell-qa

# Get device_state stream ARN
STATE_STREAM_ARN=$(aws dynamodbstreams list-streams \
  --table-name device_state \
  --region us-east-1 --profile honeywell-qa \
  --query 'Streams[0].StreamArn' --output text)

# Wire device_state → alexa_state_reporter
aws lambda create-event-source-mapping \
  --event-source-arn $STATE_STREAM_ARN \
  --function-name alexa_state_reporter \
  --starting-position LATEST \
  --batch-size 10 \
  --region us-east-1 --profile honeywell-qa
```

### Step 4c — Cognito Callback URL (Mrinal — AWS Console)

> AWS Console → Cognito → User Pool `us-east-1_DuegVptxS` → App clients → `2621qfnvrqnis9h1tfjs90lnkf` (digilux-alexa-client) → Add allowed callback URL:
>
> `https://api.nucleusqa.honeywell.com/api/alexa/cognito/callback`

This is required for Stage 2 of the Alexa app-to-app flow. Without it, Cognito will reject the redirect and linking will fail.

### Step 4d — Alexa Skill Endpoint (Mrinal — Alexa Developer Console)

> Alexa Developer Console → Skill `amzn1.ask.skill.ebdd110e-1700-4eb9-9195-f7b81fd032d7` → Smart Home → Default endpoint → paste the `alexa_fulfillment` Lambda ARN from Phase 3 output (eu-west-1 ARN)

---

## Phase 5 — Verify-Only (After All Manual Steps)

```bash
python3 deploy/deploy_alexa.py \
  --config deploy/env.honeywell.qa.json \
  --profile honeywell-qa \
  --verify-only
```

Runs checks C–G against the live environment (zero writes) and opens the HTML report. **All of sections C, D, E, F must be green before handing off for QA testing.**

| Section | Checks |
|---|---|
| C | All 10 Lambdas in Active state with correct execution roles |
| D | `alexa_fulfillment` has Alexa resource policy attached |
| E | DDB streams enabled + ESMs wired to `alexa_state_reporter` and `alexa_proactive_discovery` |
| F | Every Lambda's live env vars match what the config expects (zero missing vars) |
| G | Manual checklist reminder (Cognito callback, Alexa Dev Console, skill endpoint) |

---

## Summary Checklist

```
[ ] 1.  Mrinal provides AWS creds + confirms env.honeywell.qa.json values
[ ] 2.  aws configure --profile honeywell-qa
[ ] 3.  aws sts get-caller-identity  →  confirms account 316336725141
[ ] 4.  --dry-run  →  all A+B green in HTML report
[ ] 5.  Full deploy  →  10/10 Lambdas deployed, HTML report generated
[ ] 6.  Copy PKCE_STATE_SECRET_ARN from deploy output → update env.honeywell.qa.json
[ ] 7.  Step 4a: alexa_fulfillment resource policy  (you — CLI)
[ ] 8.  Step 4b: DDB streams + ESMs  (you — CLI)
[ ] 9.  Step 4c: Cognito callback URL  (Mrinal — AWS Console)
[ ] 10. Step 4d: Alexa skill endpoint ARN  (Mrinal — Alexa Dev Console)
[ ] 11. --verify-only  →  all C+D+E+F green in HTML report
[ ] 12. QA test: link Alexa from Honeywell Nucleus app
```
