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

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
_agent_runtime = boto3.client("bedrock-agent-runtime")
_ssm = boto3.client("ssm")

RAW_EMAILS_BUCKET = os.environ.get("RAW_EMAILS_BUCKET", "")
WAIVER_TABLE_NAME = os.environ.get("WAIVER_TABLE_NAME", "waivers")
WAIVER_MESSAGE_ID_INDEX = os.environ.get("WAIVER_MESSAGE_ID_INDEX", "message_id_index")

# Router agent id/alias: prefer direct env vars (handy for local tests), else
# resolve from the SSM parameters published by AgentStack.
ROUTER_AGENT_ID_PARAM = os.environ.get("ROUTER_AGENT_ID_PARAM", "")
ROUTER_AGENT_ALIAS_PARAM = os.environ.get("ROUTER_AGENT_ALIAS_PARAM", "")

_agent_ids_cache = None


def _resolve_agent_ids():
    """Return (agent_id, agent_alias_id), cached across warm invocations."""
    global _agent_ids_cache
    if _agent_ids_cache is not None:
        return _agent_ids_cache

    agent_id = os.environ.get("AGENT_CORE_ROUTER_ID", "")
    alias_id = os.environ.get("AGENT_CORE_ROUTER_ALIAS", "")

    def _get_param(name):
        try:
            return _ssm.get_parameter(Name=name)["Parameter"]["Value"]
        except Exception as exc:
            print(f"[ingestion] could not read SSM param {name}: {exc}")
            return ""

    if not agent_id and ROUTER_AGENT_ID_PARAM:
        agent_id = _get_param(ROUTER_AGENT_ID_PARAM)
    if not alias_id and ROUTER_AGENT_ALIAS_PARAM:
        alias_id = _get_param(ROUTER_AGENT_ALIAS_PARAM)

    _agent_ids_cache = (agent_id, alias_id)
    return _agent_ids_cache


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
# 4. Invoke the Bedrock router agent
# --------------------------------------------------------------------------- #
def _invoke_agent(payload):
    agent_id, alias_id = _resolve_agent_ids()
    if not (agent_id and alias_id):
        print("[ingestion] router agent id/alias not available — skipping agent invoke.")
        return None

    session_id = payload["thread_id"] or _safe_prefix(payload["message_id"])
    response = _agent_runtime.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=json.dumps(payload),
    )

    # invoke_agent returns a streaming completion; drain it.
    completion = ""
    for chunk_event in response.get("completion", []):
        chunk = chunk_event.get("chunk")
        if chunk and chunk.get("bytes"):
            completion += chunk["bytes"].decode("utf-8", "replace")
    return completion


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
