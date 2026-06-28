"""GET /waivers/{waiver_id} — full waiver detail (Person 5 API).

Returns WaiverDetail: summary + collected_info, missing_fields, history,
and attachments with presigned S3 download URLs.
"""
import json
import os

import boto3

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
TABLE_NAME = os.environ["WAIVER_TABLE_NAME"]
RAW_EMAILS_BUCKET = os.environ.get("RAW_EMAILS_BUCKET", "")

_CORS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body, default=str)}


def _presign(att):
    key = att.get("s3_key")
    bucket = att.get("bucket") or RAW_EMAILS_BUCKET
    url = ""
    if key and bucket:
        try:
            url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600
            )
        except Exception:
            url = ""
    return {"filename": att.get("filename", ""), "s3_presigned_url": url}


def handler(event, context):
    waiver_id = (event.get("pathParameters") or {}).get("waiver_id")
    if not waiver_id:
        return _resp(400, {"error": "waiver_id is required"})

    item = dynamodb.Table(TABLE_NAME).get_item(Key={"waiver_id": waiver_id}).get("Item")
    if not item:
        return _resp(404, {"error": f"Waiver '{waiver_id}' not found"})

    detail = {
        "waiver_id":      item.get("waiver_id"),
        "email_from":     item.get("email_from", ""),
        "department":     item.get("department", ""),
        "waiver_type":    item.get("waiver_type", ""),
        "status":         item.get("status", ""),
        "created_at":     item.get("created_at", ""),
        "updated_at":     item.get("updated_at", ""),
        "collected_info": item.get("collected_info", {}) or {},
        "missing_fields": item.get("missing_fields", []) or [],
        "history":        item.get("history", []) or [],
        "attachments":    [_presign(a) for a in (item.get("attachments", []) or [])],
    }
    return _resp(200, detail)
