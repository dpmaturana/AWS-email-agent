import json
import os
import uuid
import boto3
from strands import tool

ses = boto3.client("ses", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
lambda_client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-west-1"))

EMAIL_FROM = os.environ["EMAIL_FROM"]
WAIVER_CRITERIA_BUCKET = os.environ["WAIVER_CRITERIA_BUCKET"]

# These Lambda ARNs are implemented by Person 4
START_WAIVER_LAMBDA_ARN = os.environ["START_WAIVER_LAMBDA_ARN"]
UPDATE_WAIVER_LAMBDA_ARN = os.environ["UPDATE_WAIVER_LAMBDA_ARN"]
GET_WAIVER_LAMBDA_ARN = os.environ["GET_WAIVER_LAMBDA_ARN"]


@tool
def get_waiver_criteria(waiver_type: str, department: str) -> dict:
    """
    Fetches the criteria required for a specific waiver type from S3.
    Returns the required fields, documents, and approval conditions.

    Args:
        waiver_type: The type of waiver (e.g. attendance_waiver, late_fee_exception, grade_appeal)
        department: The department context (program_management|student_services|registrar|administration|campus_life|venture_lab|job_market)

    Returns:
        dict with required_fields, required_documents, approval_conditions
    """
    try:
        key = f"{department}/{waiver_type}.json"
        response = s3.get_object(Bucket=WAIVER_CRITERIA_BUCKET, Key=key)
        criteria = json.loads(response["Body"].read())
        return criteria
    except s3.exceptions.NoSuchKey:
        # Fallback: return generic criteria if specific type not found
        return {
            "required_fields": ["student_name", "student_id", "reason", "supporting_context"],
            "required_documents": [],
            "approval_conditions": ["Reviewed and approved by department coordinator"],
            "note": f"No specific criteria found for {waiver_type} in {department}. Using generic criteria."
        }
    except Exception as e:
        return {"error": str(e)}


@tool
def check_completeness(
    collected_info: dict,
    attachments: list,
    criteria: dict
) -> dict:
    """
    Checks whether all required information and documents are present
    to proceed with the waiver request.

    Args:
        collected_info: Dict of field name to value, collected so far from the student
        attachments: List of attachment metadata dicts received so far
        criteria: The waiver criteria returned by get_waiver_criteria

    Returns:
        dict with is_complete (bool), missing_fields (list), missing_documents (list)
    """
    missing_fields = []
    missing_documents = []

    required_fields = criteria.get("required_fields", [])
    required_documents = criteria.get("required_documents", [])

    for field in required_fields:
        if field not in collected_info or not collected_info[field]:
            missing_fields.append(field)

    attachment_names = [a.get("filename", "").lower() for a in attachments]
    for doc in required_documents:
        found = any(doc.lower() in name for name in attachment_names)
        if not found:
            missing_documents.append(doc)

    is_complete = len(missing_fields) == 0 and len(missing_documents) == 0

    return {
        "is_complete": is_complete,
        "missing_fields": missing_fields,
        "missing_documents": missing_documents,
    }


@tool
def request_missing_info(
    to: str,
    waiver_id: str,
    waiver_type: str,
    missing_fields: list,
    missing_documents: list
) -> dict:
    """
    Sends an email to the student requesting the specific missing information
    or documents needed to process their waiver request.

    Args:
        to: Student email address
        waiver_id: The waiver ID for tracking
        waiver_type: The type of waiver being processed
        missing_fields: List of missing information fields
        missing_documents: List of missing documents

    Returns:
        dict with success status
    """
    missing_items = []

    if missing_fields:
        missing_items.append("Please provide the following information:")
        for field in missing_fields:
            readable = field.replace("_", " ").capitalize()
            missing_items.append(f"  - {readable}")

    if missing_documents:
        missing_items.append("\nPlease attach the following documents:")
        for doc in missing_documents:
            missing_items.append(f"  - {doc}")

    body = (
        f"Dear student,\n\n"
        f"Thank you for submitting your {waiver_type.replace('_', ' ')} request (Reference: {waiver_id}).\n\n"
        f"In order to process your request, we need the following additional information:\n\n"
        f"{chr(10).join(missing_items)}\n\n"
        f"Please reply to this email with the requested information and we will continue processing your request.\n\n"
        f"Best regards,\n"
        f"IE University Student Services"
    )

    try:
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": f"Additional information required — {waiver_type.replace('_', ' ').title()} (Ref: {waiver_id})"},
                "Body": {"Text": {"Data": body}},
            },
        )
        return {"success": True, "sent_to": to, "waiver_id": waiver_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool
def start_waiver_workflow(
    email_from: str,
    department: str,
    waiver_type: str,
    collected_info: dict,
    missing_fields: list,
    message_id: str
) -> dict:
    """
    Creates the waiver record and starts the Step Functions approval workflow.
    Call this when a new waiver request is received for the first time.
    Implemented by Person 4.

    Args:
        email_from: Student email address
        department: Department context
        waiver_type: Type of waiver
        collected_info: Information collected so far
        missing_fields: Fields still missing
        message_id: Original email message ID

    Returns:
        dict with waiver_id and task_token
    """
    try:
        waiver_id = str(uuid.uuid4())
        response = lambda_client.invoke(
            FunctionName=START_WAIVER_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "waiver_id": waiver_id,
                "email_from": email_from,
                "department": department,
                "waiver_type": waiver_type,
                "collected_info": collected_info,
                "missing_fields": missing_fields,
                "message_id": message_id,
            }),
        )
        result = json.loads(response["Payload"].read())
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool
def get_waiver_state(waiver_id: str) -> dict:
    """
    Retrieves the full current state of an existing waiver from DynamoDB.
    Call this at the start of every reply email to reload the waiver context.
    Implemented by Person 4.

    Args:
        waiver_id: The waiver ID to retrieve

    Returns:
        dict with waiver_id, status, collected_info, missing_fields, history
    """
    try:
        response = lambda_client.invoke(
            FunctionName=GET_WAIVER_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({"waiver_id": waiver_id}),
        )
        return json.loads(response["Payload"].read())
    except Exception as e:
        return {"error": str(e)}


@tool
def update_waiver_state(
    waiver_id: str,
    new_info: dict,
    missing_fields: list
) -> dict:
    """
    Updates the waiver record in DynamoDB with newly collected information
    from the student's latest reply email.
    Implemented by Person 4.

    Args:
        waiver_id: The waiver ID to update
        new_info: New fields collected from the latest email
        missing_fields: Updated list of what is still missing

    Returns:
        dict with success status
    """
    try:
        response = lambda_client.invoke(
            FunctionName=UPDATE_WAIVER_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps({
                "waiver_id": waiver_id,
                "new_info": new_info,
                "missing_fields": missing_fields,
            }),
        )
        return json.loads(response["Payload"].read())
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool
def notify_decision(
    to: str,
    waiver_id: str,
    waiver_type: str,
    decision: str,
    comment: str
) -> dict:
    """
    Sends the final waiver decision (approved or rejected) to the student via email.
    Call this after the human approver has made their decision.

    Args:
        to: Student email address
        waiver_id: The waiver ID for reference
        waiver_type: The type of waiver
        decision: Either "approve" or "reject"
        comment: Optional comment from the approver

    Returns:
        dict with success status
    """
    if decision == "approve":
        subject = f"Your {waiver_type.replace('_', ' ').title()} request has been approved (Ref: {waiver_id})"
        body = (
            f"Dear student,\n\n"
            f"We are pleased to inform you that your {waiver_type.replace('_', ' ')} request "
            f"(Reference: {waiver_id}) has been approved.\n\n"
        )
        if comment:
            body += f"Note from the reviewer: {comment}\n\n"
        body += (
            f"If you have any questions, please don't hesitate to contact us.\n\n"
            f"Best regards,\n"
            f"IE University Student Services"
        )
    else:
        subject = f"Your {waiver_type.replace('_', ' ').title()} request — decision (Ref: {waiver_id})"
        body = (
            f"Dear student,\n\n"
            f"After careful review, we regret to inform you that your {waiver_type.replace('_', ' ')} request "
            f"(Reference: {waiver_id}) has not been approved at this time.\n\n"
        )
        if comment:
            body += f"Reason: {comment}\n\n"
        body += (
            f"If you believe this decision was made in error or would like to discuss further options, "
            f"please reply to this email.\n\n"
            f"Best regards,\n"
            f"IE University Student Services"
        )

    try:
        ses.send_email(
            Source=EMAIL_FROM,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        return {"success": True, "sent_to": to, "decision": decision}
    except Exception as e:
        return {"success": False, "error": str(e)}
