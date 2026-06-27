import json
import os
import re
import uuid
import boto3
from datetime import datetime, timezone
from strands import tool

_REGION = os.environ.get("AWS_REGION", "eu-west-1")
ses = boto3.client("ses", region_name=_REGION)
s3 = boto3.client("s3", region_name=_REGION)
lambda_client = boto3.client("lambda", region_name=_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=_REGION)
agentcore_client = boto3.client("bedrock-agentcore", region_name=_REGION)

EMAIL_FROM = os.environ["EMAIL_FROM"]
RAG_LAMBDA_ARN = os.environ["RAG_LAMBDA_ARN"]
WAIVER_AGENT_LAMBDA_ARN = os.environ.get("WAIVER_AGENT_LAMBDA_ARN", "")
# When set, the waiver agent runs on Amazon Bedrock AgentCore (preferred);
# otherwise we fall back to invoking the waiver agent Lambda.
WAIVER_AGENT_RUNTIME_ARN = os.environ.get("WAIVER_AGENT_RUNTIME_ARN", "")
RAW_EMAILS_BUCKET = os.environ.get("RAW_EMAILS_BUCKET", "")

_DEPT_SLUG = {
    "administracionclientes@ie.edu": "administration",
    "sci-tech@ie.edu": "program_management",
    "student.services@ie.edu": "student_services",
    "registrar@ie.edu": "registrar",
    "campus.life@ie.edu": "campus_life",
    "entrepreneurship@ie.edu": "venture_lab",
    "jobmarketimmersion@ie.edu": "job_market",
}


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


def _save_email_to_s3(prefix: str, mail: dict, ts: str, label: str) -> str | None:
    """Write a mail dict to s3://RAW_EMAILS_BUCKET/{prefix}/{ts}_{label}.json"""
    if not RAW_EMAILS_BUCKET:
        return None
    safe_label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    key = f"{prefix}/{ts}_{safe_label}.json"
    s3.put_object(
        Bucket=RAW_EMAILS_BUCKET,
        Key=key,
        Body=json.dumps(mail, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return key


@tool
def route_email(
    destination_email: str,
    original_subject: str,
    original_body: str,
    original_from: str,
    student_reply: str,
) -> dict:
    """
    Replies to the student and CC's the appropriate department.

    Sends two emails:
    1. A reply to the student informing them which department will follow up.
    2. A CC to the department with the full thread context.
    If SES cannot deliver (sandbox), both are saved to S3 instead.
    The CC email is always saved to routed/{dept}/ as a record.

    Args:
        destination_email: The department team email address
        original_subject: The original email subject
        original_body: The original email body text
        original_from: The student email address
        student_reply: English reply to the student. Mention the department by name
            and what they will help with. Write as IE University Student Services.
            Example: "Thank you for reaching out. I have forwarded your payment
            inquiry to our Administration team — they will get back to you shortly."

    Returns:
        dict with delivery status for student reply and department CC
    """
    dept = _DEPT_SLUG.get(destination_email.lower(),
                          re.sub(r"[^a-z0-9]+", "_", destination_email.split("@")[0]))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # The CC email: what the department receives (reply + original quoted)
    cc_body = (
        f"{student_reply}\n\n"
        f"--- Original message ---\n"
        f"From: {original_from}\n"
        f"Subject: {original_subject}\n\n"
        f"{original_body}"
    )
    cc_mail = {
        "date": ts,
        "from": EMAIL_FROM,
        "to": original_from,
        "cc": destination_email,
        "subject": f"Re: {original_subject}",
        "body": cc_body,
    }

    results = {}

    # 1. Reply to student
    try:
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [original_from]},
            Message={
                "Subject": {"Data": f"Re: {original_subject}"},
                "Body": {"Text": {"Data": student_reply}},
            },
        )
        results["student_reply"] = "sent"
    except Exception:
        results["student_reply"] = "ses_unavailable"

    # 2. CC department
    try:
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [destination_email]},
            Message={
                "Subject": {"Data": f"Re: {original_subject}"},
                "Body": {"Text": {"Data": cc_body}},
            },
        )
        results["department_cc"] = "sent"
    except Exception:
        results["department_cc"] = "ses_unavailable"

    # Always save the CC email to routed/{dept}/ as the paper trail
    key = _save_email_to_s3(f"routed/{dept}", cc_mail, ts, original_from)
    results["s3_record"] = key

    return {"success": True, **results}


@tool
def query_knowledge_base_and_reply(
    query: str,
    program: str,
    topic: str,
    student_name: str,
    reply_to: str,
    original_subject: str,
    original_body: str,
) -> dict:
    """
    Queries the IE University knowledge base for the given question and sends
    a personalised, natural reply directly to the student via email.
    Use this when the email contains a question that can be answered with
    internal IE documentation (policies, procedures, FAQs, deadlines).

    Args:
        query: The question or topic to search for in the knowledge base
        program: The program or department that owns the documents.
            Use "hr", "legal", "it", or "general" for cross-program topics.
            Use the program code (e.g. "MCSBT") for program-specific questions.
        topic: The sub-area within the program to narrow the search.
            Known MCSBT topics: "general_information", "capstone_project".
            Use empty string "" to search all topics within the program.
        student_name: The student's first name, used to personalise the reply.
        reply_to: The student email address to send the reply to.
        original_subject: The original email subject for the reply thread.
        original_body: The full body of the student's email, for context.

    Returns:
        dict with success status and answer summary
    """
    try:
        # Call the RAG Lambda
        rag_response = lambda_client.invoke(
            FunctionName=RAG_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "query":   query,
                "program": program,
                "topic":   topic or None,
                "top_k":   5
            }),
        )
        rag_result = json.loads(rag_response["Payload"].read())

        # RAG Lambda returns a plain list of {content, source, score} dicts
        chunks = rag_result if isinstance(rag_result, list) else rag_result.get("chunks", [])
        if not chunks:
            return {
                "success": False,
                "reason": "No relevant information found in knowledge base"
            }

        context = "\n\n---\n\n".join([c["content"] for c in chunks])

        synthesis_prompt = (
            f"You are a Student Services advisor at IE University writing a reply email.\n\n"
            f"Student name: {student_name}\n"
            f"Student email:\n{original_body}\n\n"
            f"Using ONLY the following excerpts from official IE documentation, write the email BODY only. "
            f"Do NOT include a subject line, headers, or metadata — start directly with the greeting.\n\n"
            f"The body must include:\n"
            f"- A warm greeting addressing the student by first name\n"
            f"- A clear, concise answer (do not paste raw text from the docs — summarise it)\n"
            f"- A friendly closing and signature as 'IE University Student Services'\n\n"
            f"If the documentation does not fully answer the question, say so and offer to help further.\n\n"
            f"DOCUMENTATION EXCERPTS:\n{context}"
        )

        synthesis_response = bedrock_runtime.invoke_model(
            modelId="eu.amazon.nova-pro-v1:0",
            body=json.dumps({
                "messages": [{"role": "user", "content": [{"text": synthesis_prompt}]}],
                "inferenceConfig": {"maxTokens": 600, "temperature": 0.3},
            }),
            contentType="application/json",
            accept="application/json",
        )
        synthesis_body = json.loads(synthesis_response["body"].read())
        raw_reply = synthesis_body["output"]["message"]["content"][0]["text"].strip()
        # Strip any "Subject:..." line Nova Pro may prepend despite instructions
        reply_body = "\n".join(
            line for i, line in enumerate(raw_reply.splitlines())
            if not (i == 0 and line.lower().startswith("subject:"))
        ).strip()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # Send reply to student
        try:
            ses.send_email(
                Source=EMAIL_FROM,
                Destination={"ToAddresses": [reply_to]},
                Message={
                    "Subject": {"Data": f"Re: {original_subject}"},
                    "Body": {"Text": {"Data": reply_body}},
                },
            )
        except Exception:
            pass  # sandbox — fall through to S3 save

        # Save the reply email to responses/ — same structure as routed emails
        _save_email_to_s3("responses", {
            "date": ts,
            "from": EMAIL_FROM,
            "to": reply_to,
            "subject": f"Re: {original_subject}",
            "body": reply_body,
        }, ts, reply_to)

        return {
            "success": True,
            "sources": [c.get("source") for c in chunks],
            "reply_sent_to": reply_to,
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

        # Preferred path: the waiver agent runs on Amazon Bedrock AgentCore.
        if WAIVER_AGENT_RUNTIME_ARN:
            # AgentCore requires a session id of at least 33 chars; reuse the
            # thread so replies share the same runtime session.
            seed = (thread_id or message_id or uuid.uuid4().hex)
            session_id = (re.sub(r"[^A-Za-z0-9]", "", seed) + uuid.uuid4().hex)[:64]
            if len(session_id) < 33:
                session_id = (session_id + uuid.uuid4().hex)[:64]
            agentcore_client.invoke_agent_runtime(
                agentRuntimeArn=WAIVER_AGENT_RUNTIME_ARN,
                runtimeSessionId=session_id,
                payload=json.dumps(payload).encode("utf-8"),
                contentType="application/json",
                accept="application/json",
            )
            return {"success": True, "runtime": "agentcore", "note": "Waiver agent (AgentCore) invoked"}

        # Fallback: invoke the waiver agent Lambda asynchronously.
        response = lambda_client.invoke(
            FunctionName=WAIVER_AGENT_LAMBDA_ARN,
            InvocationType="Event",
            Payload=json.dumps(payload),
        )
        return {
            "success": True,
            "runtime": "lambda",
            "status_code": response["StatusCode"],
            "note": "Waiver agent (Lambda) invoked asynchronously",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
