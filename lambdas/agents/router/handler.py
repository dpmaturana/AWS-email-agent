import json
import logging
from agent import create_router_agent

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict, context) -> dict:
    """
    Entry point for the Email Router agent.
    Receives the email payload from the ingestion Lambda via Bedrock AgentCore
    and runs the router agent to classify and act on the email.

    Expected event shape (Contract 1 from docs/contracts.md):
    {
        "message_id": str,
        "thread_id": str | None,
        "in_reply_to": str | None,
        "timestamp": str,
        "from": str,
        "to": str,
        "subject": str,
        "body_text": str,
        "attachments": [{ "filename": str, "s3_key": str, "content_type": str }],
        "is_new_thread": bool
    }
    """
    logger.info(f"Router agent invoked for message_id={event.get('message_id')}")

    email_from = event.get("from", "")
    subject = event.get("subject", "")
    body = event.get("body_text", "")
    thread_id = event.get("thread_id") or ""
    message_id = event.get("message_id", "")
    attachments = event.get("attachments", [])
    is_new_thread = event.get("is_new_thread", True)

    # Build the prompt for the agent
    # Include thread context if this is a reply to an existing waiver
    if not is_new_thread and thread_id:
        prompt = f"""New email received that is a REPLY to an existing thread (waiver_id: {thread_id}).

From: {email_from}
Subject: {subject}
Message ID: {message_id}

Body:
{body}

Attachments: {json.dumps(attachments)}

This is a reply to an existing waiver request. Invoke the waiver agent with the thread_id so it can continue processing."""
    else:
        prompt = f"""New email received at IE University Student Services.

From: {email_from}
Subject: {subject}
Message ID: {message_id}

Body:
{body}

Attachments: {json.dumps(attachments)}

Please classify this email and take the appropriate action."""

    try:
        agent = create_router_agent()
        result = agent(prompt)

        logger.info(f"Router agent completed for message_id={message_id}")
        logger.info(f"Agent result: {result}")

        return {
            "statusCode": 200,
            "message_id": message_id,
            "result": str(result),
        }

    except Exception as e:
        logger.error(f"Router agent failed for message_id={message_id}: {e}")
        raise
