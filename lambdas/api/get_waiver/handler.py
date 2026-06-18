"""
GET /waivers/{waiver_id}
Returns full WaiverDetail from DynamoDB.
"""
import json
import os
import boto3

TABLE_NAME = os.environ["WAIVER_TABLE_NAME"]
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
}


def handler(event, _context):
    waiver_id = event["pathParameters"]["waiver_id"]

    response = table.get_item(Key={"waiver_id": waiver_id})
    item = response.get("Item")

    if not item:
        return {
            "statusCode": 404,
            "headers": CORS_HEADERS,
            "body": json.dumps({"message": "Waiver not found"}),
        }

    item.setdefault("collected_info", {})
    item.setdefault("missing_fields", [])
    item.setdefault("history", [])
    item.setdefault("attachments", [])

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps(item, default=str),
    }
