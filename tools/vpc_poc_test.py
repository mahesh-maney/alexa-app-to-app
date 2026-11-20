#!/usr/bin/env python3
"""
vpc_poc_test.py — Invoke the 4 POC Lambdas and report PASS / FAIL.

Run this BEFORE and AFTER attaching Lambdas to the VPC:

  BEFORE VPC → all 4 should report  reachable=True   → PASS
  AFTER  VPC → all 4 should report  reachable=False  → FAIL (no NAT gateway)

Usage:
    python3 tools/vpc_poc_test.py --region us-east-1
    python3 tools/vpc_poc_test.py --region us-east-1 --profile my-aws-profile
"""

from __future__ import annotations

import argparse
import json

import boto3

POC_LAMBDAS = [
    "poc-lambda-1",
    "poc-lambda-2",
    "poc-lambda-3",
    "poc-lambda-4",
]

PASS_MARKER = "\033[32m PASS \033[0m"  # green
FAIL_MARKER = "\033[31m FAIL \033[0m"  # red
INFO_MARKER = "\033[33m WARN \033[0m"  # yellow


def invoke_lambda(lam, name: str) -> dict:
    resp    = lam.invoke(FunctionName=name, InvocationType="RequestResponse")
    payload = json.loads(resp["Payload"].read())
    return payload


def run_tests(session) -> None:
    lam = session.client("lambda")

    print(f"\n{'─'*60}")
    print(f"  {'Lambda':<40}  {'Result':<8}  Detail")
    print(f"{'─'*60}")

    passed = 0
    failed = 0
    errors = 0

    for name in POC_LAMBDAS:
        try:
            result    = invoke_lambda(lam, name)
            reachable = result.get("reachable", False)
            detail    = result.get("public_ip") or result.get("error") or result.get("message", "")

            if reachable:
                marker = PASS_MARKER
                passed += 1
            else:
                marker = FAIL_MARKER
                failed += 1

            print(f"  {name:<40}  {marker}  {detail[:50]}")

        except Exception as exc:
            print(f"  {name:<40}  {INFO_MARKER}  Invocation error: {exc}")
            errors += 1

    print(f"{'─'*60}")
    print(f"  Total: {len(POC_LAMBDAS)}   PASS: {passed}   FAIL: {failed}   ERROR: {errors}")
    print(f"{'─'*60}\n")

    # Print POC verdict
    if passed == len(POC_LAMBDAS) and failed == 0:
        print("  VERDICT: All Lambdas reached the internet → BEFORE-VPC baseline confirmed.")
    elif failed == len(POC_LAMBDAS) and passed == 0:
        print("  VERDICT: No Lambda reached the internet → AFTER-VPC isolation confirmed.")
        print("           Adding Lambdas to a VPC without a NAT gateway breaks outbound access.")
    else:
        print("  VERDICT: Mixed results — check VPC config or Lambda attachment status.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region",  required=True, help="AWS region, e.g. us-east-1")
    parser.add_argument("--profile", default=None,  help="AWS CLI profile name (optional)")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    print(f"\n  vpc_poc_test.py   region={args.region}")
    run_tests(session)


if __name__ == "__main__":
    main()
