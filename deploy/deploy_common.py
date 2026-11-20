#!/usr/bin/env python3
"""
deploy_common.py — Shared utilities for deploy_google.py, deploy_alexa.py, deploy_alexa_ireland.py

Not intended to be run directly.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3")
    sys.exit(1)

# ─── Logging ──────────────────────────────────────────────────────────────────

_step_tag = ""


def set_step(tag: str):
    global _step_tag
    _step_tag = tag


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _log(level: str, msg: str):
    tag = f"[{_step_tag}]  " if _step_tag else ""
    print(f"[{_ts()}] [{level:<7}]  {tag}{msg}", flush=True)


def log_info(m):   _log("INFO",   m)
def log_ok(m):     _log("OK",     m)
def log_skip(m):   _log("SKIP",   m)
def log_create(m): _log("CREATE", m)
def log_update(m): _log("UPDATE", m)
def log_warn(m):   _log("WARN",   m)
def log_error(m):  _log("ERROR",  m)


def step_header(n: int, name: str):
    tag = f"STEP {n} — {name}"
    set_step(tag)
    print(f"\n{'━' * 68}")
    print(f"  Step {n} — {name}")
    print(f"{'━' * 68}\n", flush=True)


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    if not os.path.exists(config_path):
        log_error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def cfg(c: dict, key: str, fallback: str = "") -> str:
    return str(c.get(key) or fallback)


def check_no_fill_in(c: dict, script_name: str):
    bad = [k for k, v in c.items() if v == "FILL_IN"]
    if bad:
        log_error(f"Config has {len(bad)} FILL_IN placeholder(s) in: {', '.join(bad)}")
        log_error(f"Fill in all FILL_IN values in the config before running {script_name}")
        sys.exit(1)
    log_ok("No FILL_IN placeholders found")


def check_required_keys(c: dict, required: list):
    missing = []
    for k in required:
        v = c.get(k, "")
        if not v:
            missing.append(k)
        else:
            log_ok(f"  {k:<46} = {str(v)[:60]}")
    if missing:
        log_error(f"Missing or empty required config keys: {', '.join(missing)}")
        sys.exit(1)


# ─── AWS Helpers ──────────────────────────────────────────────────────────────

def make_client(session, service: str, region: str = None):
    if region:
        return session.client(service, region_name=region)
    return session.client(service)


def preflight_aws(session) -> str:
    """Verify AWS credentials. Returns account ID."""
    try:
        sts = make_client(session, "sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        log_ok(f"AWS credentials valid  account={account_id}  region={session.region_name}")
        return account_id
    except (NoCredentialsError, ClientError) as e:
        log_error(f"AWS credentials check failed: {e}")
        sys.exit(1)


def verify_secret_accessible(session, secret_arn: str, secret_region: str, label: str):
    sm = make_client(session, "secretsmanager", secret_region)
    try:
        resp = sm.describe_secret(SecretId=secret_arn)
        log_ok(f"{label} accessible  name={resp.get('Name', secret_arn)}")
    except ClientError as e:
        log_error(f"{label} NOT accessible: {e}")
        sys.exit(1)


# ─── DynamoDB ─────────────────────────────────────────────────────────────────

def ensure_table(ddb, table_name: str, pk: str, ttl_attr: str = None) -> bool:
    """Create table if not exists. PAY_PER_REQUEST. Returns True if created."""
    try:
        ddb.describe_table(TableName=table_name)
        log_skip(f"Table {table_name} already exists")
        if ttl_attr:
            _ensure_ttl(ddb, table_name, ttl_attr)
        return False
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    log_create(f"Table {table_name} not found — creating  pk={pk}  billing=PAY_PER_REQUEST")
    ddb.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": pk, "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    log_info(f"Waiting for {table_name} to become ACTIVE...")
    ddb.get_waiter("table_exists").wait(TableName=table_name,
                                        WaiterConfig={"Delay": 3, "MaxAttempts": 40})
    log_ok(f"Table {table_name} ACTIVE")
    if ttl_attr:
        _ensure_ttl(ddb, table_name, ttl_attr)
    return True


def _ensure_ttl(ddb, table_name: str, attr: str):
    try:
        ttl = ddb.describe_time_to_live(TableName=table_name)
        status = ttl.get("TimeToLiveDescription", {}).get("TimeToLiveStatus", "DISABLED")
        if status in ("ENABLED", "ENABLING"):
            log_skip(f"TTL already {status} on {table_name}")
            return
    except ClientError:
        pass
    log_info(f"Enabling TTL on {table_name} (attribute: {attr})")
    try:
        ddb.update_time_to_live(TableName=table_name,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": attr})
        log_ok(f"TTL enabled on {table_name}")
    except ClientError as e:
        log_warn(f"TTL update on {table_name}: {e}")


def ensure_gsi(ddb, table_name: str, gsi_name: str, pk_attr: str):
    """Add a GSI to a table if it doesn't already exist."""
    log_info(f"Checking GSI: {gsi_name} on {table_name}")
    desc = ddb.describe_table(TableName=table_name)["Table"]
    existing = {g["IndexName"] for g in desc.get("GlobalSecondaryIndexes", [])}
    if gsi_name in existing:
        log_skip(f"GSI {gsi_name} already exists on {table_name}")
        return
    log_create(f"Adding GSI {gsi_name} to {table_name}  pk={pk_attr}")
    ddb.update_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": pk_attr, "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[{"Create": {
            "IndexName": gsi_name,
            "KeySchema": [{"AttributeName": pk_attr, "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }}],
    )
    log_info(f"Waiting for GSI {gsi_name} to become ACTIVE...")
    for _ in range(40):
        time.sleep(5)
        try:
            d2 = ddb.describe_table(TableName=table_name)["Table"]
            for g in d2.get("GlobalSecondaryIndexes", []):
                if g["IndexName"] == gsi_name and g.get("IndexStatus") == "ACTIVE":
                    log_ok(f"GSI {gsi_name} ACTIVE on {table_name}")
                    return
        except ClientError:
            pass
    log_warn(f"GSI {gsi_name} may not be ACTIVE yet — check manually")


# ─── IAM ──────────────────────────────────────────────────────────────────────

_TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]
})


def ensure_role(iam, role_name: str, inline_policy: dict) -> tuple:
    """Get-or-create IAM role, always overwrite inline policy. Returns (was_created, role_arn)."""
    log_info(f"Checking role: {role_name}")
    created = False
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        log_skip(f"Role exists — updating policy  arn={role_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        log_create(f"Role not found — creating {role_name}...")
        role_arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=_TRUST_POLICY,
            Description=f"deploy-managed role {role_name}",
        )["Role"]["Arn"]
        log_ok(f"Role created  arn={role_arn}")
        log_info("Sleeping 10s for IAM propagation...")
        time.sleep(10)
        created = True

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=f"{role_name}-policy",
        PolicyDocument=json.dumps(inline_policy),
    )
    log_ok(f"Role policy applied  arn={role_arn}")
    return created, role_arn


# ─── IAM Policy Statement Helpers ─────────────────────────────────────────────

def _stmt(effect, actions, resources):
    return {"Effect": effect, "Action": actions, "Resource": resources}


def st_cw(account_id, region):
    return _stmt("Allow",
                 ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                 f"arn:aws:logs:{region}:{account_id}:log-group:/aws/lambda/*")


def st_xray():
    return _stmt("Allow", ["xray:PutTraceSegments", "xray:PutTelemetryRecords"], "*")


def st_ddb(actions, account_id, region, *tables, idx=False):
    arns = []
    for t in tables:
        arns.append(f"arn:aws:dynamodb:{region}:{account_id}:table/{t}")
        if idx:
            arns.append(f"arn:aws:dynamodb:{region}:{account_id}:table/{t}/index/*")
    return _stmt("Allow", actions, arns)


def st_sm(*secret_arns):
    return _stmt("Allow", ["secretsmanager:GetSecretValue"], list(secret_arns))


def st_s3(bucket):
    return _stmt("Allow", ["s3:GetObject", "s3:ListBucket"],
                 [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"])


def st_kms(key_arn, actions=None):
    return _stmt("Allow", actions or ["kms:Decrypt", "kms:GenerateDataKey"], key_arn)


def st_iot(account_id, region):
    return _stmt("Allow", ["iot:Publish"],
                 f"arn:aws:iot:{region}:{account_id}:topic/iot/device/*")


def st_stream(account_id, region, table_name):
    return _stmt("Allow",
                 ["dynamodb:GetRecords", "dynamodb:GetShardIterator",
                  "dynamodb:DescribeStream", "dynamodb:ListStreams"],
                 f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}/stream/*")


def make_policy(*statements):
    return {"Version": "2012-10-17", "Statement": list(statements)}


# ─── Deploy Report ─────────────────────────────────────────────────────────────

import html as _html_escape
import webbrowser as _webbrowser


class DeployReport:
    """Collects per-step check results throughout a deploy run and renders an HTML report."""

    _BADGE = {
        "PASS": ("PASS", "badge-pass"),
        "FAIL": ("FAIL", "badge-fail"),
        "WARN": ("WARN", "badge-warn"),
        "SKIP": ("SKIP", "badge-skip"),
        "INFO": ("INFO", "badge-info"),
    }

    def __init__(self, env_label: str, account_id: str, region: str, fulfillment_region: str):
        self.env_label          = env_label
        self.account_id         = account_id
        self.region             = region
        self.fulfillment_region = fulfillment_region
        self.started_at         = datetime.now()
        self.finished_at        = None
        self._sections: list    = []
        self._fix_commands: list = []   # [{description, command}]
        self._manual_items: list = []   # [str]
        self._current_section: dict | None = None

    # ── Section / check API ───────────────────────────────────────────────────

    def begin_section(self, section_id: str, section_name: str):
        self._current_section = {"id": section_id, "name": section_name, "checks": []}
        self._sections.append(self._current_section)

    def add_check(self, name: str, status: str, detail: str, fix_command: str = ""):
        if self._current_section is None:
            self.begin_section("misc", "Miscellaneous")
        self._current_section["checks"].append({
            "name": name, "status": status.upper(),
            "detail": detail, "fix_command": fix_command,
        })

    def add_fix_command(self, description: str, command: str):
        self._fix_commands.append({"description": description, "command": command})

    def add_manual_item(self, item: str):
        self._manual_items.append(item)

    def finish(self):
        self.finished_at = datetime.now()

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _stats(self) -> dict:
        counts: dict = {}
        for s in self._sections:
            for c in s["checks"]:
                counts[c["status"]] = counts.get(c["status"], 0) + 1
        return counts

    def has_failures(self) -> bool:
        return self._stats().get("FAIL", 0) > 0

    # ── HTML rendering ────────────────────────────────────────────────────────

    def render_html(self, output_path: str) -> str:
        """Render the report to an HTML file and return the absolute path."""
        self.finish()
        stats   = self._stats()
        elapsed = ""
        if self.finished_at:
            delta   = int((self.finished_at - self.started_at).total_seconds())
            elapsed = f"{delta // 60}m {delta % 60}s"

        # ── Build section rows ────────────────────────────────────────────────
        sections_html = ""
        for section in self._sections:
            rows = ""
            for chk in section["checks"]:
                st       = chk["status"]
                badge_cls = self._BADGE.get(st, ("?", "badge-info"))[1]
                fix_html  = ""
                if chk["fix_command"]:
                    esc = _html_escape.escape(chk["fix_command"])
                    fix_html = f'<div class="inline-fix"><code>{esc}</code></div>'
                rows += (
                    f'<tr>'
                    f'<td class="col-name">{_html_escape.escape(chk["name"])}</td>'
                    f'<td class="col-status"><span class="badge {badge_cls}">{st}</span></td>'
                    f'<td class="col-detail">{_html_escape.escape(chk["detail"])}{fix_html}</td>'
                    f'</tr>'
                )
            open_attr = " open" if any(c["status"] in ("FAIL", "WARN") for c in section["checks"]) else " open"
            sections_html += (
                f'<details{open_attr}>'
                f'<summary class="sec-title">{_html_escape.escape(section["name"])}</summary>'
                f'<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>'
                f'</details>'
            )

        # ── Build fix commands block ──────────────────────────────────────────
        fix_html = ""
        if self._fix_commands:
            blocks = ""
            for fc in self._fix_commands:
                safe_cmd = _html_escape.escape(fc["command"])
                # JS-safe version for clipboard (escape backticks)
                js_cmd = fc["command"].replace("\\", "\\\\").replace("`", "\\`").replace("'", "\\'")
                blocks += (
                    f'<div class="fix-block">'
                    f'<div class="fix-label">{_html_escape.escape(fc["description"])}</div>'
                    f'<pre class="fix-pre">{safe_cmd}</pre>'
                    f'<button class="copy-btn" onclick="copyCmd(this, `{js_cmd}`)">Copy</button>'
                    f'</div>'
                )
            fix_html = (
                f'<details open>'
                f'<summary class="sec-title sec-fix">&#9888; Fix Commands — Run These to Resolve All Gaps</summary>'
                f'{blocks}'
                f'</details>'
            )

        # ── Build manual checklist ────────────────────────────────────────────
        manual_html = ""
        if self._manual_items:
            items = "".join(f'<li>{_html_escape.escape(item)}</li>' for item in self._manual_items)
            manual_html = (
                f'<details open>'
                f'<summary class="sec-title sec-manual">&#128221; Manual Checklist (Mrinal / Alexa Dev Console)</summary>'
                f'<ul class="manual-list">{items}</ul>'
                f'</details>'
            )

        # ── Overall status ────────────────────────────────────────────────────
        n_fail = stats.get("FAIL", 0)
        n_warn = stats.get("WARN", 0)
        if n_fail:
            overall_cls, overall_txt = "overall-fail", f"&#10008; FAILED ({n_fail} failure(s))"
        elif n_warn:
            overall_cls, overall_txt = "overall-warn", f"&#9888; WARNINGS ({n_warn} warning(s))"
        else:
            overall_cls, overall_txt = "overall-pass", "&#10004; SUCCESS"

        # ── CSS (all inline — no external deps) ──────────────────────────────
        css = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f1f5f9;color:#1e293b;font-size:14px;line-height:1.5}
header{background:#0f172a;color:#f8fafc;padding:28px 36px}
header h1{font-size:22px;font-weight:700;margin-bottom:6px}
.meta{font-size:12px;color:#94a3b8;margin-top:3px}
.container{max-width:1200px;margin:28px auto;padding:0 24px}
.summary-bar{display:flex;gap:14px;margin-bottom:28px;align-items:center;flex-wrap:wrap}
.stat{padding:14px 22px;border-radius:10px;text-align:center;min-width:90px}
.stat .num{font-size:30px;font-weight:800}
.stat .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.8px;margin-top:2px}
.s-pass{background:#dcfce7;color:#15803d}
.s-fail{background:#fee2e2;color:#b91c1c}
.s-warn{background:#fef9c3;color:#a16207}
.s-skip{background:#e2e8f0;color:#475569}
.overall{padding:12px 22px;border-radius:10px;font-weight:700;font-size:15px}
.overall-pass{background:#dcfce7;color:#15803d}
.overall-fail{background:#fee2e2;color:#b91c1c}
.overall-warn{background:#fef9c3;color:#a16207}
details{background:#fff;border-radius:10px;margin-bottom:18px;
  box-shadow:0 1px 4px rgba(0,0,0,.07);overflow:hidden}
summary.sec-title{cursor:pointer;padding:14px 20px;font-weight:600;font-size:14px;
  background:#f8fafc;border-bottom:1px solid #e2e8f0;user-select:none;
  list-style:none;display:flex;align-items:center;gap:8px}
summary.sec-title::marker{display:none}
summary.sec-title::before{content:"▶";font-size:10px;color:#94a3b8;
  transition:transform .2s;display:inline-block}
details[open]>summary.sec-title::before{transform:rotate(90deg)}
.sec-fix{background:#fff5f5;color:#b91c1c}
.sec-manual{background:#f0fdf4;color:#15803d}
table{width:100%;border-collapse:collapse}
th{background:#f8fafc;text-align:left;padding:10px 16px;font-size:11px;
  text-transform:uppercase;letter-spacing:.5px;color:#64748b;
  border-bottom:1px solid #e2e8f0;white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbfc}
.col-name{font-weight:500;white-space:nowrap;width:220px}
.col-status{width:80px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;
  font-size:11px;font-weight:700;letter-spacing:.4px;white-space:nowrap}
.badge-pass{background:#dcfce7;color:#15803d}
.badge-fail{background:#fee2e2;color:#b91c1c}
.badge-warn{background:#fef9c3;color:#a16207}
.badge-skip{background:#e2e8f0;color:#475569}
.badge-info{background:#dbeafe;color:#1d4ed8}
.inline-fix{margin-top:6px;background:#fef9c3;border-radius:5px;
  padding:6px 10px;font-family:monospace;font-size:12px;
  white-space:pre-wrap;word-break:break-all;color:#78350f}
.fix-block{padding:18px 22px;border-bottom:1px solid #f1f5f9}
.fix-block:last-child{border-bottom:none}
.fix-label{font-weight:600;margin-bottom:10px;color:#374151;font-size:13px}
.fix-pre{background:#0f172a;color:#a5f3fc;border-radius:8px;
  padding:16px 18px;font-family:'Courier New',monospace;font-size:12px;
  overflow-x:auto;white-space:pre-wrap;word-break:break-all;line-height:1.7}
.copy-btn{margin-top:10px;padding:5px 14px;background:#3b82f6;color:#fff;
  border:none;border-radius:5px;font-size:12px;cursor:pointer;font-weight:500}
.copy-btn:hover{background:#2563eb}
.copy-btn.copied{background:#16a34a}
.manual-list{padding:16px 24px 16px 42px}
.manual-list li{margin-bottom:8px;line-height:1.6}
footer{text-align:center;padding:28px;color:#94a3b8;font-size:12px}
"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alexa Deploy Report — {_html_escape.escape(self.env_label)}</title>
<style>{css}</style>
</head>
<body>
<header>
  <h1>Alexa Deployment Report</h1>
  <div class="meta">ENV: <strong>{_html_escape.escape(self.env_label)}</strong>
    &nbsp;|&nbsp; Account: {_html_escape.escape(self.account_id)}
    &nbsp;|&nbsp; Region: {_html_escape.escape(self.region)} / {_html_escape.escape(self.fulfillment_region)}</div>
  <div class="meta">Started: {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}
    &nbsp;|&nbsp; Duration: {elapsed}</div>
</header>
<div class="container">
  <div class="summary-bar">
    <div class="stat s-pass"><div class="num">{stats.get('PASS',0)}</div><div class="lbl">Pass</div></div>
    <div class="stat s-fail"><div class="num">{stats.get('FAIL',0)}</div><div class="lbl">Fail</div></div>
    <div class="stat s-warn"><div class="num">{stats.get('WARN',0)}</div><div class="lbl">Warn</div></div>
    <div class="stat s-skip"><div class="num">{stats.get('SKIP',0)}</div><div class="lbl">Skip</div></div>
    <div class="overall {overall_cls}">{overall_txt}</div>
  </div>
  {sections_html}
  {fix_html}
  {manual_html}
</div>
<footer>Generated by deploy_alexa.py &nbsp;·&nbsp; {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}</footer>
<script>
function copyCmd(btn, text) {{
  navigator.clipboard.writeText(text).then(function() {{
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(function() {{ btn.textContent = 'Copy'; btn.classList.remove('copied'); }}, 2000);
  }});
}}
</script>
</body>
</html>"""

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        log_ok(f"HTML report written → {output_path}")
        return os.path.abspath(output_path)

    def open_in_browser(self, path: str):
        abs_path = os.path.abspath(path)
        log_info(f"Opening report in browser: file://{abs_path}")
        _webbrowser.open(f"file://{abs_path}")


# ─── Pre-Deploy Permission Checks ─────────────────────────────────────────────

# Actions needed per service (used to build consolidated IAM fix policy)
_DEPLOYER_ACTIONS = {
    "lambda": [
        "lambda:CreateFunction", "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration", "lambda:GetFunction",
        "lambda:GetFunctionConfiguration", "lambda:AddPermission",
        "lambda:RemovePermission", "lambda:ListFunctions",
    ],
    "iam": [
        "iam:CreateRole", "iam:GetRole", "iam:PutRolePolicy",
        "iam:GetRolePolicy", "iam:AttachRolePolicy", "iam:PassRole",
        "iam:ListRoles", "iam:ListAttachedRolePolicies",
    ],
    "dynamodb": [
        "dynamodb:CreateTable", "dynamodb:DescribeTable",
        "dynamodb:UpdateTable", "dynamodb:UpdateTimeToLive", "dynamodb:ListTables",
    ],
    "kms": [
        "kms:CreateKey", "kms:CreateAlias", "kms:ListAliases",
        "kms:DescribeKey", "kms:EnableKeyRotation",
    ],
    "apigateway": [
        "apigateway:GET", "apigateway:POST",
        "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE",
    ],
    "secretsmanager": [
        "secretsmanager:CreateSecret", "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
        "secretsmanager:ListSecrets",
    ],
    "logs": [
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
        "logs:PutRetentionPolicy", "logs:DescribeLogGroups",
    ],
}


def check_service_access(session, main_region: str, fulfillment_region: str,
                         report: DeployReport, profile: str = "") -> bool:
    """
    Probe each required AWS service with a lightweight read call.
    Collects ALL failures, then builds one consolidated IAM policy + CLI command.
    Returns True if all probes pass.
    """
    report.begin_section("A", "A — Deployer Service Access Probes")
    log_info("Probing required AWS services for deployer access...")

    # (label, service_name, region, service_key, probe_fn)
    probes = [
        ("Lambda",           "lambda",         main_region,         "lambda",         lambda c: c.list_functions(MaxItems=1)),
        ("Lambda (ireland)", "lambda",         fulfillment_region,  "lambda",         lambda c: c.list_functions(MaxItems=1)),
        ("IAM",              "iam",            None,                "iam",            lambda c: c.list_roles(MaxItems=1)),
        ("DynamoDB",         "dynamodb",       main_region,         "dynamodb",       lambda c: c.list_tables(Limit=1)),
        ("KMS",              "kms",            main_region,         "kms",            lambda c: c.list_aliases(Limit=1)),
        ("API Gateway",      "apigateway",     main_region,         "apigateway",     lambda c: c.get_rest_apis(limit=1)),
        ("Secrets Manager",  "secretsmanager", main_region,         "secretsmanager", lambda c: c.list_secrets(MaxResults=1)),
        ("CloudWatch Logs",  "logs",           main_region,         "logs",           lambda c: c.describe_log_groups(limit=1)),
    ]

    failed_keys: list = []
    all_ok = True

    for label, svc, region, svc_key, probe in probes:
        region_tag = f" ({region})" if region else ""
        try:
            client = make_client(session, svc, region)
            probe(client)
            log_ok(f"  {label}{region_tag}: OK")
            report.add_check(f"{label}{region_tag}", "PASS", "Read access confirmed")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            _AUTH_CODES = {"AccessDenied", "AccessDeniedException",
                           "UnauthorizedException", "AuthorizationError",
                           "InvalidClientTokenId", "ExpiredTokenException"}
            if code in _AUTH_CODES:
                log_error(f"  {label}{region_tag}: ACCESS DENIED ({code})")
                preview = ", ".join(_DEPLOYER_ACTIONS[svc_key][:3]) + " …"
                report.add_check(f"{label}{region_tag}", "FAIL",
                                 f"AccessDenied ({code}) — needs: {preview}")
                if svc_key not in failed_keys:
                    failed_keys.append(svc_key)
                all_ok = False
            else:
                log_warn(f"  {label}{region_tag}: probe error (non-auth) — {code}")
                report.add_check(f"{label}{region_tag}", "WARN",
                                 f"Non-auth probe error: {code}")
        except Exception as exc:
            log_warn(f"  {label}{region_tag}: probe exception — {exc}")
            report.add_check(f"{label}{region_tag}", "WARN", f"Probe exception: {exc}")

    if not all_ok:
        # Build consolidated policy covering all failed services
        statements = []
        for key in failed_keys:
            resource = "arn:aws:iam::*:role/*" if key == "iam" else "*"
            statements.append({
                "Effect": "Allow",
                "Action": _DEPLOYER_ACTIONS[key],
                "Resource": resource,
            })
        policy = {"Version": "2012-10-17", "Statement": statements}

        ts          = datetime.now().strftime("%Y%m%dT%H%M%S")
        policy_path = os.path.join("deploy", f"missing_deployer_permissions_{ts}.json")
        os.makedirs("deploy", exist_ok=True)
        with open(policy_path, "w") as fh:
            json.dump(policy, fh, indent=2)

        # Detect caller type (IAM user vs assumed role)
        profile_flag = f" --profile {profile}" if profile else " --profile <HONEYWELL_PROFILE>"
        try:
            identity    = make_client(session, "sts").get_caller_identity()
            caller_arn  = identity["Arn"]
            if ":user/" in caller_arn:
                caller_name = caller_arn.split(":user/")[-1]
                cmd = (
                    f"aws iam put-user-policy \\\n"
                    f"  --user-name {caller_name} \\\n"
                    f"  --policy-name DigiluxAlexaDeploy \\\n"
                    f"  --policy-document file://{policy_path}{profile_flag}"
                )
            elif ":assumed-role/" in caller_arn:
                role_name = caller_arn.split(":assumed-role/")[1].split("/")[0]
                cmd = (
                    f"aws iam put-role-policy \\\n"
                    f"  --role-name {role_name} \\\n"
                    f"  --policy-name DigiluxAlexaDeploy \\\n"
                    f"  --policy-document file://{policy_path}{profile_flag}"
                )
            else:
                cmd = (
                    f"# Attach file://{policy_path} to your IAM identity manually\n"
                    f"# Caller ARN: {caller_arn}"
                )
        except Exception:
            cmd = (
                f"aws iam put-user-policy \\\n"
                f"  --user-name <IAM_USER> \\\n"
                f"  --policy-name DigiluxAlexaDeploy \\\n"
                f"  --policy-document file://{policy_path}{profile_flag}"
            )

        log_error(f"Policy file written → {policy_path}")
        log_error("Re-run deploy after granting permissions.")
        report.add_fix_command(
            f"Grant missing deployer permissions ({len(failed_keys)} service(s): "
            f"{', '.join(failed_keys)})",
            cmd,
        )

    return all_ok


def check_referenced_resources(session, c: dict, report: DeployReport) -> bool:
    """
    Verify that secrets, Lambdas, and S3 buckets referenced in the config exist.
    Records results in report. Returns True if all required resources are accessible.
    """
    report.begin_section("B", "B — Referenced Resource Checks")
    log_info("Checking referenced secrets, Lambdas, and S3 buckets...")
    region  = cfg(c, "AWS_REGION")
    all_ok  = True

    # ── Secrets Manager ARNs that must exist before deploy ───────────────────
    secrets_to_check = [
        ("ENDPOINT_KEY_SECRET_ARN", cfg(c, "ENDPOINT_KEY_SECRET_ARN"),
         cfg(c, "ENDPOINT_KEY_REGION") or region),
        ("DEVICE_TABLE_SECRET_ARN", cfg(c, "DEVICE_TABLE_SECRET_ARN"),
         cfg(c, "DEVICE_TABLE_SECRET_REGION") or region),
    ]
    for label, arn, sec_region in secrets_to_check:
        if not arn:
            log_warn(f"  {label}: not configured — skipped")
            report.add_check(label, "SKIP", "Not set in config — skipped")
            continue
        try:
            make_client(session, "secretsmanager", sec_region).describe_secret(SecretId=arn)
            log_ok(f"  {label}: accessible")
            report.add_check(label, "PASS", f"Secret accessible in {sec_region}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            log_error(f"  {label}: NOT accessible ({code})  arn={arn}")
            report.add_check(label, "FAIL",
                             f"{code} — secret does not exist or access denied: {arn[:70]}…")
            all_ok = False

    # ── CREATE_UPDATE_FN Lambda must exist ───────────────────────────────────
    fn_name = cfg(c, "CREATE_UPDATE_FN")
    if fn_name:
        try:
            make_client(session, "lambda", region).get_function(FunctionName=fn_name)
            log_ok(f"  CREATE_UPDATE_FN ({fn_name}): exists")
            report.add_check("CREATE_UPDATE_FN Lambda", "PASS",
                             f"{fn_name} found in {region}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            log_error(f"  CREATE_UPDATE_FN ({fn_name}): NOT found ({code})")
            report.add_check("CREATE_UPDATE_FN Lambda", "FAIL",
                             f"{fn_name} not found in {region} — Mrinal/infra team must deploy it first")
            all_ok = False

    # ── METADATA_BUCKET S3 bucket must exist ─────────────────────────────────
    bucket = cfg(c, "METADATA_BUCKET")
    if bucket:
        try:
            make_client(session, "s3", region).head_bucket(Bucket=bucket)
            log_ok(f"  METADATA_BUCKET ({bucket}): exists")
            report.add_check("METADATA_BUCKET (S3)", "PASS", f"Bucket {bucket} accessible")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            log_error(f"  METADATA_BUCKET ({bucket}): NOT accessible ({code})")
            report.add_check("METADATA_BUCKET (S3)", "FAIL",
                             f"Bucket {bucket} not found or no access — must be created by Mrinal/infra team")
            all_ok = False

    return all_ok


# ─── Lambda Packaging ─────────────────────────────────────────────────────────

def package_lambda(source_dir: str, fn_name: str, build_root: str) -> str:
    """Package a Lambda into a zip. Returns zip path."""
    zip_path = os.path.join(build_root, f"{fn_name}.zip")
    log_info(f"Packaging {fn_name} from {source_dir}")

    req_file = os.path.join(source_dir, "requirements.txt")
    deps_dir = os.path.join(build_root, f"{fn_name}_deps")

    if os.path.exists(req_file):
        if os.path.exists(deps_dir):
            shutil.rmtree(deps_dir)
        os.makedirs(deps_dir, exist_ok=True)
        log_info(f"  requirements.txt found — installing Python deps (manylinux/python3.12)...")
        cmd = [sys.executable, "-m", "pip", "install", "-r", req_file, "-t", deps_dir,
               "--platform", "manylinux2014_x86_64", "--python-version", "3.12",
               "--only-binary=:all:", "--implementation", "cp", "--quiet"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            log_warn("  manylinux wheels unavailable — retrying without platform flags...")
            cmd2 = [sys.executable, "-m", "pip", "install", "-r", req_file,
                    "-t", deps_dir, "--quiet"]
            res2 = subprocess.run(cmd2, capture_output=True, text=True)
            if res2.returncode != 0:
                log_error(f"  pip install failed:\n{res2.stderr}")
                raise RuntimeError(f"pip install failed for {fn_name}")
        size_kb = sum(os.path.getsize(os.path.join(dp, f))
                      for dp, _, files in os.walk(deps_dir)
                      for f in files) // 1024
        log_ok(f"  Deps installed  size={size_kb} KB")
    else:
        deps_dir = None

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if deps_dir and os.path.exists(deps_dir):
            for root, dirs, files in os.walk(deps_dir):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fname in files:
                    if fname.endswith(".pyc"):
                        continue
                    fp = os.path.join(root, fname)
                    zf.write(fp, os.path.relpath(fp, deps_dir))
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
            for fname in files:
                if fname.endswith(".pyc") or fname == "requirements.txt":
                    continue
                fp = os.path.join(root, fname)
                zf.write(fp, os.path.relpath(fp, source_dir))

    kb = os.path.getsize(zip_path) // 1024
    log_ok(f"  Zip ready: {zip_path}  ({kb} KB)")
    return zip_path


# ─── Lambda Deploy ────────────────────────────────────────────────────────────

def deploy_lambda(session, fn_name: str, zip_path: str, role_arn: str,
                  timeout: int, memory: int, env_vars: dict, region: str) -> str:
    """Create or update a Lambda. Returns function ARN."""
    log_info(f"Deploying {fn_name} → {region}  timeout={timeout}s  memory={memory}MB")
    lam = make_client(session, "lambda", region)
    env = {"Variables": {k: str(v) for k, v in env_vars.items() if v is not None and v != ""}}

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    try:
        existing = lam.get_function(FunctionName=fn_name)
        arn = existing["Configuration"]["FunctionArn"]
        log_update(f"Function exists — uploading new code ({len(zip_bytes)//1024} KB)...")
        lam.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        log_info("Waiting for code update to complete...")
        lam.get_waiter("function_updated").wait(FunctionName=fn_name,
                                                WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok("Code updated")
        log_info("Updating configuration (role, env vars, timeout)...")
        lam.update_function_configuration(
            FunctionName=fn_name, Role=role_arn,
            Timeout=timeout, MemorySize=memory,
            Runtime="python3.12",
            Handler="lambda_function.lambda_handler",
            Environment=env,
        )
        log_info("Waiting for configuration update to complete...")
        lam.get_waiter("function_updated").wait(FunctionName=fn_name,
                                                WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok(f"Lambda {fn_name} deployed  region={region}  arn={arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        log_create(f"Function not found — creating {fn_name}...")
        resp = lam.create_function(
            FunctionName=fn_name, Runtime="python3.12", Role=role_arn,
            Handler="lambda_function.lambda_handler",
            Code={"ZipFile": zip_bytes},
            Timeout=timeout, MemorySize=memory, Environment=env,
        )
        arn = resp["FunctionArn"]
        log_info("Waiting for function to become active...")
        lam.get_waiter("function_active").wait(FunctionName=fn_name,
                                               WaiterConfig={"Delay": 5, "MaxAttempts": 40})
        log_ok(f"Lambda {fn_name} created  region={region}  arn={arn}")
        return arn


def set_log_retention(session, fn_name: str, region: str, days: int = 30):
    logs = make_client(session, "logs", region)
    log_group = f"/aws/lambda/{fn_name}"
    try:
        logs.put_retention_policy(logGroupName=log_group, retentionInDays=days)
        log_ok(f"Log retention set: {log_group} → {days} days  ({region})")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            log_info(f"Log group {log_group} not yet created — will be set on first invocation")
        else:
            log_warn(f"Could not set log retention for {log_group}: {e}")


# ─── API Gateway ──────────────────────────────────────────────────────────────

def get_root_resource_id(apigw, api_id: str) -> str:
    for r in apigw.get_resources(restApiId=api_id, limit=500).get("items", []):
        if r.get("path") == "/":
            return r["id"]
    raise RuntimeError(f"Root resource '/' not found in API {api_id}")


def get_or_create_resource(apigw, api_id: str, parent_id: str, path_part: str) -> str:
    """Idempotent: return existing resource ID or create and return new one."""
    for r in apigw.get_resources(restApiId=api_id, limit=500).get("items", []):
        if r.get("parentId") == parent_id and r.get("pathPart") == path_part:
            log_skip(f"  Resource /{path_part} exists  id={r['id']}")
            return r["id"]
    log_create(f"  Creating resource /{path_part} under {parent_id}")
    resp = apigw.create_resource(restApiId=api_id, parentId=parent_id, pathPart=path_part)
    log_ok(f"  Resource /{path_part} created  id={resp['id']}")
    return resp["id"]


def ensure_cognito_authorizer(apigw, api_id: str, pool_arn: str) -> str:
    """Get or create Cognito authorizer named 'cognito_authorizer'."""
    for a in apigw.get_authorizers(restApiId=api_id).get("items", []):
        if a.get("name") == "cognito_authorizer":
            log_skip(f"Cognito authorizer exists  id={a['id']}")
            return a["id"]
    log_create("Creating Cognito authorizer 'cognito_authorizer'...")
    resp = apigw.create_authorizer(
        restApiId=api_id, name="cognito_authorizer",
        type="COGNITO_USER_POOLS", providerARNs=[pool_arn],
        identitySource="method.request.header.Authorization",
        authorizerResultTtlInSeconds=300,
    )
    log_ok(f"Cognito authorizer created  id={resp['id']}")
    return resp["id"]


def wire_method(session, apigw, api_id: str, resource_id: str, http_method: str,
                lambda_arn: str, api_region: str, account_id: str,
                fn_name: str, auth_type: str = "NONE", authorizer_id: str = None):
    """Wire HTTP method → Lambda (idempotent). Adds Lambda invoke permission."""
    log_info(f"  Wiring {http_method} → {fn_name}  auth={auth_type}")
    uri = (f"arn:aws:apigateway:{api_region}:lambda:path/2015-03-31"
           f"/functions/{lambda_arn}/invocations")

    # put_method (ConflictException = already exists, that's fine)
    try:
        kwargs = dict(restApiId=api_id, resourceId=resource_id,
                      httpMethod=http_method, authorizationType=auth_type)
        if auth_type == "COGNITO_USER_POOLS" and authorizer_id:
            kwargs["authorizerId"] = authorizer_id
        apigw.put_method(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        log_skip(f"    Method {http_method} already exists — updating integration")

    # put_integration (always overwrites)
    apigw.put_integration(restApiId=api_id, resourceId=resource_id,
                          httpMethod=http_method, type="AWS_PROXY",
                          integrationHttpMethod="POST", uri=uri)

    # Lambda permission (remove-then-add = idempotent)
    lam = make_client(session, "lambda", api_region)
    stmt_id = f"apigw-{fn_name.replace('_', '-')}-{http_method.lower()}"[:64]
    try:
        lam.remove_permission(FunctionName=lambda_arn, StatementId=stmt_id)
    except ClientError:
        pass
    try:
        lam.add_permission(
            FunctionName=lambda_arn, StatementId=stmt_id,
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{api_region}:{account_id}:{api_id}/*/*",
        )
        log_ok(f"    Lambda invoke permission granted → {fn_name}")
    except ClientError as e:
        log_warn(f"    Could not add Lambda permission: {e}")


def wire_cors(apigw, api_id: str, resource_id: str):
    """Add OPTIONS/MOCK CORS handler (idempotent)."""
    try:
        apigw.put_method(restApiId=api_id, resourceId=resource_id,
                         httpMethod="OPTIONS", authorizationType="NONE")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise

    apigw.put_integration(restApiId=api_id, resourceId=resource_id,
                          httpMethod="OPTIONS", type="MOCK",
                          requestTemplates={"application/json": '{"statusCode":200}'})
    _silent_put(apigw.put_method_response, restApiId=api_id, resourceId=resource_id,
                httpMethod="OPTIONS", statusCode="200",
                responseParameters={
                    "method.response.header.Access-Control-Allow-Origin": False,
                    "method.response.header.Access-Control-Allow-Headers": False,
                    "method.response.header.Access-Control-Allow-Methods": False,
                })
    _silent_put(apigw.put_integration_response, restApiId=api_id, resourceId=resource_id,
                httpMethod="OPTIONS", statusCode="200",
                responseParameters={
                    "method.response.header.Access-Control-Allow-Origin": "'*'",
                    "method.response.header.Access-Control-Allow-Headers": "'Content-Type,Authorization'",
                    "method.response.header.Access-Control-Allow-Methods": "'GET,POST,PUT,DELETE,OPTIONS'",
                },
                responseTemplates={"application/json": ""})
    log_ok(f"  CORS OPTIONS wired for resource {resource_id}")


def _silent_put(fn, **kwargs):
    try:
        fn(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise


# ─── Smoke Tests ──────────────────────────────────────────────────────────────

def smoke_test(method: str, url: str, expected: int, body: bytes = None) -> bool:
    try:
        req = Request(url, method=method, data=body)
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=15) as r:
                actual = r.status
        except HTTPError as e:
            actual = e.code
        passed = actual == expected
        if passed:
            log_ok(f"  {method:<7} {url}  →  HTTP {actual}  expected={expected}  [PASS]")
        else:
            log_warn(f"  {method:<7} {url}  →  HTTP {actual}  expected={expected}  [FAIL]")
        return passed
    except URLError as e:
        log_warn(f"  {method:<7} {url}  →  connection error: {e}  [FAIL]")
        return False


# ─── Interactive Deploy Confirm ───────────────────────────────────────────────

def confirm_deploy(env_label: str, script_name: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"\n  [DRY-RUN] Validation complete. No resources were created.")
        return False
    print(f"\n  {'━' * 58}")
    print(f"  Ready to deploy: {script_name}  env={env_label}")
    print(f"  This will CREATE or UPDATE AWS resources.")
    print(f"  {'━' * 58}")
    try:
        ans = input("  Proceed? [Y/n]:  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans == "" or ans.startswith("y")
