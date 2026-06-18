"""
Strands `@tool` wrapper for `query_knowledge_base` (Person 3 -> Person 2).

This is the exact interface Person 2's Router/Waiver agents import. It is a thin
client that invokes Person 3's `query_kb` Lambda (ARN exported from RagStack as
`RagQueryToolLambdaArn`). Keeping retrieval behind a Lambda means the agent needs
no Bedrock data-plane permissions of its own and the contract stays stable.

Set the env var QUERY_KB_LAMBDA_ARN (or _NAME) on the agent runtime to the
exported ARN. During development Person 2 can mock this module instead.
"""

import json
import os

import boto3

from strands import tool  # provided in Person 2's agent runtime

_lambda = boto3.client("lambda")
_TARGET = os.environ.get("QUERY_KB_LAMBDA_ARN") or os.environ.get("QUERY_KB_LAMBDA_NAME")


@tool
def query_knowledge_base(query: str, department: str, top_k: int = 5) -> list[dict]:
    """
    Queries the Bedrock Knowledge Base filtered by department.
    Returns the top_k most relevant chunks.
    Each chunk: { "content": str, "source": str, "score": float }

    Args:
        query: natural-language question to retrieve context for.
        department: one of "hr" | "legal" | "it" | "general".
        top_k: number of chunks to return (default 5).
    """
    if not _TARGET:
        raise RuntimeError(
            "Set QUERY_KB_LAMBDA_ARN to the RagStack-exported query Lambda ARN."
        )
    payload = {"query": query, "department": department, "top_k": top_k}
    resp = _lambda.invoke(
        FunctionName=_TARGET,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"query_knowledge_base Lambda error: {body}")
    return body
