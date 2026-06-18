"""
GET /waivers
Query params: status, department, page (default 1), limit (default 20)
Reads from DynamoDB WaiverTable owned by Person 4.
"""
import json
import os
import boto3
from boto3.dynamodb.conditions import Attr

TABLE_NAME = os.environ["WAIVER_TABLE_NAME"]
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}

SUMMARY_FIELDS = {
    "waiver_id", "email_from", "department", "waiver_type",
    "status", "created_at", "updated_at",
}


def handler(event, _context):
    params = event.get("queryStringParameters") or {}
    status_filter = params.get("status")
    dept_filter = params.get("department")
    page = max(1, int(params.get("page", 1)))
    limit = min(100, max(1, int(params.get("limit", 20))))

    filter_expr = None
    if status_filter:
        filter_expr = Attr("status").eq(status_filter)
    if dept_filter:
        dept_cond = Attr("department").eq(dept_filter)
        filter_expr = filter_expr & dept_cond if filter_expr else dept_cond

    kwargs = {"ProjectionExpression": ", ".join(SUMMARY_FIELDS)}
    if filter_expr:
        kwargs["FilterExpression"] = filter_expr

    items = []
    response = table.scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
        items.extend(response.get("Items", []))

    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    total = len(items)
    start = (page - 1) * limit

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "items": items[start: start + limit],
            "total": total,
            "page": page,
            "limit": limit,
        }),
    }
