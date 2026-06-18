"""
StoreTaskToken Lambda
======================
Called as a Lambda task state inside the Step Functions workflow,
immediately before WaitForHumanApproval.

Step Functions passes the task token via the context object ($$.Task.Token).
This Lambda writes the token to DynamoDB and publishes the approver
notification via SNS → SES.

Event shape (from Step Functions):
{
  "waiver_id":   "string",
  "email_from":  "string",
  "waiver_type": "string",
  "department":  "string",
  "task_token":  "string"   ← injected by SFN via $$.Task.Token
}
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dynamodb          = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
sns               = boto3.client("sns",        region_name=os.environ["AWS_REGION"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def handler(event: dict, context) -> dict:
    waiver_id   = event["waiver_id"]
    task_token  = event["task_token"]       # injected by SFN state definition
    email_from  = event.get("email_from",  "")
    waiver_type = event.get("waiver_type", "")
    department  = event.get("department",  "")

    table         = dynamodb.Table(os.environ["WAIVER_TABLE"])
    portal_url    = os.environ["REVIEW_PORTAL_URL"]
    sns_topic_arn = os.environ["APPROVER_SNS_ARN"]
    now           = _now()

    # ----------------------------------------------------------------
    # 1. Write task token + status to DynamoDB
    # ----------------------------------------------------------------
    try:
        table.update_item(
            Key={"waiver_id": waiver_id},
            UpdateExpression=(
                "SET task_token = :tok, #st = :st, updated_at = :ts, "
                "history = list_append(if_not_exists(history, :empty), :h)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":tok":   task_token,
                ":st":    "pending_approval",
                ":ts":    now,
                ":h": [{
                    "timestamp": now,
                    "event":     "pending_approval",
                    "content":   "Waiver is ready for human review. Task token stored.",
                }],
                ":empty": [],
            },
        )
        logger.info("Task token stored | waiver_id=%s", waiver_id)
    except ClientError as exc:
        logger.error("DynamoDB update failed: %s", exc)
        raise

    # ----------------------------------------------------------------
    # 2. Notify approver via SNS → SES
    # ----------------------------------------------------------------
    review_link = f"{portal_url.rstrip('/')}?waiver_id={waiver_id}"
    message = (
        f"A waiver request requires your approval.\n\n"
        f"Waiver ID  : {waiver_id}\n"
        f"Requestor  : {email_from}\n"
        f"Type       : {waiver_type.replace('_', ' ').title()}\n"
        f"Department : {department}\n\n"
        f"Review and approve or reject here:\n{review_link}\n\n"
        f"--- Waiver Processing System"
    )

    try:
        sns.publish(
            TopicArn=sns_topic_arn,
            Subject=f"[Approval Required] Waiver {waiver_id} — {waiver_type.replace('_', ' ').title()}",
            Message=message,
        )
        logger.info("Approver notification sent | waiver_id=%s", waiver_id)
    except ClientError as exc:
        # Non-fatal — token is stored, workflow continues
        logger.error("SNS publish failed (non-fatal): %s", exc)

    return {"waiver_id": waiver_id, "status": "pending_approval"}
