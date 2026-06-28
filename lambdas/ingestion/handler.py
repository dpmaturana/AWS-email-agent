"""Ingestion Lambda — entry point of the whole system (Person 1).

Responsibilities:
  1. Obtain the raw .eml (from an S3/SES event, or a direct test payload).
  2. Parse it into structured fields (from / to / subject / body / attachments).
  3. Read the In-Reply-To header to detect a reply to an existing thread.
  4. Look up the thread in DynamoDB (Person 4's `waivers` table) via the
     `message_id_index` GSI to resolve thread_id (== waiver_id).
  5. Build the AgentCore payload (Contract 1 — docs/contracts.md).
  6. Invoke the Bedrock router agent with that payload.

Invocation modes
----------------
* S3 event       — SES receipt rule stored the .eml in raw-emails-bucket.
                   event = {"Records": [{"s3": {"bucket": {...}, "object": {...}}}]}
* SES event      — {"Records": [{"ses": {"mail": {...}}}]} (objectKey under a prefix)
* Direct test    — sandbox demo. Pass ONE of:
                     {"raw_email": "<full MIME string>"}
                     {"bucket": "...", "key": "..."}   (or {"raw_email_s3_key": "..."})
"""

import base64
import email
import json
import os
import re
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
_lambda_client = boto3.client("lambda")
_ssm = boto3.client("ssm")
_agentcore_client = boto3.client("bedrock-agentcore")

# Router AgentCore runtime ARN — resolved from env or SSM (published by AgentStack).
# When set, the router agent runs on Amazon Bedrock AgentCore (preferred);
# otherwise we fall back to invoking the router agent Lambda.
ROUTER_RUNTIME_ARN_PARAM = os.environ.get("ROUTER_RUNTIME_ARN_PARAM", "/email-agent/router/runtime-arn")
_router_runtime_arn_cache = None


def _resolve_router_runtime_arn() -> str:
    global _router_runtime_arn_cache
    if _router_runtime_arn_cache is not None:
        return _router_runtime_arn_cache
    arn = os.environ.get("ROUTER_AGENT_RUNTIME_ARN", "")
    if not arn and ROUTER_RUNTIME_ARN_PARAM:
        try:
            arn = _ssm.get_parameter(Name=ROUTER_RUNTIME_ARN_PARAM)["Parameter"]["Value"]
        except Exception as exc:
            print(f"[ingestion] could not read SSM param {ROUTER_RUNTIME_ARN_PARAM}: {exc}")
    _router_runtime_arn_cache = arn or ""
    return _router_runtime_arn_cache

RAW_EMAILS_BUCKET = os.environ.get("RAW_EMAILS_BUCKET", "")
WAIVER_TABLE_NAME = os.environ.get("WAIVER_TABLE_NAME", "waivers")
WAIVER_MESSAGE_ID_INDEX = os.environ.get("WAIVER_MESSAGE_ID_INDEX", "message_id_index")

ROUTER_LAMBDA_ARN_PARAM = os.environ.get("ROUTER_LAMBDA_ARN_PARAM", "/email-agent/router/lambda-arn")

_router_lambda_arn_cache = None


def _resolve_router_lambda_arn() -> str:
    global _router_lambda_arn_cache
    if _router_lambda_arn_cache:
        return _router_lambda_arn_cache
    arn = os.environ.get("ROUTER_LAMBDA_ARN", "")
    if not arn:
        try:
            arn = _ssm.get_parameter(Name=ROUTER_LAMBDA_ARN_PARAM)["Parameter"]["Value"]
        except Exception as exc:
            print(f"[ingestion] could not read SSM param {ROUTER_LAMBDA_ARN_PARAM}: {exc}")
    _router_lambda_arn_cache = arn
    return arn


# --------------------------------------------------------------------------- #
# 1. Load the raw email bytes
# --------------------------------------------------------------------------- #
def _load_raw_email(event):
    """Return (raw_bytes, source_s3_key_or_none) from any supported event shape."""
    # Direct test payload: full MIME string inline
    if isinstance(event, dict) and event.get("raw_email"):
        return event["raw_email"].encode("utf-8"), None

    # Direct test payload: explicit bucket/key
    bucket = event.get("bucket") if isinstance(event, dict) else None
    key = event.get("key") or event.get("raw_email_s3_key") if isinstance(event, dict) else None

    # S3 event notification
    if not key and isinstance(event, dict) and event.get("Records"):
        record = event["Records"][0]
        if "s3" in record:
            bucket = record["s3"]["bucket"]["name"]
            key = record["s3"]["object"]["key"]
        elif "ses" in record:
            # SES "S3" action stores the message under <prefix><messageId>.
            bucket = RAW_EMAILS_BUCKET
            key = record["ses"]["mail"]["messageId"]

    if not key:
        raise ValueError(
            "Could not determine the raw email source from the event. "
            "Provide 'raw_email', 'raw_email_s3_key', or an S3/SES event."
        )

    bucket = bucket or RAW_EMAILS_BUCKET
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read(), key


# --------------------------------------------------------------------------- #
# 2. Parse the email
# --------------------------------------------------------------------------- #
def _safe_prefix(message_id):
    """Filesystem/S3-safe prefix derived from the Message-ID."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", message_id.strip("<>")) or uuid.uuid4().hex


def _extract_body_text(msg):
    """Return the best-effort text/plain body."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_content().strip()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", "replace").strip()
        # Fallback to text/html stripped-ish if no plain part
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                try:
                    return part.get_content().strip()
                except Exception:
                    pass
        return ""
    try:
        return msg.get_content().strip()
    except Exception:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace").strip()


def _extract_attachments(msg, message_id):
    """Persist each attachment to raw-emails-bucket and return contract records."""
    attachments = []
    prefix = f"attachments/{_safe_prefix(message_id)}"
    for part in msg.walk():
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "")
        if not filename and disposition != "attachment":
            continue
        if not filename:
            continue

        data = part.get_payload(decode=True) or b""
        content_type = part.get_content_type()
        s3_key = f"{prefix}/{re.sub(r'[^A-Za-z0-9._-]', '_', filename)}"

        if RAW_EMAILS_BUCKET:
            s3.put_object(
                Bucket=RAW_EMAILS_BUCKET,
                Key=s3_key,
                Body=data,
                ContentType=content_type,
            )
        attachments.append(
            {"filename": filename, "s3_key": s3_key, "content_type": content_type}
        )
    return attachments


def _parse_email(raw_bytes):
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"<{uuid.uuid4().hex}@ingestion.local>"

    in_reply_to = msg.get("In-Reply-To")
    in_reply_to = in_reply_to.strip() if in_reply_to else None

    # Timestamp from the Date header, else "now".
    timestamp = None
    if msg.get("Date"):
        try:
            timestamp = parsedate_to_datetime(msg["Date"]).astimezone(timezone.utc).isoformat()
        except Exception:
            timestamp = None
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "timestamp": timestamp,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "")),
        "body_text": _extract_body_text(msg),
        "attachments": _extract_attachments(msg, message_id),
    }


# --------------------------------------------------------------------------- #
# 3. Thread detection — resolve thread_id (waiver_id) from DynamoDB
# --------------------------------------------------------------------------- #
def _lookup_thread_id(in_reply_to):
    """Query the message_id GSI; return the waiver_id of the matching thread."""
    if not in_reply_to:
        return None
    try:
        table = dynamodb.Table(WAIVER_TABLE_NAME)
        resp = table.query(
            IndexName=WAIVER_MESSAGE_ID_INDEX,
            KeyConditionExpression=boto3.dynamodb.conditions.Key("message_id").eq(in_reply_to),
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            return items[0].get("waiver_id")
    except Exception as exc:  # table may not exist yet during early integration
        print(f"[ingestion] thread lookup failed (continuing as new thread): {exc}")
    return None


# --------------------------------------------------------------------------- #
# 4. Invoke the router agent Lambda (Strands agent)
# --------------------------------------------------------------------------- #
def _invoke_router_runtime(payload, runtime_arn):
    """Invoke the router agent deployed on Amazon Bedrock AgentCore Runtime."""
    seed = payload.get("thread_id") or payload.get("message_id") or uuid.uuid4().hex
    session_id = (re.sub(r"[^A-Za-z0-9]", "", seed) + uuid.uuid4().hex)[:64]
    if len(session_id) < 33:
        session_id = (session_id + uuid.uuid4().hex)[:64]
    resp = _agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    raw = resp["response"].read()
    try:
        data = json.loads(raw)
        return data.get("result") or json.dumps(data)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)


def _invoke_agent(payload):
    # Preferred path: the router agent runs on Amazon Bedrock AgentCore.
    runtime_arn = _resolve_router_runtime_arn()
    if runtime_arn:
        return _invoke_router_runtime(payload, runtime_arn)

    # Fallback: invoke the router agent Lambda.
    arn = _resolve_router_lambda_arn()
    if not arn:
        print("[ingestion] router Lambda ARN not available — skipping agent invoke.")
        return None

    response = _lambda_client.invoke(
        FunctionName=arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    result = json.loads(response["Payload"].read())
    return result.get("result") or json.dumps(result)


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #
def handler(event, context):
    print(f"[ingestion] event keys: {list(event) if isinstance(event, dict) else type(event)}")

    raw_bytes, source_key = _load_raw_email(event)
    parsed = _parse_email(raw_bytes)

    thread_id = _lookup_thread_id(parsed["in_reply_to"])

    payload = {
        "message_id": parsed["message_id"],
        "thread_id": thread_id,
        "in_reply_to": parsed["in_reply_to"],
        "timestamp": parsed["timestamp"],
        "from": parsed["from"],
        "to": parsed["to"],
        "subject": parsed["subject"],
        "body_text": parsed["body_text"],
        "attachments": parsed["attachments"],
        "is_new_thread": thread_id is None,
    }

    print(f"[ingestion] payload: {json.dumps(payload)[:1500]}")

    agent_response = _invoke_agent(payload)

    return {
        "statusCode": 200,
        "payload": payload,
        "source_s3_key": source_key,
        "agent_response": agent_response,
    }


