"""
Approval Handler Lambda (Person 4)
====================================
Called by the frontend (Person 5) via API Gateway when an approver
submits their decision (approve or reject).

API Gateway resource : POST /waiver/approve
Exported ARN         : WaiverStack.ApprovalHandlerArn

Input (JSON body):
{
  "waiver_id": "string",
  "decision":  "approve" | "reject",
  "comment":   "string"             // optional reviewer note
}

Logic:
  1. Load task_token from DynamoDB using waiver_id
  2. Validate waiver is in "pending_approval" status
  3. If "approve" → stepfunctions.send_task_success(taskToken, output)
  4. If "reject"  → stepfunctions.send_task_failure(taskToken, error, cause)
  5. Update DynamoDB status + append history entry
  6. Return 200 JSON response to frontend
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
sfn      = boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type":                "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }


def handler(event: dict, context) -> dict:
    # ----------------------------------------------------------------
    # Parse input
    # ----------------------------------------------------------------
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})

    waiver_id = body.get("waiver_id", "").strip()
    decision  = body.get("decision", "").strip().lower()
    comment   = body.get("comment", "").strip()

    if not waiver_id:
        return _json_response(400, {"error": "waiver_id is required"})
    if decision not in ("approve", "reject"):
        return _json_response(400, {"error": "decision must be 'approve' or 'reject'"})

    table = dynamodb.Table(os.environ["WAIVER_TABLE"])

    # ----------------------------------------------------------------
    # 1. Load waiver record and extract task token
    # ----------------------------------------------------------------
    try:
        resp = table.get_item(Key={"waiver_id": waiver_id})
        item = resp.get("Item")
    except ClientError as exc:
        logger.error("DynamoDB get_item failed: %s", exc)
        return _json_response(500, {"error": "Database error"})

    if not item:
        return _json_response(404, {"error": f"Waiver '{waiver_id}' not found"})

    task_token  = item.get("task_token")
    status      = item.get("status", "")

    if not task_token:
        return _json_response(409, {
            "error": "No active task token. The waiver may have already been decided or timed out."
        })

    if status != "pending_approval":
        return _json_response(409, {
            "error": f"Waiver is not pending approval (current status: {status})"
        })

    # ----------------------------------------------------------------
    # 2. Signal Step Functions
    # ----------------------------------------------------------------
    now = _now()

    if decision == "approve":
        sfn_output = json.dumps({
            "waiver_id": waiver_id,
            "decision":  "approved",
            "comment":   comment,
        })
        try:
            sfn.send_task_success(
                taskToken=task_token,
                output=sfn_output,
            )
            logger.info("send_task_success | waiver_id=%s", waiver_id)
        except sfn.exceptions.TaskTimedOut:
            return _json_response(410, {"error": "Review window expired. Task token timed out."})
        except sfn.exceptions.InvalidToken:
            return _json_response(400, {"error": "Invalid task token."})
        except ClientError as exc:
            logger.error("send_task_success failed: %s", exc)
            return _json_response(500, {"error": str(exc)})

    else:  # reject
        try:
            sfn.send_task_failure(
                taskToken=task_token,
                error="WaiverRejected",
                cause=json.dumps({
                    "waiver_id": waiver_id,
                    "decision":  "rejected",
                    "comment":   comment,
                }),
            )
            logger.info("send_task_failure | waiver_id=%s", waiver_id)
        except sfn.exceptions.TaskTimedOut:
            return _json_response(410, {"error": "Review window expired. Task token timed out."})
        except sfn.exceptions.InvalidToken:
            return _json_response(400, {"error": "Invalid task token."})
        except ClientError as exc:
            logger.error("send_task_failure failed: %s", exc)
            return _json_response(500, {"error": str(exc)})

    # ----------------------------------------------------------------
    # 3. Optimistic DynamoDB update — Step Functions will also update
    #    via the Approved/Rejected states, but we record intent here
    # ----------------------------------------------------------------
    interim_status = "approved" if decision == "approve" else "rejected"
    try:
        table.update_item(
            Key={"waiver_id": waiver_id},
            UpdateExpression=(
                "SET #st = :st, task_token = :null, updated_at = :ts, "
                "history = list_append(if_not_exists(history, :empty), :h)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st":    interim_status,
                ":null":  None,             # Invalidate token — prevent replay
                ":ts":    now,
                ":h": [{
                    "timestamp": now,
                    "event":     f"human_decision_{interim_status}",
                    "content":   f"Approver decision: {decision}. Comment: {comment or 'none'}",
                }],
                ":empty": [],
            },
        )
    except ClientError as exc:
        # SFN was already signalled — log but don't fail the response
        logger.error("DynamoDB post-decision update failed (non-fatal): %s", exc)

    logger.info("Approval handled | waiver_id=%s | decision=%s", waiver_id, decision)

    return _json_response(200, {
        "ok":        True,
        "waiver_id": waiver_id,
        "decision":  interim_status,
    })
