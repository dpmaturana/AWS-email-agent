import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_region   = os.environ.get("AWS_REGION", "eu-west-1")
dynamodb  = boto3.resource("dynamodb", region_name=_region)
sfn       = boto3.client("stepfunctions", region_name=_region)


def _now():
    return datetime.now(timezone.utc).isoformat()


def handler(event, context):
    waiver_id      = event.get("waiver_id")
    email_from     = event.get("email_from", "")
    department     = event.get("department", "")
    waiver_type    = event.get("waiver_type", "")
    collected_info = event.get("collected_info", {})
    missing_fields = event.get("missing_fields", [])
    attachments    = event.get("attachments", []) or []

    table   = dynamodb.Table(os.environ["WAIVER_TABLE"])
    sfn_arn = os.environ["SFN_ARN"]
    now     = _now()

    item = {
        "waiver_id":          waiver_id,
        "thread_message_ids": [],
        "email_from":         email_from,
        "department":         department,
        "waiver_type":        waiver_type,
        "status":             "pending_info" if missing_fields else "pending_approval",
        "collected_info":     collected_info,
        "missing_fields":     missing_fields,
        "attachments":        attachments,
        "criteria":           {},
        "task_token":         None,
        "history": [{
            "timestamp": now,
            "event":     "waiver_created",
            "content":   f"Waiver created for {email_from} — type: {waiver_type}",
        }],
        "created_at": now,
        "updated_at": now,
    }

    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(waiver_id)")
        logger.info("DynamoDB record created | waiver_id=%s", waiver_id)
    except Exception as e:
        if "ConditionalCheckFailed" in str(e):
            logger.warning("Waiver %s already exists — skipping create", waiver_id)
        else:
            logger.error("DynamoDB put_item failed: %s", e)
            return {"success": False, "error": str(e)}

    # Only complete requests enter the human approval workflow. When information
    # or documents are still missing, the record stays in "pending_info" and the
    # agent emails the student for what is missing — the approver is NOT notified
    # until the request is complete (the SFN is started later by update_state).
    if missing_fields:
        logger.info(
            "Waiver %s incomplete (missing=%s) — holding for info, not starting approval",
            waiver_id, missing_fields,
        )
        return {"success": True, "waiver_id": waiver_id, "task_token": "", "status": "pending_info"}

    try:
        sfn_resp      = sfn.start_execution(
            stateMachineArn=sfn_arn,
            name=f"waiver-{waiver_id}",
            input=json.dumps({
                "waiver_id":   waiver_id,
                "email_from":  email_from,
                "waiver_type": waiver_type,
                "department":  department,
            }),
        )
        execution_arn = sfn_resp["executionArn"]
        logger.info("SFN execution started | waiver_id=%s | arn=%s", waiver_id, execution_arn)
    except Exception as e:
        logger.error("SFN start_execution failed: %s", e)
        return {"success": False, "error": str(e)}

    # Poll briefly for the task token that StoreToken Lambda writes to DynamoDB
    task_token = ""
    for _ in range(10):
        time.sleep(0.5)
        resp  = table.get_item(Key={"waiver_id": waiver_id})
        token = resp.get("Item", {}).get("task_token")
        if token:
            task_token = token
            break

    table.update_item(
        Key={"waiver_id": waiver_id},
        UpdateExpression="SET sfn_execution_arn = :arn, updated_at = :ts",
        ExpressionAttributeValues={":arn": execution_arn, ":ts": _now()},
    )

    return {"success": True, "waiver_id": waiver_id, "task_token": task_token, "status": "pending_approval"}