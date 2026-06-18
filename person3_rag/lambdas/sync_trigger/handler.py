"""
Auto-sync trigger (Person 3).

Fires on an EventBridge "Object Created" event for the documents bucket. For
each new document it:
  1. Derives the department from the S3 key's first path segment.
  2. Writes a `<key>.metadata.json` sidecar tagging the chunk with the
     department, so query-time metadata filtering works.
  3. Starts a Bedrock ingestion job on that department's data source, which
     chunks + embeds + indexes the document into OpenSearch Serverless.

The sidecar write itself produces an "Object Created" event, so we ignore any
key ending in `.metadata.json` to avoid an infinite loop.
"""

import json
import os
import urllib.parse

import boto3
from botocore.exceptions import ClientError

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
DATA_SOURCE_IDS = json.loads(os.environ["DATA_SOURCE_IDS"])  # {"hr": "id", ...}
DOCUMENTS_BUCKET = os.environ["DOCUMENTS_BUCKET"]

METADATA_SUFFIX = ".metadata.json"

_s3 = boto3.client("s3")
_bedrock = boto3.client("bedrock-agent")


def handler(event, _context):
    detail = event.get("detail", {})
    bucket = detail.get("bucket", {}).get("name")
    raw_key = detail.get("object", {}).get("key", "")
    key = urllib.parse.unquote_plus(raw_key)

    if not bucket or not key:
        print(f"Skipping malformed event: {event}")
        return {"skipped": True}

    # Avoid recursion: the sidecar we write also raises an event.
    if key.endswith(METADATA_SUFFIX):
        return {"skipped": "metadata sidecar"}

    department = key.split("/", 1)[0] if "/" in key else None
    if department not in DATA_SOURCE_IDS:
        print(f"Key {key!r} not under a known department prefix; skipping.")
        return {"skipped": f"unknown department for key {key}"}

    # 1. Write the department metadata sidecar next to the object.
    sidecar_key = f"{key}{METADATA_SUFFIX}"
    sidecar_body = json.dumps(
        {"metadataAttributes": {"department": department}}
    ).encode("utf-8")
    _s3.put_object(
        Bucket=bucket,
        Key=sidecar_key,
        Body=sidecar_body,
        ContentType="application/json",
    )
    print(f"Wrote metadata sidecar {sidecar_key}")

    # 2. Start an ingestion job for this department's data source.
    data_source_id = DATA_SOURCE_IDS[department]
    try:
        resp = _bedrock.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=data_source_id,
            description=f"Auto-sync triggered by {key}",
        )
        job_id = resp["ingestionJob"]["ingestionJobId"]
        print(f"Started ingestion job {job_id} for department {department}")
        return {"department": department, "ingestionJobId": job_id}
    except ClientError as exc:
        # A job is already running for this data source. It will index what it
        # finds; the next upload re-triggers. Acceptable for the demo (see the
        # debounce limitation noted in the report).
        if exc.response["Error"]["Code"] == "ConflictException":
            print(f"Ingestion job already running for {department}; will retry on next upload.")
            return {"department": department, "ingestionJobId": None, "conflict": True}
        raise
