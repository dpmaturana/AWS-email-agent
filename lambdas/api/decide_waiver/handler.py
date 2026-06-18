"""
POST /waivers/{waiver_id}/decide
Body: { "decision": "approve" | "reject", "comment": "string" }
Invokes Person 4's approval Lambda (ARN from APPROVAL_LAMBDA_ARN env var).
"""
import json
import os
import boto3

APPROVAL_LAMBDA_ARN = os.environ["APPROVAL_LAMBDA_ARN"]
lambda_client = boto3.client("lambda")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}


def handler(event, _context):
    waiver_id = event["pathParameters"]["waiver_id"]

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"message": "Invalid JSON body"}),
        }

    decision = body.get("decision")
    comment = body.get("comment", "")

    if decision not in ("approve", "reject"):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"message": "'decision' must be 'approve' or 'reject'"}),
        }

    payload = {"waiver_id": waiver_id, "decision": decision, "comment": comment}

    response = lambda_client.invoke(
        FunctionName=APPROVAL_LAMBDA_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )

    result = json.loads(response["Payload"].read())
    status_code = result.get("statusCode", 200)

    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps({"success": status_code < 300}),
    }
