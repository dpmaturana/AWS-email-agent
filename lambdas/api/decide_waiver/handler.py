"""POST /waivers/{waiver_id}/decide — approve or reject (Person 5 API).

Body: { "decision": "approve" | "reject", "comment": "string" }
Returns: { "success": boolean }
Action: invokes Person 4's approval Lambda, which signals Step Functions.
"""
import json
import os

import boto3

lambda_client = boto3.client("lambda")
APPROVAL_LAMBDA_ARN = os.environ["APPROVAL_LAMBDA_ARN"]

_CORS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body, default=str)}


def handler(event, context):
    waiver_id = (event.get("pathParameters") or {}).get("waiver_id")
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _resp(400, {"error": "Invalid JSON body"})

    decision = (body.get("decision") or "").strip().lower()
    comment = body.get("comment", "")

    if not waiver_id:
        return _resp(400, {"error": "waiver_id is required"})
    if decision not in ("approve", "reject"):
        return _resp(400, {"error": "decision must be 'approve' or 'reject'"})

    # The approval Lambda parses an API-Gateway-style event (body is a JSON string).
    inv = lambda_client.invoke(
        FunctionName=APPROVAL_LAMBDA_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "body": json.dumps({
                "waiver_id": waiver_id,
                "decision":  decision,
                "comment":   comment,
            })
        }).encode("utf-8"),
    )
    result = json.loads(inv["Payload"].read() or "{}")
    code = int(result.get("statusCode", 502))
    ok = 200 <= code < 300
    detail = result.get("body")
    try:
        detail = json.loads(detail) if isinstance(detail, str) else detail
    except (json.JSONDecodeError, TypeError):
        pass
    return _resp(200 if ok else code, {"success": ok, "detail": detail})
