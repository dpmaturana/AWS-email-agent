"""Amazon Bedrock AgentCore Runtime entrypoint for the Waiver Processor agent.

This wraps the same Strands agent used elsewhere (create_waiver_agent) in the
AgentCore Runtime contract so the agent runs on Bedrock AgentCore rather than a
plain Lambda. Deployed via the bedrock-agentcore starter toolkit
(direct_code_deploy — no local Docker).

Invocation payload (sent by the router agent's invoke_waiver_agent tool):
{
  "email_from": str, "email_subject": str, "email_body": str,
  "department": str, "thread_id": str | null, "message_id": str,
  "attachments": [ ... ]
}
"""
import json

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from waiver_agent import create_waiver_agent

app = BedrockAgentCoreApp()


def _build_prompt(event: dict) -> str:
    email_from = event.get("email_from", "")
    email_subject = event.get("email_subject", "")
    email_body = event.get("email_body", "")
    department = event.get("department", "program_management")
    thread_id = event.get("thread_id") or None
    message_id = event.get("message_id", "")
    attachments = event.get("attachments", [])

    if thread_id is None:
        return f"""New waiver request received at IE University.

From: {email_from}
Subject: {email_subject}
Department: {department}
Message ID: {message_id}

Email body:
{email_body}

Attachments: {json.dumps(attachments)}

This is a new waiver request (no existing thread). Follow Scenario A from your instructions."""

    return f"""Reply received for an existing waiver request.

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


@app.entrypoint
def invoke(payload, context=None):
    """AgentCore entrypoint — runs the waiver Strands agent over the email payload."""
    message_id = payload.get("message_id", "")
    prompt = _build_prompt(payload)
    agent = create_waiver_agent()
    result = agent(prompt)
    return {"message_id": message_id, "thread_id": payload.get("thread_id"), "result": str(result)}


if __name__ == "__main__":
    app.run()
