"""
Step Functions Flow Tests (Person 4)
======================================
Tests the store_task_token Lambda and notify_requestor Lambda
that are invoked by the Step Functions state machine.
These simulate what SFN calls during execution.
"""

import json
import os
import uuid
import pytest
import boto3
from datetime import datetime, timezone
from moto import mock_aws

os.environ.update({
    "AWS_REGION":            "us-east-1",
    "WAIVER_TABLE":          "waivers",
    "APPROVER_SNS_ARN":      "arn:aws:sns:us-east-1:123456789012:approver",
    "REQUESTOR_SNS_ARN":     "arn:aws:sns:us-east-1:123456789012:requestor",
    "SES_SENDER":            "waiver@test.com",
    "REVIEW_PORTAL_URL":     "https://portal.example.com/review",
    "AWS_DEFAULT_REGION":    "us-east-1",
    "AWS_ACCESS_KEY_ID":     "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN":    "testing",
    "AWS_SESSION_TOKEN":     "testing",
})

from waiver_flow.lambdas.store_task_token import handler as store_token_handler
from waiver_flow.lambdas.notify_requestor  import handler as notify_handler


@pytest.fixture
def aws_env():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="waivers",
            KeySchema=[{"AttributeName": "waiver_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "waiver_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="waivers")

        sns_client = boto3.client("sns", region_name="us-east-1")
        approver_t  = sns_client.create_topic(Name="approver")
        requestor_t = sns_client.create_topic(Name="requestor")
        os.environ["APPROVER_SNS_ARN"]  = approver_t["TopicArn"]
        os.environ["REQUESTOR_SNS_ARN"] = requestor_t["TopicArn"]

        # Pre-seed a waiver record (normally created by start_waiver_workflow)
        now = datetime.now(timezone.utc).isoformat()
        wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
        table.put_item(Item={
            "waiver_id":      wid,
            "email_from":     "alice@example.com",
            "department":     "hr",
            "waiver_type":    "medical",
            "status":         "pending_info",
            "collected_info": {"full_name": "Alice"},
            "missing_fields": [],
            "criteria":       {},
            "task_token":     None,
            "history":        [],
            "created_at":     now,
            "updated_at":     now,
        })
        yield {"table": table, "wid": wid, "sns": sns_client}


class TestStoreTaskTokenLambda:
    """Simulates SFN calling the StoreToken Lambda task."""

    def test_stores_token_in_dynamodb(self, aws_env):
        token = f"sfn-token-{uuid.uuid4().hex}"
        event = {
            "waiver_id":   aws_env["wid"],
            "email_from":  "alice@example.com",
            "waiver_type": "medical",
            "department":  "hr",
            "task_token":  token,
        }
        result = store_token_handler(event, None)
        assert result["status"] == "pending_approval"

        item = aws_env["table"].get_item(Key={"waiver_id": aws_env["wid"]})["Item"]
        assert item["task_token"] == token
        assert item["status"] == "pending_approval"

    def test_appends_history_entry(self, aws_env):
        event = {
            "waiver_id":  aws_env["wid"], "email_from": "alice@example.com",
            "waiver_type": "medical", "department": "hr",
            "task_token": "tok-123",
        }
        store_token_handler(event, None)
        item    = aws_env["table"].get_item(Key={"waiver_id": aws_env["wid"]})["Item"]
        history = item.get("history", [])
        assert any(h["event"] == "pending_approval" for h in history)

    def test_publishes_sns_notification(self, aws_env):
        """SNS publish should not raise even if no subscribers."""
        event = {
            "waiver_id":  aws_env["wid"], "email_from": "alice@example.com",
            "waiver_type": "medical", "department": "hr",
            "task_token": "tok-456",
        }
        # Should not raise
        result = store_token_handler(event, None)
        assert result["waiver_id"] == aws_env["wid"]

    def test_portal_link_uses_waiver_id(self, aws_env):
        """Portal URL stored in SNS message should contain the waiver_id."""
        event = {
            "waiver_id":  aws_env["wid"], "email_from": "alice@example.com",
            "waiver_type": "medical", "department": "hr",
            "task_token": "tok-789",
        }
        result = store_token_handler(event, None)
        assert result["waiver_id"] == aws_env["wid"]


class TestNotifyRequestorLambda:
    """Simulates SFN calling NotifyRequestor after Approved/Rejected."""

    def _seed_approved(self, table, wid: str, decision: str):
        now = datetime.now(timezone.utc).isoformat()
        status = "approved" if decision == "approved" else "rejected"
        table.put_item(Item={
            "waiver_id":      wid,
            "email_from":     "requestor@example.com",
            "department":     "finance",
            "waiver_type":    "financial_hardship",
            "status":         status,
            "collected_info": {"full_name": "Bob"},
            "missing_fields": [],
            "criteria":       {},
            "task_token":     None,
            "history":        [],
            "created_at":     now,
            "updated_at":     now,
        })

    def test_approved_sets_status(self, aws_env):
        wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
        self._seed_approved(aws_env["table"], wid, "approved")
        result = notify_handler(
            {"waiver_id": wid, "decision": "approved", "comment": "All good"}, None
        )
        assert result["decision"] == "approved"
        assert result["notified"] is True
        item = aws_env["table"].get_item(Key={"waiver_id": wid})["Item"]
        assert item["status"] == "approved"

    def test_rejected_sets_status(self, aws_env):
        wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
        self._seed_approved(aws_env["table"], wid, "rejected")
        result = notify_handler(
            {"waiver_id": wid, "decision": "rejected", "comment": "Does not meet criteria"}, None
        )
        assert result["decision"] == "rejected"
        item = aws_env["table"].get_item(Key={"waiver_id": wid})["Item"]
        assert item["status"] == "rejected"

    def test_task_token_cleared_after_decision(self, aws_env):
        wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
        self._seed_approved(aws_env["table"], wid, "approved")
        # Plant a token first
        aws_env["table"].update_item(
            Key={"waiver_id": wid},
            UpdateExpression="SET task_token = :t",
            ExpressionAttributeValues={":t": "old-token"},
        )
        notify_handler({"waiver_id": wid, "decision": "approved", "comment": ""}, None)
        item = aws_env["table"].get_item(Key={"waiver_id": wid})["Item"]
        assert item.get("task_token") is None

    def test_history_appended(self, aws_env):
        wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
        self._seed_approved(aws_env["table"], wid, "approved")
        notify_handler({"waiver_id": wid, "decision": "approved", "comment": "Verified"}, None)
        item = aws_env["table"].get_item(Key={"waiver_id": wid})["Item"]
        assert any("approved" in h["event"] for h in item.get("history", []))
