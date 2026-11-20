#!/usr/bin/env python3
"""
vpc_lambda_audit.py — VPC + Lambda association audit and auto-attach POC.

Steps this script performs:
  1. Create (or reuse) a temporary VPC, subnet, and security group
     identified by the tag  Name=digilux-temp-vpc / digilux-temp-sg.
  2. List ALL Lambda functions in the region.
  3. Classify each Lambda as:
       - COMPLIANT    : already in the temp VPC and already has the temp SG
       - MISSING_SG   : in the temp VPC but temp SG not in its security group list
       - NO_VPC       : no VPC config at all (non-VPC Lambda)
       - OTHER_VPC    : in a different VPC (reported; not modified by --attach)
  4. --attach : update MISSING_SG and NO_VPC Lambdas to add temp VPC + temp SG.
                OTHER_VPC Lambdas are reported but left untouched (safety).
  5. --cleanup: delete the temp VPC, subnet, and security group created by this script.

Usage:
    # Audit only (read-only, no changes)
    python3 tools/vpc_lambda_audit.py --region us-east-1

    # Audit + attach
    python3 tools/vpc_lambda_audit.py --region us-east-1 --attach

    # Tear down temp resources
    python3 tools/vpc_lambda_audit.py --region us-east-1 --cleanup

Requirements:
    pip install boto3
    AWS credentials configured (env vars, ~/.aws/credentials, or IAM role)

IAM permissions needed:
    ec2: CreateVpc, CreateSubnet, CreateSecurityGroup, DescribeVpcs,
         DescribeSubnets, DescribeSecurityGroups, ModifyVpcAttribute,
         DeleteVpc, DeleteSubnet, DeleteSecurityGroup
    lambda: ListFunctions, GetFunctionConfiguration, UpdateFunctionConfiguration
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

# ── Config ─────────────────────────────────────────────────────────────────────

TEMP_VPC_TAG        = "digilux-temp-vpc"
TEMP_SG_TAG         = "digilux-temp-sg"
TEMP_SUBNET_TAG     = "digilux-temp-subnet"
VPC_CIDR            = "10.99.0.0/16"
SUBNET_CIDR         = "10.99.1.0/24"
SG_NAME             = "digilux-temp-sg"
SG_DESCRIPTION      = "Temporary security group created by vpc_lambda_audit.py"

# ── Helpers ────────────────────────────────────────────────────────────────────

def _tag(name: str) -> list[dict]:
    return [{"Key": "Name", "Value": name}, {"Key": "CreatedBy", "Value": "vpc_lambda_audit.py"}]


def _find_by_tag(ec2, resource_type: str, tag_value: str) -> str | None:
    """Return the first resource ID matching Name=tag_value, or None."""
    type_filter = {
        "vpc":            ("vpc-id",            "vpc"),
        "subnet":         ("subnet-id",         "subnet"),
        "security-group": ("group-id",          "security-group"),
    }
    id_key, rt = type_filter[resource_type]
    resp = ec2.describe_tags(Filters=[
        {"Name": "key",          "Values": ["Name"]},
        {"Name": "value",        "Values": [tag_value]},
        {"Name": "resource-type","Values": [rt]},
    ])
    tags = resp.get("Tags", [])
    return tags[0]["ResourceId"] if tags else None


def _ok(msg: str)   -> None: print(f"  [OK]     {msg}")
def _info(msg: str) -> None: print(f"  [INFO]   {msg}")
def _warn(msg: str) -> None: print(f"  [WARN]   {msg}")
def _err(msg: str)  -> None: print(f"  [ERROR]  {msg}")
def _skip(msg: str) -> None: print(f"  [SKIP]   {msg}")


# ── Step 1: Ensure temp VPC ────────────────────────────────────────────────────

def ensure_vpc(ec2) -> str:
    print("\n── Step 1: Temp VPC ─────────────────────────────────────────────────────────")
    existing = _find_by_tag(ec2, "vpc", TEMP_VPC_TAG)
    if existing:
        _skip(f"Temp VPC already exists  id={existing}")
        return existing

    resp = ec2.create_vpc(CidrBlock=VPC_CIDR)
    vpc_id = resp["Vpc"]["VpcId"]
    # Wait for VPC to be available
    ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
    ec2.create_tags(Resources=[vpc_id], Tags=_tag(TEMP_VPC_TAG))
    # Enable DNS hostnames (required for Lambda)
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    _ok(f"Temp VPC created  id={vpc_id}  cidr={VPC_CIDR}")
    return vpc_id


def ensure_subnet(ec2, vpc_id: str) -> str:
    print("\n── Step 1b: Temp Subnet ─────────────────────────────────────────────────────")
    existing = _find_by_tag(ec2, "subnet", TEMP_SUBNET_TAG)
    if existing:
        _skip(f"Temp subnet already exists  id={existing}")
        return existing

    resp = ec2.create_subnet(VpcId=vpc_id, CidrBlock=SUBNET_CIDR)
    subnet_id = resp["Subnet"]["SubnetId"]
    ec2.create_tags(Resources=[subnet_id], Tags=_tag(TEMP_SUBNET_TAG))
    _ok(f"Temp subnet created  id={subnet_id}  cidr={SUBNET_CIDR}")
    return subnet_id


def ensure_security_group(ec2, vpc_id: str) -> str:
    print("\n── Step 1c: Temp Security Group ─────────────────────────────────────────────")
    existing = _find_by_tag(ec2, "security-group", TEMP_SG_TAG)
    if existing:
        _skip(f"Temp security group already exists  id={existing}")
        return existing

    resp = ec2.create_security_group(
        GroupName=SG_NAME,
        Description=SG_DESCRIPTION,
        VpcId=vpc_id,
    )
    sg_id = resp["GroupId"]
    ec2.create_tags(Resources=[sg_id], Tags=_tag(TEMP_SG_TAG))

    # Inbound: allow HTTPS (443) from anywhere — example rule
    ec2.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[{
            "IpProtocol": "tcp",
            "FromPort": 443,
            "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS inbound (temp)"}],
        }],
    )
    # Outbound: allow all (Lambda default) — already set by AWS, but explicit for clarity
    _ok(f"Temp security group created  id={sg_id}  vpc={vpc_id}")
    _info("Inbound rule: TCP 443 from 0.0.0.0/0")
    _info("Outbound rule: ALL TRAFFIC (AWS default)")
    return sg_id


# ── Step 2: List all Lambda functions ─────────────────────────────────────────

def list_all_lambdas(lam) -> list[dict]:
    print("\n── Step 2: Listing Lambda functions ─────────────────────────────────────────")
    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        functions.extend(page["Functions"])
    _ok(f"Found {len(functions)} Lambda function(s)")
    return functions


# ── Step 3: Classify Lambdas ───────────────────────────────────────────────────

def classify_lambdas(
    functions: list[dict],
    vpc_id: str,
    sg_id: str,
) -> dict[str, list[dict]]:
    """
    Returns a dict with keys:
      compliant   — in temp VPC + has temp SG
      missing_sg  — in temp VPC but temp SG not in SG list
      no_vpc      — no VPC config at all
      other_vpc   — in a different VPC
    """
    print("\n── Step 3: Classifying Lambdas ──────────────────────────────────────────────")
    result: dict[str, list[dict]] = {
        "compliant":  [],
        "missing_sg": [],
        "no_vpc":     [],
        "other_vpc":  [],
    }

    for fn in functions:
        name   = fn["FunctionName"]
        vpc_cfg = fn.get("VpcConfig") or {}
        fn_vpc  = vpc_cfg.get("VpcId", "")
        fn_sgs  = vpc_cfg.get("SecurityGroupIds", [])

        if not fn_vpc:
            result["no_vpc"].append(fn)
            _info(f"  NO_VPC     {name}")
        elif fn_vpc == vpc_id:
            if sg_id in fn_sgs:
                result["compliant"].append(fn)
                _ok(f"  COMPLIANT  {name}")
            else:
                result["missing_sg"].append(fn)
                _warn(f"  MISSING_SG {name}  (in temp VPC but SG {sg_id} not attached)")
        else:
            result["other_vpc"].append(fn)
            _warn(f"  OTHER_VPC  {name}  vpc={fn_vpc}  sgs={fn_sgs}")

    print()
    print(f"  Summary:")
    print(f"    COMPLIANT  : {len(result['compliant'])}")
    print(f"    MISSING_SG : {len(result['missing_sg'])}")
    print(f"    NO_VPC     : {len(result['no_vpc'])}")
    print(f"    OTHER_VPC  : {len(result['other_vpc'])}  ← not modified by --attach")
    return result


# ── Step 4: Attach Lambdas to temp VPC + SG ───────────────────────────────────

def attach_lambdas(
    lam,
    classified: dict[str, list[dict]],
    subnet_id: str,
    sg_id: str,
    vpc_id: str,
) -> None:
    print("\n── Step 4: Attaching Lambdas ─────────────────────────────────────────────────")

    to_attach = classified["no_vpc"] + classified["missing_sg"]
    if not to_attach:
        _ok("Nothing to attach — all audited Lambdas are already compliant or in another VPC")
        return

    for fn in to_attach:
        name    = fn["FunctionName"]
        vpc_cfg = fn.get("VpcConfig") or {}
        fn_vpc  = vpc_cfg.get("VpcId", "")

        if fn_vpc and fn_vpc == vpc_id:
            # In temp VPC but missing SG — merge SGs, keep existing subnets
            existing_sgs     = vpc_cfg.get("SecurityGroupIds", [])
            existing_subnets = vpc_cfg.get("SubnetIds", [subnet_id])
            new_sgs = list(set(existing_sgs + [sg_id]))
            new_subnets = list(set(existing_subnets + [subnet_id]))
            category = "MISSING_SG"
        else:
            # No VPC — attach fresh
            new_sgs     = [sg_id]
            new_subnets = [subnet_id]
            category = "NO_VPC"

        try:
            lam.update_function_configuration(
                FunctionName=name,
                VpcConfig={
                    "SubnetIds":        new_subnets,
                    "SecurityGroupIds": new_sgs,
                },
            )
            # Wait for update to complete
            waiter = lam.get_waiter("function_updated")
            waiter.wait(FunctionName=name)
            _ok(f"ATTACHED  {name}  ({category})  subnets={new_subnets}  sgs={new_sgs}")
        except ClientError as exc:
            _err(f"FAILED    {name}  error={exc.response['Error']['Code']}  {exc.response['Error']['Message']}")
        except Exception as exc:
            _err(f"FAILED    {name}  error={exc}")

    if classified["other_vpc"]:
        print()
        _warn(f"Skipped {len(classified['other_vpc'])} Lambda(s) in other VPCs — not modified:")
        for fn in classified["other_vpc"]:
            vpc_cfg = fn.get("VpcConfig") or {}
            _warn(f"  {fn['FunctionName']}  vpc={vpc_cfg.get('VpcId','?')}")


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup(ec2) -> None:
    print("\n── Cleanup: Removing temp resources ─────────────────────────────────────────")

    sg_id = _find_by_tag(ec2, "security-group", TEMP_SG_TAG)
    if sg_id:
        try:
            ec2.delete_security_group(GroupId=sg_id)
            _ok(f"Deleted security group  id={sg_id}")
        except ClientError as exc:
            _err(f"Could not delete SG {sg_id}: {exc.response['Error']['Message']}")
            _info("Hint: detach it from all Lambdas first, then re-run --cleanup")
    else:
        _skip("Temp security group not found (already deleted?)")

    subnet_id = _find_by_tag(ec2, "subnet", TEMP_SUBNET_TAG)
    if subnet_id:
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
            _ok(f"Deleted subnet  id={subnet_id}")
        except ClientError as exc:
            _err(f"Could not delete subnet {subnet_id}: {exc.response['Error']['Message']}")
    else:
        _skip("Temp subnet not found (already deleted?)")

    vpc_id = _find_by_tag(ec2, "vpc", TEMP_VPC_TAG)
    if vpc_id:
        try:
            ec2.delete_vpc(VpcId=vpc_id)
            _ok(f"Deleted VPC  id={vpc_id}")
        except ClientError as exc:
            _err(f"Could not delete VPC {vpc_id}: {exc.response['Error']['Message']}")
            _info("Hint: ensure all subnets/SGs inside the VPC are deleted first")
    else:
        _skip("Temp VPC not found (already deleted?)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region",  required=True, help="AWS region, e.g. us-east-1")
    parser.add_argument("--attach",  action="store_true", help="Attach unassociated Lambdas to temp VPC + SG")
    parser.add_argument("--cleanup", action="store_true", help="Delete temp VPC, subnet, and SG created by this script")
    parser.add_argument("--prefix",  default=None, help="Only target Lambdas whose names start with this prefix, e.g. poc-lambda")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name (optional)")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    ec2 = session.client("ec2")
    lam = session.client("lambda")

    print(f"\n{'='*70}")
    prefix_label = args.prefix or "(all)"
    print(f"  vpc_lambda_audit.py   region={args.region}  attach={args.attach}  cleanup={args.cleanup}  prefix={prefix_label}")
    print(f"{'='*70}")

    if args.cleanup:
        cleanup(ec2)
        return

    # Step 1 — Ensure temp infrastructure
    vpc_id    = ensure_vpc(ec2)
    subnet_id = ensure_subnet(ec2, vpc_id)
    sg_id     = ensure_security_group(ec2, vpc_id)

    print(f"\n  Temp VPC    : {vpc_id}")
    print(f"  Temp Subnet : {subnet_id}")
    print(f"  Temp SG     : {sg_id}")

    # Step 2 — List Lambdas
    functions = list_all_lambdas(lam)
    if args.prefix:
        before = len(functions)
        functions = [f for f in functions if f["FunctionName"].startswith(args.prefix)]
        _info(f"Prefix filter '{args.prefix}': {before} total → {len(functions)} matched")
    if not functions:
        print("\nNo Lambda functions found (after prefix filter). Done.")
        return

    # Step 3 — Classify
    classified = classify_lambdas(functions, vpc_id, sg_id)

    # Step 4 — Attach (if requested)
    if args.attach:
        attach_lambdas(lam, classified, subnet_id, sg_id, vpc_id)
    else:
        print("\n  Run with --attach to update the MISSING_SG and NO_VPC Lambdas above.")

    print(f"\n{'='*70}")
    print("  Done.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
