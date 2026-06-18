#!/usr/bin/env python3
"""Simulate an inbound email by invoking the ingestion Lambda directly.

SES is in sandbox mode for the demo (no domain / no receipt rule), so we feed
the ingestion Lambda a raw .eml exactly as SES would have stored it.

Usage:
    python scripts/invoke_ingestion.py scripts/sample_email.eml
    python scripts/invoke_ingestion.py scripts/sample_reply.eml --function IngestionLambda

The function name defaults to the CloudFormation logical resource name; pass the
deployed physical name (from `cdk deploy` outputs / Lambda console) if different.
"""

import argparse
import json
import sys

import boto3


def main():
    parser = argparse.ArgumentParser(description="Invoke the ingestion Lambda with a raw .eml")
    parser.add_argument("eml", help="path to a raw .eml file")
    parser.add_argument(
        "--function",
        default="InfraStack-IngestionLambda",
        help="Lambda function name or ARN (default: %(default)s)",
    )
    parser.add_argument("--region", default=None, help="AWS region override")
    args = parser.parse_args()

    with open(args.eml, "r", encoding="utf-8") as fh:
        raw_email = fh.read()

    client = boto3.client("lambda", region_name=args.region)
    resp = client.invoke(
        FunctionName=args.function,
        InvocationType="RequestResponse",
        Payload=json.dumps({"raw_email": raw_email}).encode("utf-8"),
    )

    body = resp["Payload"].read().decode("utf-8")
    print(f"StatusCode: {resp['StatusCode']}")
    if resp.get("FunctionError"):
        print(f"FunctionError: {resp['FunctionError']}")
    try:
        print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        print(body)
        sys.exit(1)


if __name__ == "__main__":
    main()
