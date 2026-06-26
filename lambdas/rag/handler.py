"""
query_knowledge_base tool implementation.

Retrieves chunks from the Bedrock Knowledge Base scoped by program and
optionally topic, so content from different programs/departments never
bleeds into unrelated answers.

Event:
{
  "query":   "what are the capstone deadlines",
  "program": "MCSBT",           # top-level folder (required)
  "topic":   "capstone_project", # sub-folder (optional — omit to search all topics)
  "top_k":   5                   # optional, default 5
}

Returns:
[ { "content": str, "source": str, "score": float }, ... ]
"""

import os

import boto3

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]

_client = boto3.client("bedrock-agent-runtime")


def handler(event, _context):
    query   = (event or {}).get("query")
    program = (event or {}).get("program")
    topic   = (event or {}).get("topic") or None
    top_k   = int((event or {}).get("top_k", 5))

    if not query or not isinstance(query, str):
        raise ValueError("`query` is required and must be a string")
    if not program or not isinstance(program, str):
        raise ValueError("`program` is required and must be a string")

    if topic:
        metadata_filter = {
            "andAll": [
                {"equals": {"key": "program", "value": program}},
                {"equals": {"key": "topic",   "value": topic}},
            ]
        }
    else:
        metadata_filter = {"equals": {"key": "program", "value": program}}

    response = _client.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "filter": metadata_filter,
            }
        },
    )

    results = []
    for item in response.get("retrievalResults", []):
        location = item.get("location", {})
        source = (
            location.get("s3Location", {}).get("uri")
            or location.get("type")
            or "unknown"
        )
        results.append(
            {
                "content": item.get("content", {}).get("text", ""),
                "source":  source,
                "score":   item.get("score", 0.0),
            }
        )
    return results