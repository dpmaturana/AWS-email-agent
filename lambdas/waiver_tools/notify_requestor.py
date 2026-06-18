"""
NotifyRequestor Lambda
=======================
Called by both the Approved and Rejected states in the Step Functions workflow.
Sends the final outcome to the requestor via SNS → SES and updates DynamoDB.

Event shape (sent by Step Functions after task resolution):
{
  "waiver_id": "string",
  "decision":  "approved" | "rejected",
  "comment":   "string"               // reviewer comment
}
"""

import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"])
sns      = boto3.client("sns",        region_name=os.environ["AWS_REGION"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def handler(event: dict, context) -> dict:
    waiver_id = event["waiver_id"]
    decision  = event["decision"]           # "approved" | "rejected"
    comment   = event.get("comment", "")

    table         = dynamodb.Table(os.environ["WAIVER_TABLE"])
    sns_topic_arn = os.environ["REQUESTOR_SNS_ARN"]
    now           = _now()

    # ----------------------------------------------------------------
    # 1. Update DynamoDB
    # ----------------------------------------------------------------
    final_status = "approved" if decision == "approved" else "rejected"
    try:
        table.update_item(
            Key={"waiver_id": waiver_id},
            UpdateExpression=(
                "SET #st = :st, task_token = :null, updated_at = :ts, "
                "history = list_append(if_not_exists(history, :empty), :h)"
            ),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st":    final_status,
                ":null":  None,
                ":ts":    now,
                ":h": [{
                    "timestamp": now,
                    "event":     f"waiver_{final_status}",
                    "content":   f"Decision: {decision}. Comment: {comment or 'none'}",
                }],
                ":empty": [],
            },
        )
        logger.info("Waiver finalised | waiver_id=%s | decision=%s", waiver_id, decision)
    except ClientError as exc:
        logger.error("DynamoDB update failed: %s", exc)
        raise

    # ----------------------------------------------------------------
    # 2. Fetch requestor email
    # ----------------------------------------------------------------
    item       = table.get_item(Key={"waiver_id": waiver_id}).get("Item", {})
    email_from = item.get("email_from", "")

    # ----------------------------------------------------------------
    # 3. Notify requestor via SNS → SES
    # ----------------------------------------------------------------
    if decision == "approved":
        subject = f"Waiver Request Approved — Reference {waiver_id}"
        body    = (
            f"Dear Applicant,\n\n"
            f"We are pleased to inform you that your waiver request has been APPROVED.\n\n"
            f"Reference : {waiver_id}\n"
        )
    else:
        subject = f"Waiver Request Declined — Reference {waiver_id}"
        body    = (
            f"Dear Applicant,\n\n"
            f"We regret to inform you that your waiver request has been DECLINED.\n\n"
            f"Reference : {waiver_id}\n"
        )

    if comment:
        body += f"Reviewer Note : {comment}\n"
    body += "\nKind regards,\nWaiver Processing Team"

    try:
        sns.publish(
            TopicArn=sns_topic_arn,
            Subject=subject,
            Message=body,
            MessageAttributes={
                "email_to": {
                    "DataType":    "String",
                    "StringValue": email_from,
                }
            },
        )
        logger.info("Requestor notification sent | waiver_id=%s | email=%s", waiver_id, email_from)
    except ClientError as exc:
        logger.error("SNS publish to requestor failed: %s", exc)

    return {"waiver_id": waiver_id, "decision": decision, "notified": True}
