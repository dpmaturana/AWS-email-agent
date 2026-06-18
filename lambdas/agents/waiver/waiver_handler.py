import json
import logging
from agent import create_waiver_agent

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """
    Entry point for the Waiver Processor agent.
    Invoked asynchronously by the Router agent (Agent 1) via invoke_waiver_agent tool.

    Expected event shape:
    {
        "email_from": str,
        "email_subject": str,
        "email_body": str,
        "department": str,
        "thread_id": str | None,
        "message_id": str,
        "attachments": [{ "filename": str, "s3_key": str, "content_type": str }]
    }
    """
    logger.info(f"Waiver agent invoked — message_id={event.get('message_id')}, thread_id={event.get('thread_id')}")

    email_from = event.get("email_from", "")
    email_subject = event.get("email_subject", "")
    email_body = event.get("email_body", "")
    department = event.get("department", "program_management")
    thread_id = event.get("thread_id") or None
    message_id = event.get("message_id", "")
    attachments = event.get("attachments", [])

    is_new_thread = thread_id is None

    if is_new_thread:
        prompt = f"""New waiver request received at IE University.

From: {email_from}
Subject: {email_subject}
Department: {department}
Message ID: {message_id}

Email body:
{email_body}

Attachments: {json.dumps(attachments)}

This is a new waiver request (no existing thread). Follow Scenario A from your instructions."""
    else:
        prompt = f"""Reply received for an existing waiver request.

From: {email_from}
Subject: {email_subject}
Department: {department}
Existing waiver ID (thread_id): {thread_id}
Message ID: {message_id}

Email body:
{email_body}

Attachments: {json.dumps(attachments)}

This is a reply to an existing waiver (thread_id: {thread_id}). Follow Scenario B from your instructions.
Start by calling get_waiver_state with waiver_id="{thread_id}" to reload the full context."""

    try:
        agent = create_waiver_agent()
        result = agent(prompt)

        logger.info(f"Waiver agent completed — message_id={message_id}")
        return {
            "statusCode": 200,
            "message_id": message_id,
            "thread_id": thread_id,
            "result": str(result),
        }

    except Exception as e:
        logger.error(f"Waiver agent failed — message_id={message_id}: {e}")
        raise
