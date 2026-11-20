#!/usr/bin/env python3
"""
vpc_poc_setup.py — Create / tear down the VPC isolation POC environment.

Creates:
  - A temp VPC (10.99.0.0/16) + subnet + security group
  - 4 test Lambdas, all starting with NO VPC config

Each Lambda handler tries to reach https://checkip.amazonaws.com (public internet).
  Before VPC  → reachable=True   (PASS)
  After VPC   → reachable=False  (FAIL — no NAT gateway in temp VPC)
  After NAT   → reachable=True   (PASS — NAT restores internet access)

Usage:
    python3 tools/vpc_poc_setup.py --region us-east-1 --setup
    python3 tools/vpc_poc_setup.py --region us-east-1 --add-nat
    python3 tools/vpc_poc_setup.py --region us-east-1 --cleanup

Logging:
    All activity is written to vpc_poc_setup.log (DEBUG level) in the current directory.
    Console shows INFO+ by default. Use --verbose for DEBUG on console too.
    Use --log-file <path> to override the log file location.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# ── Logging setup ──────────────────────────────────────────────────────────────

LOG_FILE_DEFAULT = "vpc_poc_setup.log"

def _setup_logging(log_file: str, verbose: bool) -> logging.Logger:
    logger = logging.getLogger("vpc_poc")
    logger.setLevel(logging.DEBUG)  # capture everything at root

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(funcName)-35s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — always DEBUG, full detail
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler — INFO by default, DEBUG if --verbose
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
    ch.setFormatter(logging.Formatter("    [LOG] %(levelname)s  %(message)s"))
    logger.addHandler(ch)

    return logger


log: logging.Logger = logging.getLogger("vpc_poc")  # replaced in main()


# ── Required IAM permissions ───────────────────────────────────────────────────

ALL_REQUIRED_ACTIONS = [
    "sts:GetCallerIdentity",
    "ec2:DescribeTags",    "ec2:CreateTags",
    "ec2:CreateVpc",       "ec2:DescribeVpcs",      "ec2:ModifyVpcAttribute",  "ec2:DeleteVpc",
    "ec2:CreateSubnet",    "ec2:DescribeSubnets",    "ec2:ModifySubnetAttribute","ec2:DeleteSubnet",
    "ec2:CreateSecurityGroup", "ec2:DescribeSecurityGroups",
    "ec2:AuthorizeSecurityGroupIngress",             "ec2:DeleteSecurityGroup",
    "ec2:CreateInternetGateway", "ec2:AttachInternetGateway",
    "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
    "ec2:AllocateAddress", "ec2:ReleaseAddress",
    "ec2:CreateNatGateway","ec2:DescribeNatGateways","ec2:DeleteNatGateway",
    "ec2:CreateRouteTable","ec2:DescribeRouteTables","ec2:CreateRoute",
    "ec2:ReplaceRoute",    "ec2:AssociateRouteTable","ec2:DisassociateRouteTable","ec2:DeleteRouteTable",
    "lambda:ListFunctions","lambda:GetFunction",     "lambda:CreateFunction",
    "lambda:UpdateFunctionConfiguration",            "lambda:DeleteFunction",   "lambda:InvokeFunction",
    "iam:GetRole",         "iam:CreateRole",         "iam:DeleteRole",
    "iam:AttachRolePolicy","iam:DetachRolePolicy",   "iam:PassRole",
    "iam:SimulatePrincipalPolicy",
]


# ── Console print helpers (also log to file) ───────────────────────────────────

def _ok(msg: str)   -> None:
    print(f"  [OK]    {msg}")
    log.info("OK: %s", msg)

def _info(msg: str) -> None:
    print(f"  [INFO]  {msg}")
    log.info("INFO: %s", msg)

def _skip(msg: str) -> None:
    print(f"  [SKIP]  {msg}")
    log.info("SKIP: %s", msg)

def _warn(msg: str) -> None:
    print(f"  [WARN]  {msg}")
    log.warning("WARN: %s", msg)

def _err(msg: str)  -> None:
    print(f"  [ERR]   {msg}")
    log.error("ERR: %s", msg)

def _hdr(msg: str)  -> None:
    print(f"\n── {msg} {'─' * max(1, 60 - len(msg))}")
    log.info("=== SECTION: %s ===", msg)


# ── Preflight permissions check ────────────────────────────────────────────────

def preflight_check(session) -> None:
    """
    Verify the current AWS principal has all permissions needed by this script.
    Uses iam:SimulatePrincipalPolicy — no actual resources are created.
    Exits with a clear error + fix commands if any permission is missing.
    """
    print("\n── Preflight: Checking IAM permissions ─────────────────────────────────────")
    log.info("Starting preflight IAM permissions check")
    log.debug("Total actions to verify: %d", len(ALL_REQUIRED_ACTIONS))

    sts = session.client("sts")
    iam = session.client("iam")

    # 1. Identify the caller
    log.debug("Calling sts:GetCallerIdentity")
    try:
        identity   = sts.get_caller_identity()
        caller_arn = identity["Arn"]
        account_id = identity["Account"]
        user_id    = identity["UserId"]
        print(f"  Caller  : {caller_arn}")
        print(f"  Account : {account_id}")
        log.info("Caller identity resolved  caller_arn=%s  account_id=%s  user_id=%s",
                 caller_arn, account_id, user_id)
    except ClientError as e:
        log.error("sts:GetCallerIdentity failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        _err(f"Cannot call sts:GetCallerIdentity — {e.response['Error']['Message']}")
        _err("Cannot verify permissions without knowing the caller identity.")
        sys.exit(1)

    # 2. Resolve policy source ARN (assumed-role sessions → underlying role)
    policy_source_arn = caller_arn
    if ":assumed-role/" in caller_arn:
        parts     = caller_arn.split(":")
        role_seg  = parts[5]
        role_name = role_seg.split("/")[1]
        policy_source_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        log.info("Assumed-role session detected  session_arn=%s  resolved_role_arn=%s",
                 caller_arn, policy_source_arn)
        _info(f"Assumed-role session — simulating against role: {policy_source_arn}")

    # 3. Simulate all required actions
    log.debug("Calling iam:SimulatePrincipalPolicy  source=%s  action_count=%d",
              policy_source_arn, len(ALL_REQUIRED_ACTIONS))
    denied: list[str] = []
    try:
        paginator = iam.get_paginator("simulate_principal_policy")
        page_num  = 0
        for page in paginator.paginate(
            PolicySourceArn=policy_source_arn,
            ActionNames=ALL_REQUIRED_ACTIONS,
            ResourceArns=["*"],
        ):
            page_num += 1
            results = page["EvaluationResults"]
            log.debug("SimulatePrincipalPolicy page=%d  results=%d", page_num, len(results))
            for result in results:
                action   = result["EvalActionName"]
                decision = result["EvalDecision"]
                log.debug("  permission_check  action=%-50s  decision=%s", action, decision)
                if decision != "allowed":
                    denied.append(action)
                    log.warning("PERMISSION DENIED: %s  decision=%s", action, decision)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        log.warning("SimulatePrincipalPolicy call failed  code=%s  message=%s",
                    code, e.response["Error"]["Message"])
        if code in ("AccessDenied", "AccessDeniedException"):
            _warn("iam:SimulatePrincipalPolicy is not granted — cannot verify permissions automatically.")
            _info("Proceeding anyway. If a permission is missing it will surface as an error during execution.")
            _info("To enable automatic checks, grant: iam:SimulatePrincipalPolicy to this principal.")
            print()
            return
        log.error("Unexpected error during permission simulation", exc_info=True)
        _err(f"Unexpected error during permission simulation: {e}")
        sys.exit(1)

    if not denied:
        log.info("All %d permissions verified — preflight passed", len(ALL_REQUIRED_ACTIONS))
        _ok(f"All {len(ALL_REQUIRED_ACTIONS)} required permissions verified — good to go.")
        print()
        return

    # 4. Report missing permissions + generate fix commands
    log.error("Preflight FAILED — %d permission(s) missing: %s", len(denied), denied)
    print()
    _err(f"{len(denied)} permission(s) MISSING for caller: {caller_arn}")
    print()
    print("  ── Missing permissions ──────────────────────────────────────────")
    for action in denied:
        print(f"    ✗  {action}")

    grouped: dict[str, list[str]] = {}
    for action in denied:
        svc = action.split(":")[0]
        grouped.setdefault(svc, []).append(action)

    policy_doc = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid":      f"VpcPocAllow{svc.capitalize()}",
                "Effect":   "Allow",
                "Action":   actions,
                "Resource": "*",
            }
            for svc, actions in grouped.items()
        ],
    }, indent=2)
    log.debug("Generated inline policy for missing permissions:\n%s", policy_doc)

    if ":user/" in caller_arn:
        principal_name = caller_arn.split("/")[-1]
        attach_cmd = (
            f"aws iam put-user-policy \\\n"
            f"  --user-name {principal_name} \\\n"
            f"  --policy-name VpcPocPermissions \\\n"
            f"  --policy-document '{policy_doc}'"
        )
        detach_cmd = (
            f"aws iam delete-user-policy \\\n"
            f"  --user-name {principal_name} \\\n"
            f"  --policy-name VpcPocPermissions"
        )
    else:
        principal_name = policy_source_arn.split("/")[-1]
        attach_cmd = (
            f"aws iam put-role-policy \\\n"
            f"  --role-name {principal_name} \\\n"
            f"  --policy-name VpcPocPermissions \\\n"
            f"  --policy-document '{policy_doc}'"
        )
        detach_cmd = (
            f"aws iam delete-role-policy \\\n"
            f"  --role-name {principal_name} \\\n"
            f"  --policy-name VpcPocPermissions"
        )

    print()
    print("  ── Inline policy to grant missing permissions ───────────────────")
    print()
    print(policy_doc)
    print()
    print("  ── Command to apply it ──────────────────────────────────────────")
    print()
    print(f"    {attach_cmd}")
    print()
    print("  ── Command to remove it after the POC ───────────────────────────")
    print()
    print(f"    {detach_cmd}")
    print()
    sys.exit(1)


# ── Constants ──────────────────────────────────────────────────────────────────

TEMP_VPC_TAG           = "digilux-temp-vpc"
TEMP_SG_TAG            = "digilux-temp-sg"
TEMP_SUBNET_TAG        = "digilux-temp-subnet"
TEMP_PUBLIC_SUBNET_TAG = "digilux-temp-public-subnet"
TEMP_IGW_TAG           = "digilux-temp-igw"
TEMP_NAT_TAG           = "digilux-temp-nat"
TEMP_EIP_TAG           = "digilux-temp-eip"
VPC_CIDR               = "10.99.0.0/16"
SUBNET_CIDR            = "10.99.1.0/24"
PUBLIC_SUBNET_CIDR     = "10.99.0.0/24"
SG_NAME                = "digilux-temp-sg"
ROLE_NAME              = "poc-lambda-basic-role"

POC_LAMBDAS = ["poc-lambda-1", "poc-lambda-2", "poc-lambda-3", "poc-lambda-4"]


# ── Lambda handler source ──────────────────────────────────────────────────────

HANDLER_SOURCE = """\
import urllib.request

TEST_URL = "https://checkip.amazonaws.com"
TIMEOUT  = 5


def lambda_handler(event, context):
    try:
        with urllib.request.urlopen(TEST_URL, timeout=TIMEOUT) as resp:
            public_ip = resp.read().decode().strip()
        return {"statusCode": 200, "reachable": True,
                "public_ip": public_ip,
                "message": f"Reached {TEST_URL} — public IP is {public_ip}"}
    except Exception as exc:
        return {"statusCode": 500, "reachable": False,
                "error": str(exc),
                "message": f"Could NOT reach {TEST_URL}: {exc}"}
"""


def _make_lambda_zip() -> bytes:
    log.debug("Building Lambda deployment zip in-memory")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("handler.py", HANDLER_SOURCE)
    size = buf.tell()
    log.debug("Lambda zip built  size_bytes=%d", size)
    return buf.getvalue()


# ── Tag helpers ────────────────────────────────────────────────────────────────

def _tag(name: str) -> list[dict]:
    return [
        {"Key": "Name",      "Value": name},
        {"Key": "CreatedBy", "Value": "vpc_poc_setup.py"},
    ]


def _find_by_tag(ec2, resource_type: str, tag_value: str) -> str | None:
    rt_map = {
        "vpc":              "vpc",
        "subnet":           "subnet",
        "security-group":   "security-group",
        "internet-gateway": "internet-gateway",
        "elastic-ip":       "elastic-ip",
    }
    log.debug("Looking up resource by tag  resource_type=%s  tag_value=%s", resource_type, tag_value)
    resp = ec2.describe_tags(Filters=[
        {"Name": "key",           "Values": ["Name"]},
        {"Name": "value",         "Values": [tag_value]},
        {"Name": "resource-type", "Values": [rt_map[resource_type]]},
    ])
    tags = resp.get("Tags", [])
    result = tags[0]["ResourceId"] if tags else None
    log.debug("Tag lookup result  resource_type=%s  tag=%s  found=%s", resource_type, tag_value, result)
    return result


def _find_nat_gateway(ec2, tag_value: str) -> str | None:
    log.debug("Looking up NAT Gateway by tag  tag_value=%s", tag_value)
    resp = ec2.describe_nat_gateways(Filters=[
        {"Name": "tag:Name", "Values": [tag_value]},
        {"Name": "state",    "Values": ["pending", "available"]},
    ])
    gateways = resp.get("NatGateways", [])
    result = gateways[0]["NatGatewayId"] if gateways else None
    log.debug("NAT Gateway lookup  tag=%s  found=%s  total_matching=%d",
              tag_value, result, len(gateways))
    return result


# ── Infrastructure ─────────────────────────────────────────────────────────────

def ensure_vpc(ec2) -> str:
    _hdr("Temp VPC")
    log.debug("Checking if VPC already exists  tag=%s", TEMP_VPC_TAG)
    existing = _find_by_tag(ec2, "vpc", TEMP_VPC_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating VPC  cidr=%s", VPC_CIDR)
    try:
        resp   = ec2.create_vpc(CidrBlock=VPC_CIDR)
        vpc_id = resp["Vpc"]["VpcId"]
        log.info("VPC created  id=%s  state=%s", vpc_id, resp["Vpc"]["State"])
    except ClientError as e:
        log.error("create_vpc failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Waiting for VPC to become available  id=%s", vpc_id)
    t0 = time.time()
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    log.info("VPC available  id=%s  waited_seconds=%.1f", vpc_id, time.time() - t0)

    log.debug("Tagging VPC  id=%s  tag=%s", vpc_id, TEMP_VPC_TAG)
    ec2.create_tags(Resources=[vpc_id], Tags=_tag(TEMP_VPC_TAG))

    log.debug("Enabling DNS hostnames and DNS support  vpc_id=%s", vpc_id)
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    log.info("VPC attributes set  dns_hostnames=True  dns_support=True  vpc_id=%s", vpc_id)

    _ok(f"Created  id={vpc_id}  cidr={VPC_CIDR}  (NO internet gateway — intentional)")
    return vpc_id


def ensure_subnet(ec2, vpc_id: str) -> str:
    _hdr("Temp Private Subnet")
    log.debug("Checking if private subnet exists  tag=%s  vpc_id=%s", TEMP_SUBNET_TAG, vpc_id)
    existing = _find_by_tag(ec2, "subnet", TEMP_SUBNET_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating private subnet  vpc_id=%s  cidr=%s", vpc_id, SUBNET_CIDR)
    try:
        resp      = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_CIDR)
        subnet_id = resp["Subnet"]["SubnetId"]
        log.info("Private subnet created  id=%s  vpc_id=%s  cidr=%s  state=%s",
                 subnet_id, vpc_id, SUBNET_CIDR, resp["Subnet"]["State"])
    except ClientError as e:
        log.error("create_subnet failed  vpc_id=%s  cidr=%s  code=%s  message=%s",
                  vpc_id, SUBNET_CIDR,
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Tagging private subnet  id=%s", subnet_id)
    ec2.create_tags(Resources=[subnet_id], Tags=_tag(TEMP_SUBNET_TAG))
    _ok(f"Created  id={subnet_id}  cidr={SUBNET_CIDR}  (private — no auto-assign public IP)")
    return subnet_id


def ensure_security_group(ec2, vpc_id: str) -> str:
    _hdr("Temp Security Group")
    log.debug("Checking if SG exists  tag=%s  vpc_id=%s", TEMP_SG_TAG, vpc_id)
    existing = _find_by_tag(ec2, "security-group", TEMP_SG_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating security group  name=%s  vpc_id=%s", SG_NAME, vpc_id)
    try:
        resp  = ec2.create_security_group(
            GroupName=SG_NAME,
            Description="Temp SG for VPC isolation POC",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        log.info("Security group created  id=%s  vpc_id=%s", sg_id, vpc_id)
    except ClientError as e:
        log.error("create_security_group failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Tagging SG  id=%s", sg_id)
    ec2.create_tags(Resources=[sg_id], Tags=_tag(TEMP_SG_TAG))

    log.debug("Adding inbound rule: TCP 443 from 0.0.0.0/0  sg_id=%s", sg_id)
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS (temp)"}],
        }],
    )
    log.info("SG inbound rule added  id=%s  rule=TCP/443/0.0.0.0/0", sg_id)
    _ok(f"Created  id={sg_id}  (outbound: ALL, but VPC has no NAT — traffic won't leave)")
    return sg_id


def ensure_lambda_role(iam) -> str:
    _hdr("IAM Execution Role")
    log.debug("Checking if IAM role exists  name=%s", ROLE_NAME)
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        arn  = role["Arn"]
        log.info("IAM role already exists  name=%s  arn=%s  created=%s",
                 ROLE_NAME, arn, role.get("CreateDate"))
        _skip(f"Already exists  arn={arn}")
        return arn
    except iam.exceptions.NoSuchEntityException:
        log.info("IAM role not found, will create  name=%s", ROLE_NAME)

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })
    log.info("Creating IAM role  name=%s", ROLE_NAME)
    try:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=trust,
            Description="Minimal role for VPC isolation POC",
        )
        arn = resp["Role"]["Arn"]
        log.info("IAM role created  name=%s  arn=%s", ROLE_NAME, arn)
    except ClientError as e:
        log.error("create_role failed  name=%s  code=%s  message=%s",
                  ROLE_NAME, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    policies = [
        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
    ]
    for policy_arn in policies:
        log.debug("Attaching managed policy  role=%s  policy=%s", ROLE_NAME, policy_arn)
        try:
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
            log.info("Policy attached  role=%s  policy=%s", ROLE_NAME, policy_arn)
        except ClientError as e:
            log.error("attach_role_policy failed  policy=%s  code=%s  message=%s",
                      policy_arn, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            raise

    _ok(f"Created  name={ROLE_NAME}  arn={arn}")
    _info("Waiting 15s for IAM propagation before creating Lambdas...")
    log.info("Sleeping 15s for IAM propagation  role=%s", ROLE_NAME)
    time.sleep(15)
    log.info("IAM propagation wait complete")
    return arn


# ── Lambda creation ────────────────────────────────────────────────────────────

def _lambda_exists(lam, name: str) -> bool:
    log.debug("Checking if Lambda exists  name=%s", name)
    try:
        cfg = lam.get_function(FunctionName=name)["Configuration"]
        log.debug("Lambda found  name=%s  state=%s  modified=%s",
                  name, cfg.get("State"), cfg.get("LastModified"))
        return True
    except lam.exceptions.ResourceNotFoundException:
        log.debug("Lambda not found  name=%s", name)
        return False


def create_poc_lambdas(lam, role_arn: str) -> None:
    _hdr("Creating 4 Test Lambdas (no VPC)")
    log.info("Building Lambda deployment package")
    zip_bytes = _make_lambda_zip()

    for name in POC_LAMBDAS:
        if _lambda_exists(lam, name):
            _skip(f"Already exists  {name}")
            continue

        log.info("Creating Lambda  name=%s  role=%s  runtime=python3.12", name, role_arn)
        try:
            resp = lam.create_function(
                FunctionName=name,
                Runtime="python3.12",
                Role=role_arn,
                Handler="handler.lambda_handler",
                Code={"ZipFile": zip_bytes},
                Description="VPC isolation POC — calls checkip.amazonaws.com",
                Timeout=10,
                MemorySize=128,
                Tags={"CreatedBy": "vpc_poc_setup.py"},
            )
            log.info("Lambda create_function call returned  name=%s  arn=%s  state=%s",
                     name, resp["FunctionArn"], resp.get("State"))
        except ClientError as e:
            log.error("create_function failed  name=%s  code=%s  message=%s",
                      name, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            raise

        log.debug("Waiting for Lambda to become Active  name=%s", name)
        t0 = time.time()
        lam.get_waiter("function_active").wait(FunctionName=name)
        log.info("Lambda active  name=%s  waited_seconds=%.1f", name, time.time() - t0)
        _ok(f"Created  {name}  (no VPC — internet accessible)")

    log.info("All POC Lambdas created/verified  count=%d", len(POC_LAMBDAS))


# ── NAT Gateway setup ──────────────────────────────────────────────────────────
#
#   VPC (10.99.0.0/16)
#   ├── Public subnet  10.99.0.0/24  → route: 0.0.0.0/0 → Internet Gateway
#   │     └── NAT Gateway (Elastic IP)
#   └── Private subnet 10.99.1.0/24 → route: 0.0.0.0/0 → NAT Gateway
#         └── Lambda functions
#
# Traffic: Lambda → NAT Gateway → Internet Gateway → Internet

def _ensure_igw(ec2, vpc_id: str) -> str:
    _hdr("Internet Gateway")
    log.debug("Checking if IGW exists  tag=%s  vpc_id=%s", TEMP_IGW_TAG, vpc_id)
    existing = _find_by_tag(ec2, "internet-gateway", TEMP_IGW_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating internet gateway  vpc_id=%s", vpc_id)
    try:
        igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        log.info("Internet gateway created  id=%s", igw_id)
    except ClientError as e:
        log.error("create_internet_gateway failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Tagging IGW  id=%s", igw_id)
    ec2.create_tags(Resources=[igw_id], Tags=_tag(TEMP_IGW_TAG))

    log.info("Attaching IGW to VPC  igw_id=%s  vpc_id=%s", igw_id, vpc_id)
    try:
        ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
        log.info("IGW attached successfully  igw_id=%s  vpc_id=%s", igw_id, vpc_id)
    except ClientError as e:
        log.error("attach_internet_gateway failed  igw_id=%s  vpc_id=%s  code=%s  message=%s",
                  igw_id, vpc_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    _ok(f"Created and attached  id={igw_id}  vpc={vpc_id}")
    return igw_id


def _ensure_public_subnet(ec2, vpc_id: str) -> str:
    _hdr("Public Subnet (NAT Gateway lives here)")
    log.debug("Checking if public subnet exists  tag=%s", TEMP_PUBLIC_SUBNET_TAG)
    existing = _find_by_tag(ec2, "subnet", TEMP_PUBLIC_SUBNET_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating public subnet  vpc_id=%s  cidr=%s", vpc_id, PUBLIC_SUBNET_CIDR)
    try:
        subnet_id = ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=PUBLIC_SUBNET_CIDR
        )["Subnet"]["SubnetId"]
        log.info("Public subnet created  id=%s", subnet_id)
    except ClientError as e:
        log.error("create_subnet (public) failed  cidr=%s  code=%s  message=%s",
                  PUBLIC_SUBNET_CIDR, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Tagging public subnet  id=%s", subnet_id)
    ec2.create_tags(Resources=[subnet_id], Tags=_tag(TEMP_PUBLIC_SUBNET_TAG))

    log.debug("Enabling MapPublicIpOnLaunch  subnet_id=%s", subnet_id)
    ec2.modify_subnet_attribute(SubnetId=subnet_id, MapPublicIpOnLaunch={"Value": True})
    log.info("Public subnet configured  id=%s  map_public_ip=True", subnet_id)

    _ok(f"Created  id={subnet_id}  cidr={PUBLIC_SUBNET_CIDR}  (auto-assign public IP enabled)")
    return subnet_id


def _ensure_public_route_table(ec2, vpc_id: str, public_subnet_id: str, igw_id: str) -> str:
    _hdr("Public Route Table (0.0.0.0/0 → IGW)")
    log.debug("Checking if public route table exists  vpc_id=%s  subnet_id=%s",
              vpc_id, public_subnet_id)
    resp = ec2.describe_route_tables(Filters=[
        {"Name": "association.subnet-id", "Values": [public_subnet_id]},
        {"Name": "tag:Name",              "Values": ["digilux-temp-public-rt"]},
    ])
    if resp["RouteTables"]:
        rt_id = resp["RouteTables"][0]["RouteTableId"]
        log.info("Public route table already exists  id=%s", rt_id)
        _skip(f"Already exists  id={rt_id}")
        return rt_id

    log.info("Creating route table  vpc_id=%s", vpc_id)
    try:
        rt_id = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        log.info("Route table created  id=%s  vpc_id=%s", rt_id, vpc_id)
    except ClientError as e:
        log.error("create_route_table failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    ec2.create_tags(Resources=[rt_id], Tags=[
        {"Key": "Name",      "Value": "digilux-temp-public-rt"},
        {"Key": "CreatedBy", "Value": "vpc_poc_setup.py"},
    ])

    log.debug("Creating route 0.0.0.0/0 → IGW  rt_id=%s  igw_id=%s", rt_id, igw_id)
    try:
        ec2.create_route(RouteTableId=rt_id, DestinationCidrBlock="0.0.0.0/0",
                         GatewayId=igw_id)
        log.info("Default route created  rt_id=%s  destination=0.0.0.0/0  target=%s", rt_id, igw_id)
    except ClientError as e:
        log.error("create_route (IGW) failed  rt_id=%s  igw_id=%s  code=%s  message=%s",
                  rt_id, igw_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Associating route table with public subnet  rt_id=%s  subnet_id=%s", rt_id, public_subnet_id)
    assoc = ec2.associate_route_table(RouteTableId=rt_id, SubnetId=public_subnet_id)
    log.info("Route table associated  rt_id=%s  subnet_id=%s  association_id=%s",
             rt_id, public_subnet_id, assoc.get("AssociationId"))

    _ok(f"Created  id={rt_id}  route: 0.0.0.0/0 → {igw_id}")
    return rt_id


def _ensure_eip(ec2) -> str:
    _hdr("Elastic IP (for NAT Gateway)")
    log.debug("Checking if EIP exists  tag=%s", TEMP_EIP_TAG)
    existing = _find_by_tag(ec2, "elastic-ip", TEMP_EIP_TAG)
    if existing:
        _skip(f"Already allocated  allocation_id={existing}")
        return existing

    log.info("Allocating Elastic IP for NAT Gateway")
    try:
        resp          = ec2.allocate_address(Domain="vpc")
        allocation_id = resp["AllocationId"]
        public_ip     = resp["PublicIp"]
        log.info("EIP allocated  allocation_id=%s  public_ip=%s", allocation_id, public_ip)
    except ClientError as e:
        log.error("allocate_address failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    log.debug("Tagging EIP  allocation_id=%s", allocation_id)
    ec2.create_tags(Resources=[allocation_id], Tags=_tag(TEMP_EIP_TAG))
    _ok(f"Allocated  allocation_id={allocation_id}  public_ip={public_ip}")
    return allocation_id


def _ensure_nat_gateway(ec2, public_subnet_id: str, eip_allocation_id: str) -> str:
    _hdr("NAT Gateway")
    log.debug("Checking if NAT Gateway exists  tag=%s", TEMP_NAT_TAG)
    existing = _find_nat_gateway(ec2, TEMP_NAT_TAG)
    if existing:
        _skip(f"Already exists  id={existing}")
        return existing

    log.info("Creating NAT Gateway  subnet_id=%s  eip_allocation_id=%s",
             public_subnet_id, eip_allocation_id)
    try:
        nat_id = ec2.create_nat_gateway(
            SubnetId=public_subnet_id,
            AllocationId=eip_allocation_id,
            TagSpecifications=[{
                "ResourceType": "natgateway",
                "Tags": _tag(TEMP_NAT_TAG),
            }],
        )["NatGateway"]["NatGatewayId"]
        log.info("NAT Gateway create call returned  id=%s", nat_id)
    except ClientError as e:
        log.error("create_nat_gateway failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise

    _info(f"Created  id={nat_id}  — waiting for it to become available (~60s)...")
    log.info("Waiting for NAT Gateway to become available  id=%s", nat_id)
    t0 = time.time()
    ec2.get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat_id])
    elapsed = time.time() - t0
    log.info("NAT Gateway available  id=%s  waited_seconds=%.1f", nat_id, elapsed)
    _ok(f"NAT Gateway available  id={nat_id}  (took {elapsed:.0f}s)")
    return nat_id


def _update_private_subnet_route(ec2, vpc_id: str, private_subnet_id: str, nat_id: str) -> None:
    _hdr("Private Subnet Route → NAT Gateway")
    log.debug("Looking up route table for private subnet  vpc_id=%s  subnet_id=%s",
              vpc_id, private_subnet_id)

    resp = ec2.describe_route_tables(Filters=[
        {"Name": "vpc-id",                "Values": [vpc_id]},
        {"Name": "association.subnet-id", "Values": [private_subnet_id]},
    ])
    if not resp["RouteTables"]:
        log.info("No explicit RT association for private subnet — falling back to main RT  vpc_id=%s", vpc_id)
        resp = ec2.describe_route_tables(Filters=[
            {"Name": "vpc-id",           "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]},
        ])

    rt_id  = resp["RouteTables"][0]["RouteTableId"]
    routes = resp["RouteTables"][0].get("Routes", [])
    log.info("Private subnet route table found  rt_id=%s  existing_routes=%d", rt_id, len(routes))
    for r in routes:
        log.debug("  existing_route  dest=%s  gateway=%s  nat=%s  state=%s",
                  r.get("DestinationCidrBlock"), r.get("GatewayId"),
                  r.get("NatGatewayId"), r.get("State"))

    for route in routes:
        if route.get("DestinationCidrBlock") == "0.0.0.0/0":
            existing_nat = route.get("NatGatewayId")
            if existing_nat == nat_id:
                log.info("Route already correct  rt_id=%s  dest=0.0.0.0/0  nat_id=%s", rt_id, nat_id)
                _skip(f"Route already points to NAT  rt={rt_id}")
            else:
                log.info("Replacing existing default route  rt_id=%s  old_target=%s  new_nat=%s",
                         rt_id, existing_nat or route.get("GatewayId"), nat_id)
                try:
                    ec2.replace_route(RouteTableId=rt_id,
                                      DestinationCidrBlock="0.0.0.0/0",
                                      NatGatewayId=nat_id)
                    log.info("Route replaced  rt_id=%s  dest=0.0.0.0/0  nat_id=%s", rt_id, nat_id)
                    _ok(f"Replaced default route  rt={rt_id}  → {nat_id}")
                except ClientError as e:
                    log.error("replace_route failed  rt_id=%s  code=%s  message=%s",
                              rt_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
                    raise
            return

    log.info("No existing default route — creating  rt_id=%s  nat_id=%s", rt_id, nat_id)
    try:
        ec2.create_route(RouteTableId=rt_id,
                         DestinationCidrBlock="0.0.0.0/0",
                         NatGatewayId=nat_id)
        log.info("Route created  rt_id=%s  dest=0.0.0.0/0  nat_id=%s", rt_id, nat_id)
        _ok(f"Added route  rt={rt_id}  0.0.0.0/0 → {nat_id}")
    except ClientError as e:
        log.error("create_route (NAT) failed  code=%s  message=%s",
                  e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        raise


def add_nat_gateway(session, region: str) -> None:
    ec2 = session.client("ec2")

    log.info("add_nat_gateway started  region=%s", region)
    vpc_id         = _find_by_tag(ec2, "vpc", TEMP_VPC_TAG)
    private_sub_id = _find_by_tag(ec2, "subnet", TEMP_SUBNET_TAG)

    if not vpc_id or not private_sub_id:
        log.error("Prerequisite resources not found  vpc_id=%s  private_subnet=%s",
                  vpc_id, private_sub_id)
        _err("Temp VPC or private subnet not found — run --setup first")
        sys.exit(1)

    log.info("Prerequisites found  vpc_id=%s  private_subnet_id=%s", vpc_id, private_sub_id)

    igw_id        = _ensure_igw(ec2, vpc_id)
    public_sub_id = _ensure_public_subnet(ec2, vpc_id)
    _ensure_public_route_table(ec2, vpc_id, public_sub_id, igw_id)
    eip_id        = _ensure_eip(ec2)
    nat_id        = _ensure_nat_gateway(ec2, public_sub_id, eip_id)
    _update_private_subnet_route(ec2, vpc_id, private_sub_id, nat_id)

    log.info("NAT Gateway setup complete  igw=%s  public_subnet=%s  nat=%s  private_subnet=%s",
             igw_id, public_sub_id, nat_id, private_sub_id)

    print(f"""
{'='*65}
  NAT Gateway ready.

  Internet Gateway : {igw_id}
  Public Subnet    : {public_sub_id}  ({PUBLIC_SUBNET_CIDR})
  NAT Gateway      : {nat_id}
  Private Subnet   : {private_sub_id}  ({SUBNET_CIDR})
    Route added    : 0.0.0.0/0 → {nat_id}

  Running test now — all 4 Lambdas should be reachable again:
{'='*65}
""")
    test_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpc_poc_test.py")
    log.info("Loading vpc_poc_test module  path=%s", test_path)
    spec = importlib.util.spec_from_file_location("vpc_poc_test", test_path)
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        log.info("Running post-NAT connectivity test")
        mod.run_tests(session)
    else:
        log.warning("Could not load vpc_poc_test module  path=%s", test_path)
        _info(f"Run manually: python3 tools/vpc_poc_test.py --region {region}")


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup(session) -> None:
    ec2 = session.client("ec2")
    lam = session.client("lambda")
    iam = session.client("iam")

    log.info("Cleanup started")

    # 1. Delete Lambdas first (hold ENIs in the VPC)
    _hdr("Deleting Lambdas")
    for name in POC_LAMBDAS:
        log.info("Deleting Lambda  name=%s", name)
        try:
            lam.delete_function(FunctionName=name)
            log.info("Lambda deleted  name=%s", name)
            _ok(f"Deleted  {name}")
        except lam.exceptions.ResourceNotFoundException:
            log.info("Lambda not found (already deleted)  name=%s", name)
            _skip(f"Not found  {name}")
        except ClientError as e:
            log.error("delete_function failed  name=%s  code=%s  message=%s",
                      name, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"Failed to delete {name}: {e.response['Error']['Message']}")

    _info("Waiting 30s for Lambda ENIs to detach from VPC...")
    log.info("Sleeping 30s for Lambda ENI release")
    time.sleep(30)
    log.info("ENI release wait complete")

    # 2. NAT Gateway (must go before EIP + subnets)
    _hdr("Deleting NAT Gateway")
    nat_id = _find_nat_gateway(ec2, TEMP_NAT_TAG)
    if nat_id:
        log.info("Deleting NAT Gateway  id=%s", nat_id)
        try:
            ec2.delete_nat_gateway(NatGatewayId=nat_id)
            _info(f"Deleting  id={nat_id}  — waiting (~60s)...")
            log.info("Waiting for NAT Gateway deletion  id=%s", nat_id)
            t0 = time.time()
            ec2.get_waiter("nat_gateway_deleted").wait(NatGatewayIds=[nat_id])
            log.info("NAT Gateway deleted  id=%s  waited_seconds=%.1f", nat_id, time.time() - t0)
            _ok(f"Deleted  id={nat_id}")
        except ClientError as e:
            log.error("delete_nat_gateway failed  id=%s  code=%s  message=%s",
                      nat_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        log.info("NAT Gateway not found — skipping  tag=%s", TEMP_NAT_TAG)
        _skip("Not found")

    # 3. Elastic IP
    _hdr("Releasing Elastic IP")
    eip_id = _find_by_tag(ec2, "elastic-ip", TEMP_EIP_TAG)
    if eip_id:
        log.info("Releasing EIP  allocation_id=%s", eip_id)
        try:
            ec2.release_address(AllocationId=eip_id)
            log.info("EIP released  allocation_id=%s", eip_id)
            _ok(f"Released  allocation_id={eip_id}")
        except ClientError as e:
            log.error("release_address failed  allocation_id=%s  code=%s  message=%s",
                      eip_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        log.info("EIP not found — skipping  tag=%s", TEMP_EIP_TAG)
        _skip("Not found")

    # 4. Internet Gateway
    _hdr("Deleting Internet Gateway")
    igw_id = _find_by_tag(ec2, "internet-gateway", TEMP_IGW_TAG)
    vpc_id = _find_by_tag(ec2, "vpc", TEMP_VPC_TAG)
    if igw_id:
        log.info("Detaching and deleting IGW  id=%s  vpc_id=%s", igw_id, vpc_id)
        try:
            if vpc_id:
                ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
                log.info("IGW detached  id=%s  vpc_id=%s", igw_id, vpc_id)
            ec2.delete_internet_gateway(InternetGatewayId=igw_id)
            log.info("IGW deleted  id=%s", igw_id)
            _ok(f"Deleted  id={igw_id}")
        except ClientError as e:
            log.error("delete_internet_gateway failed  id=%s  code=%s  message=%s",
                      igw_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        log.info("IGW not found — skipping  tag=%s", TEMP_IGW_TAG)
        _skip("Not found")

    # 5. Custom route tables
    _hdr("Deleting Custom Route Tables")
    if vpc_id:
        log.debug("Describing custom route tables  vpc_id=%s", vpc_id)
        rts = ec2.describe_route_tables(Filters=[
            {"Name": "vpc-id",   "Values": [vpc_id]},
            {"Name": "tag:Name", "Values": ["digilux-temp-public-rt"]},
        ])["RouteTables"]
        log.info("Found %d custom route table(s) to delete  vpc_id=%s", len(rts), vpc_id)
        for rt in rts:
            rt_id = rt["RouteTableId"]
            log.debug("Disassociating and deleting route table  id=%s", rt_id)
            for assoc in rt.get("Associations", []):
                if not assoc.get("Main"):
                    assoc_id = assoc["RouteTableAssociationId"]
                    try:
                        ec2.disassociate_route_table(AssociationId=assoc_id)
                        log.info("Route table disassociated  assoc_id=%s  rt_id=%s", assoc_id, rt_id)
                    except ClientError as e:
                        log.warning("disassociate_route_table failed  assoc_id=%s  code=%s",
                                    assoc_id, e.response["Error"]["Code"])
            try:
                ec2.delete_route_table(RouteTableId=rt_id)
                log.info("Route table deleted  id=%s", rt_id)
                _ok(f"Deleted route table  id={rt_id}")
            except ClientError as e:
                log.error("delete_route_table failed  id=%s  code=%s  message=%s",
                          rt_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
                _err(f"{e.response['Error']['Message']}")

    # 6. Public subnet
    _hdr("Deleting Public Subnet")
    pub_sub_id = _find_by_tag(ec2, "subnet", TEMP_PUBLIC_SUBNET_TAG)
    if pub_sub_id:
        log.info("Deleting public subnet  id=%s", pub_sub_id)
        try:
            ec2.delete_subnet(SubnetId=pub_sub_id)
            log.info("Public subnet deleted  id=%s", pub_sub_id)
            _ok(f"Deleted  id={pub_sub_id}")
        except ClientError as e:
            log.error("delete_subnet (public) failed  id=%s  code=%s  message=%s",
                      pub_sub_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        _skip("Not found")

    # 7. Private subnet
    _hdr("Deleting Private Subnet")
    subnet_id = _find_by_tag(ec2, "subnet", TEMP_SUBNET_TAG)
    if subnet_id:
        log.info("Deleting private subnet  id=%s", subnet_id)
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
            log.info("Private subnet deleted  id=%s", subnet_id)
            _ok(f"Deleted  id={subnet_id}")
        except ClientError as e:
            log.error("delete_subnet (private) failed  id=%s  code=%s  message=%s",
                      subnet_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        _skip("Not found")

    # 8. Security Group
    _hdr("Deleting Security Group")
    sg_id = _find_by_tag(ec2, "security-group", TEMP_SG_TAG)
    if sg_id:
        log.info("Deleting security group  id=%s", sg_id)
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log.info("Security group deleted  id=%s", sg_id)
            _ok(f"Deleted  id={sg_id}")
        except ClientError as e:
            log.error("delete_security_group failed  id=%s  code=%s  message=%s",
                      sg_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
    else:
        _skip("Not found")

    # 9. VPC
    _hdr("Deleting VPC")
    if vpc_id:
        log.info("Deleting VPC  id=%s", vpc_id)
        try:
            ec2.delete_vpc(VpcId=vpc_id)
            log.info("VPC deleted  id=%s", vpc_id)
            _ok(f"Deleted  id={vpc_id}")
        except ClientError as e:
            log.error("delete_vpc failed  id=%s  code=%s  message=%s",
                      vpc_id, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
            _err(f"{e.response['Error']['Message']}")
            _info("Tip: check AWS console → VPC → Network Interfaces for leftover ENIs")
    else:
        _skip("Not found")

    # 10. IAM Role
    _hdr("Deleting IAM Role")
    log.info("Cleaning up IAM role  name=%s", ROLE_NAME)
    try:
        for policy_arn in [
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
        ]:
            try:
                iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
                log.info("Policy detached  role=%s  policy=%s", ROLE_NAME, policy_arn)
            except Exception as e:
                log.debug("detach_role_policy skipped (may already be detached)  policy=%s  reason=%s",
                          policy_arn, e)
        iam.delete_role(RoleName=ROLE_NAME)
        log.info("IAM role deleted  name=%s", ROLE_NAME)
        _ok(f"Deleted  {ROLE_NAME}")
    except iam.exceptions.NoSuchEntityException:
        log.info("IAM role not found (already deleted)  name=%s", ROLE_NAME)
        _skip(f"Not found  {ROLE_NAME}")
    except ClientError as e:
        log.error("delete_role failed  name=%s  code=%s  message=%s",
                  ROLE_NAME, e.response["Error"]["Code"], e.response["Error"]["Message"], exc_info=True)
        _err(f"{e.response['Error']['Message']}")

    log.info("Cleanup complete")
    _ok("Cleanup complete.")


# ── Setup ──────────────────────────────────────────────────────────────────────

def setup(session, region: str) -> None:
    log.info("Setup started  region=%s", region)
    ec2 = session.client("ec2")
    lam = session.client("lambda")
    iam = session.client("iam")

    role_arn  = ensure_lambda_role(iam)
    vpc_id    = ensure_vpc(ec2)
    subnet_id = ensure_subnet(ec2, vpc_id)
    sg_id     = ensure_security_group(ec2, vpc_id)
    create_poc_lambdas(lam, role_arn)

    log.info("Setup complete  vpc_id=%s  subnet_id=%s  sg_id=%s  lambdas=%s",
             vpc_id, subnet_id, sg_id, POC_LAMBDAS)

    print(f"""
{'='*65}
  POC environment ready.

  Temp VPC    : {vpc_id}   (NO internet gateway — intentional)
  Temp Subnet : {subnet_id}
  Temp SG     : {sg_id}
  Lambdas     : {', '.join(POC_LAMBDAS)}

  ── Run the POC ───────────────────────────────────────────────

  STEP 1 — Test BEFORE VPC
           Expected: all 4 reachable=True  (PASS)

    python3 tools/vpc_poc_test.py --region {region}

  STEP 2 — Attach all 4 Lambdas to the temp VPC (no NAT yet)

    python3 tools/vpc_lambda_audit.py --region {region} --prefix poc-lambda --attach

  STEP 3 — Test AFTER VPC, BEFORE NAT
           Expected: all 4 reachable=False (FAIL — no internet route)

    python3 tools/vpc_poc_test.py --region {region}

  STEP 4 — Add NAT Gateway + re-test automatically
           Expected: all 4 reachable=True  (PASS — NAT restores internet)

    python3 tools/vpc_poc_setup.py --region {region} --add-nat

  STEP 5 — Cleanup everything

    python3 tools/vpc_poc_setup.py --region {region} --cleanup

{'='*65}
""")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region",   required=True,  help="AWS region, e.g. us-east-1")
    parser.add_argument("--setup",    action="store_true", help="Create POC infra + 4 test Lambdas")
    parser.add_argument("--add-nat",  action="store_true", help="Add NAT Gateway to restore internet in VPC")
    parser.add_argument("--cleanup",  action="store_true", help="Delete all POC resources")
    parser.add_argument("--profile",  default=None,   help="AWS CLI profile name (optional)")
    parser.add_argument("--log-file", default=LOG_FILE_DEFAULT,
                        help=f"Log file path (default: {LOG_FILE_DEFAULT})")
    parser.add_argument("--verbose",  action="store_true",
                        help="Also print DEBUG logs to console (default: file only)")
    args = parser.parse_args()

    if not args.setup and not args.cleanup and not args.add_nat:
        parser.print_help()
        sys.exit(1)

    # Initialise logging
    global log
    log = _setup_logging(args.log_file, args.verbose)

    action = "SETUP" if args.setup else "ADD-NAT" if args.add_nat else "CLEANUP"

    log.info("=" * 60)
    log.info("vpc_poc_setup.py started  action=%s  region=%s  log_file=%s",
             action, args.region, args.log_file)
    log.info("Python pid=%d  timestamp=%s", os.getpid(), datetime.now(timezone.utc).isoformat())
    log.info("=" * 60)

    print(f"\n{'='*65}")
    print(f"  vpc_poc_setup.py   region={args.region}  {action}")
    print(f"  Log file: {args.log_file}")
    print(f"{'='*65}")

    session = boto3.Session(region_name=args.region, profile_name=args.profile)

    preflight_check(session)

    t0 = time.time()
    try:
        if args.setup:
            setup(session, args.region)
        elif args.add_nat:
            add_nat_gateway(session, args.region)
        else:
            cleanup(session)
    except Exception as e:
        log.critical("Unhandled exception — script aborted  action=%s  error=%s",
                     action, e, exc_info=True)
        _err(f"Script aborted due to unexpected error: {e}")
        _err(f"Full details in log file: {args.log_file}")
        sys.exit(1)

    elapsed = time.time() - t0
    log.info("vpc_poc_setup.py finished  action=%s  elapsed_seconds=%.1f", action, elapsed)
    print(f"\n  Log written to: {args.log_file}")

    _generate_html_report(args.log_file)


def _generate_html_report(log_file: str) -> None:
    """Offer to generate an HTML activity report from the log file."""
    report_script = os.path.join(os.path.dirname(__file__), "vpc_poc_report.py")
    if not os.path.exists(report_script):
        return  # report script not present — silently skip

    print(f"\n{'─'*65}")
    print("  HTML Activity Report")
    print(f"{'─'*65}")
    print(f"  A human-readable HTML report can be generated from the log.")
    print(f"  Log file : {log_file}")
    print(f"  Output   : vpc_poc_report.html  (current directory)")
    print()

    try:
        answer = input("  Generate HTML report now? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return  # non-interactive environment — skip quietly

    if answer in ("", "y", "yes"):
        import subprocess
        out_file = "vpc_poc_report.html"
        result = subprocess.run(
            [sys.executable, report_script, "--log", log_file, "--out", out_file, "--open"],
            capture_output=False,
        )
        if result.returncode == 0:
            print(f"\n  Report saved to: {os.path.abspath(out_file)}")
        else:
            print(f"\n  Report generation failed (exit code {result.returncode}).")
            print(f"  Run manually:  python3 tools/vpc_poc_report.py --log {log_file}")
    else:
        print(f"\n  Skipped. Run manually any time:")
        print(f"    python3 tools/vpc_poc_report.py --log {log_file}")
    print(f"{'─'*65}\n")


if __name__ == "__main__":
    main()
