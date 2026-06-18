import json
import os
import boto3
from strands import tool

ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "eu-west-1"))

EMAIL_FROM = os.environ["EMAIL_FROM"]
RAG_LAMBDA_ARN = os.environ["RAG_LAMBDA_ARN"]
WAIVER_AGENT_LAMBDA_ARN = os.environ["WAIVER_AGENT_LAMBDA_ARN"]


@tool
def classify_email(subject: str, body: str) -> dict:
    """
    Classifies the email intent and identifies the department.
    Returns the classification result with intent and department.

    Args:
        subject: Email subject line
        body: Email body text

    Returns:
        dict with keys: intent (forward|rag|waiver), department (hr|legal|it|general)
    """
    # This tool is a structured output helper — the agent itself does the
    # classification via its LLM reasoning. This tool formalizes the result.
    # The agent calls this tool with its own classification decision.
    return {
        "intent": "unknown",
        "department": "general",
        "note": "Agent should call this tool with its classification decision"
    }


@tool
def route_email(destination_email: str, original_subject: str, original_body: str, original_from: str) -> dict:
    """
    Forwards the email to the appropriate department team via SES.
    Use this when the email should be handled by a human team, not answered automatically.

    Args:
        destination_email: The team email address to forward to
        original_subject: The original email subject
        original_body: The original email body
        original_from: The original sender email address

    Returns:
        dict with success status
    """
    try:
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [destination_email]},
            Message={
                "Subject": {"Data": f"[Forwarded] {original_subject}"},
                "Body": {
                    "Text": {
                        "Data": f"Forwarded from: {original_from}\n\n{original_body}"
                    }
                },
            },
        )
        return {"success": True, "forwarded_to": destination_email}
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool
def query_knowledge_base_and_reply(
    query: str,
    department: str,
    reply_to: str,
    original_subject: str
) -> dict:
    """
    Queries the IE University knowledge base for the given question and sends
    the answer directly to the student via email.
    Use this when the email contains a question that can be answered with
    internal IE documentation (policies, procedures, FAQs, deadlines).

    Args:
        query: The question or topic to search for in the knowledge base
        department: The department context (hr|legal|it|general)
        reply_to: The student email address to send the answer to
        original_subject: The original email subject for the reply

    Returns:
        dict with success status and answer summary
    """
    try:
        # Call the RAG Lambda (implemented by Person 3)
        rag_response = lambda_client.invoke(
            FunctionName=RAG_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "query": query,
                "department": department,
                "top_k": 5
            }),
        )
        rag_result = json.loads(rag_response["Payload"].read())

        if not rag_result or not rag_result.get("chunks"):
            return {
                "success": False,
                "reason": "No relevant information found in knowledge base"
            }

        # Build answer from retrieved chunks
        chunks = rag_result["chunks"]
        context = "\n\n".join([c["content"] for c in chunks])

        # Send reply to student
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [reply_to]},
            Message={
                "Subject": {"Data": f"Re: {original_subject}"},
                "Body": {
                    "Text": {
                        "Data": (
                            "Dear student,\n\n"
                            "Thank you for contacting IE University Student Services.\n\n"
                            f"{context}\n\n"
                            "If you have further questions, please don't hesitate to reach out.\n\n"
                            "Best regards,\n"
                            "IE University Student Services"
                        )
                    }
                },
            },
        )
        return {
            "success": True,
            "sources": [c.get("source") for c in chunks],
            "reply_sent_to": reply_to
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool
def invoke_waiver_agent(
    email_from: str,
    email_subject: str,
    email_body: str,
    department: str,
    thread_id: str,
    message_id: str,
    attachments: list
) -> dict:
    """
    Delegates waiver processing to Agent 2 (Waiver Processor).
    Use this when the email is identified as a waiver request of any kind
    (academic requirement waiver, tuition exception, late enrollment, etc.)

    Args:
        email_from: The student email address
        email_subject: The email subject
        email_body: The email body
        department: The department context (hr|legal|it|general)
        thread_id: Existing waiver_id if this is a reply, empty string if new
        message_id: The email message ID
        attachments: List of attachment metadata dicts

    Returns:
        dict with waiver_id and status
    """
    try:
        payload = {
            "email_from": email_from,
            "email_subject": email_subject,
            "email_body": email_body,
            "department": department,
            "thread_id": thread_id or None,
            "message_id": message_id,
            "attachments": attachments,
        }
        response = lambda_client.invoke(
            FunctionName=WAIVER_AGENT_LAMBDA_ARN,
            InvocationType="Event",  # async — waiver processing can take time
            Payload=json.dumps(payload),
        )
        return {
            "success": True,
            "status_code": response["StatusCode"],
            "note": "Waiver agent invoked asynchronously"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
