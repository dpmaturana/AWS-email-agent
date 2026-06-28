"""Amazon Bedrock AgentCore Runtime entrypoint for the Email Router agent.

Wraps the same Strands agent used in the Lambda handler (create_router_agent) in
the AgentCore Runtime contract. Deployed via the bedrock-agentcore starter
toolkit (direct_code_deploy — no local Docker).

Invocation payload = Contract 1 from the ingestion Lambda:
{
  "message_id", "thread_id", "in_reply_to", "timestamp",
  "from", "to", "subject", "body_text", "attachments", "is_new_thread"
}
"""
import json

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from agent import create_router_agent
from tools import reset_action_guard

app = BedrockAgentCoreApp()


def _build_prompt(event: dict) -> str:
    email_from = event.get("from", "")
    subject = event.get("subject", "")
    body = event.get("body_text", "")
    thread_id = event.get("thread_id") or ""
    message_id = event.get("message_id", "")
    attachments = event.get("attachments", [])
    is_new_thread = event.get("is_new_thread", True)

    if not is_new_thread and thread_id:
        return f"""New email received that is a REPLY to an existing thread (waiver_id: {thread_id}).

From: {email_from}
Subject: {subject}
Message ID: {message_id}

Body:
{body}

Attachments: {json.dumps(attachments)}

This is a reply to an existing waiver request. Invoke the waiver agent with the thread_id so it can continue processing."""

    return f"""New email received at IE University Student Services.

From: {email_from}
Subject: {subject}
Message ID: {message_id}

Body:
{body}

Attachments: {json.dumps(attachments)}

Please classify this email and take the appropriate action."""


@app.entrypoint
def invoke(payload, context=None):
    """AgentCore entrypoint — runs the router Strands agent over the email payload."""
    message_id = payload.get("message_id", "")
    prompt = _build_prompt(payload)
    reset_action_guard()  # fresh single-action guard per invocation (process is reused)
    agent = create_router_agent()
    result = agent(prompt)
    return {"message_id": message_id, "result": str(result)}


if __name__ == "__main__":
    app.run()
