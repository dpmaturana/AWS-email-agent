import json
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_region  = os.environ.get("AWS_REGION", "eu-west-1")
dynamodb = boto3.resource("dynamodb", region_name=_region)
sfn      = boto3.client("stepfunctions", region_name=_region)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _start_approval(item, waiver_id):
    """Start the human-approval Step Functions execution for a now-complete waiver.

    Called only when a reply has filled the last gap. Idempotent: the execution
    name is derived from the waiver_id, so a duplicate call is safely ignored.
    """
    sfn_arn = os.environ.get("SFN_ARN")
    if not sfn_arn:
        logger.error("SFN_ARN not configured — cannot start approval for %s", waiver_id)
        return None
    if item.get("sfn_execution_arn"):
        logger.info("Approval already started for %s — skipping", waiver_id)
        return item["sfn_execution_arn"]
    try:
        resp = sfn.start_execution(
            stateMachineArn=sfn_arn,
            name=f"waiver-{waiver_id}",
            input=json.dumps({
                "waiver_id":   waiver_id,
                "email_from":  item.get("email_from", ""),
                "waiver_type": item.get("waiver_type", ""),
                "department":  item.get("department", ""),
            }),
        )
        execution_arn = resp["executionArn"]
        dynamodb.Table(os.environ["WAIVER_TABLE"]).update_item(
            Key={"waiver_id": waiver_id},
            UpdateExpression="SET sfn_execution_arn = :arn, updated_at = :ts",
            ExpressionAttributeValues={":arn": execution_arn, ":ts": _now()},
        )
        logger.info("Approval workflow started | waiver_id=%s | arn=%s", waiver_id, execution_arn)
        return execution_arn
    except sfn.exceptions.ExecutionAlreadyExists:
        logger.info("Approval execution already exists for %s — skipping", waiver_id)
        return None
    except Exception as e:
        logger.error("Failed to start approval for %s: %s", waiver_id, e)
        return None


def handler(event, context):
    waiver_id       = event.get("waiver_id")
    new_info        = event.get("new_info", {})
    missing_fields  = event.get("missing_fields", [])
    new_attachments = event.get("new_attachments", []) or []

    if not waiver_id:
        return {"success": False, "error": "waiver_id is required"}

    table = dynamodb.Table(os.environ["WAIVER_TABLE"])
    now   = _now()

    try:
        resp          = table.get_item(Key={"waiver_id": waiver_id})
        existing_item = resp.get("Item", {})
        existing_info = existing_item.get("collected_info", {})
    except Exception as e:
        return {"success": False, "error": str(e)}

    merged_info = {**existing_info, **new_info}
    # Append any documents from this reply to the ones already on file.
    merged_attachments = (existing_item.get("attachments", []) or []) + new_attachments
    new_status  = "pending_info" if missing_fields else "pending_approval"

    try:
        table.update_item(
            Key={"waiver_id": waiver_id},
            UpdateExpression=(
                "SET collected_info = :ci, missing_fields = :mf, "
                "attachments = :at, "
                "#st = :st, updated_at = :ts, "
                "history = list_append(if_not_exists(history, :empty), :h)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":ci":    merged_info,
                ":mf":    missing_fields,
                ":at":    merged_attachments,
                ":st":    new_status,
                ":ts":    now,
                ":h": [{
                    "timestamp": now,
                    "event":     "info_updated",
                    "content": (
                        f"Updated fields: {list(new_info.keys())}. "
                        f"Still missing: {missing_fields or 'nothing'}."
                    ),
                }],
                ":empty": [],
            },
        )
        logger.info("Waiver updated | waiver_id=%s | status=%s", waiver_id, new_status)
    except Exception as e:
        logger.error("update_item failed: %s", e)
        return {"success": False, "error": str(e)}

    # The request just became complete — now (and only now) enter human approval.
    if new_status == "pending_approval":
        _start_approval(existing_item, waiver_id)

    return {"success": True, "waiver_id": waiver_id, "status": new_status}