"""
query_knowledge_base tool implementation (Person 3).

Person 2's Strands agent calls a `@tool query_knowledge_base(...)` whose body
invokes THIS Lambda. We use the Bedrock `Retrieve` API (not RetrieveAndGenerate)
because the tool contract returns raw chunks — the agent does the generation
itself. Retrieval is scoped to a single department via a metadata filter so HR
docs never surface in an IT answer.

Event (matches the tool contract):
{
  "query": "how many vacation days do I get",
  "department": "hr",          # hr | legal | it | general
  "top_k": 5                    # optional, default 5
}

Returns:
[ { "content": str, "source": str, "score": float }, ... ]
"""

import os

import boto3

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
VALID_DEPARTMENTS = {"hr", "legal", "it", "general"}

_client = boto3.client("bedrock-agent-runtime")


def handler(event, _context):
    query = (event or {}).get("query")
    department = (event or {}).get("department")
    top_k = int((event or {}).get("top_k", 5))

    if not query or not isinstance(query, str):
        raise ValueError("`query` is required and must be a string")
    if department not in VALID_DEPARTMENTS:
        raise ValueError(
            f"`department` must be one of {sorted(VALID_DEPARTMENTS)}, got {department!r}"
        )

    response = _client.retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                # Metadata filter — only chunks tagged with this department.
                "filter": {"equals": {"key": "department", "value": department}},
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
                "source": source,
                "score": item.get("score", 0.0),
            }
        )
    return results
