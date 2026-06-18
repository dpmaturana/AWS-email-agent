#!/usr/bin/env python3
"""
Smoke test for the deployed query_knowledge_base tool Lambda.

Usage:
    python test_query_kb.py <query-lambda-arn-or-name> "<query>" <department>

Example:
    python test_query_kb.py RagStack-QueryKbFn... "how many vacation days" hr

Requires AWS credentials with lambda:InvokeFunction on the target.
"""

import json
import sys

import boto3


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    target, query, department = sys.argv[1], sys.argv[2], sys.argv[3]
    top_k = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    client = boto3.client("lambda")
    resp = client.invoke(
        FunctionName=target,
        InvocationType="RequestResponse",
        Payload=json.dumps(
            {"query": query, "department": department, "top_k": top_k}
        ).encode("utf-8"),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        print(f"FunctionError: {body}")
        sys.exit(2)

    print(f"Retrieved {len(body)} chunks for department={department!r}:\n")
    for i, chunk in enumerate(body, 1):
        print(f"[{i}] score={chunk['score']:.4f} source={chunk['source']}")
        print(f"    {chunk['content'][:200]}...\n")


if __name__ == "__main__":
    main()
