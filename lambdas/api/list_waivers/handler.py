"""GET /waivers — list waivers (Person 5 API).

Query params: status, department, page (default 1), limit (default 20).
Returns: { items: [WaiverSummary], total, page, limit }
"""
import json
import os

import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ["WAIVER_TABLE_NAME"]

SUMMARY_FIELDS = [
    "waiver_id", "email_from", "department",
    "waiver_type", "status", "created_at", "updated_at",
]

_CORS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body, default=str)}


def handler(event, context):
    qs = event.get("queryStringParameters") or {}
    status = qs.get("status")
    department = qs.get("department")
    try:
        page = max(1, int(qs.get("page") or 1))
        limit = max(1, int(qs.get("limit") or 20))
    except (TypeError, ValueError):
        return _resp(400, {"error": "page and limit must be integers"})

    scan_kwargs = {}
    filt = None
    if status:
        filt = Attr("status").eq(status)
    if department:
        dept_filt = Attr("department").eq(department)
        filt = filt & dept_filt if filt is not None else dept_filt
    if filt is not None:
        scan_kwargs["FilterExpression"] = filt

    table = dynamodb.Table(TABLE_NAME)
    items = []
    resp = table.scan(**scan_kwargs)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **scan_kwargs)
        items.extend(resp.get("Items", []))

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    total = len(items)
    start = (page - 1) * limit
    page_items = items[start:start + limit]
    summaries = [{k: it.get(k, "") for k in SUMMARY_FIELDS} for it in page_items]

    return _resp(200, {"items": summaries, "total": total, "page": page, "limit": limit})
