"""
Vector index setup custom resource (Person 3).

A Bedrock Knowledge Base on OpenSearch Serverless requires the vector index to
already exist with the right kNN mapping — Bedrock does not create it. This
custom resource (driven by CDK's Provider framework) PUTs the index on Create
and deletes it on Delete.

We sign requests with SigV4 for the `aoss` service using botocore (already in
the Lambda runtime) and send them with urllib, so there are NO external
dependencies to bundle — keeps the asset Docker-free and the deploy hermetic.
"""

import json
import os
import time
import urllib.request
import urllib.error

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ["AWS_REGION"]
ENDPOINT = os.environ["COLLECTION_ENDPOINT"].rstrip("/")
INDEX_NAME = os.environ["INDEX_NAME"]
VECTOR_FIELD = os.environ["VECTOR_FIELD"]
TEXT_FIELD = os.environ["TEXT_FIELD"]
METADATA_FIELD = os.environ["METADATA_FIELD"]
VECTOR_DIMENSION = int(os.environ["VECTOR_DIMENSION"])

_session = botocore.session.Session()


def _index_body() -> dict:
    return {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                VECTOR_FIELD: {
                    "type": "knn_vector",
                    "dimension": VECTOR_DIMENSION,
                    "method": {
                        "name": "hnsw",
                        "engine": "faiss",
                        "space_type": "l2",
                        "parameters": {"ef_construction": 512, "m": 16},
                    },
                },
                TEXT_FIELD: {"type": "text"},
                METADATA_FIELD: {"type": "text", "index": False},
            }
        },
    }


def _signed_request(method: str, path: str, body: dict | None):
    url = f"{ENDPOINT}/{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = AWSRequest(
        method=method,
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    creds = _session.get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "aoss", REGION).add_auth(req)

    urllib_req = urllib.request.Request(
        url, data=data, method=method, headers=dict(req.headers)
    )
    with urllib.request.urlopen(urllib_req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8")


def _create_index():
    last_err = None
    # AOSS can take a moment to accept data-plane calls after the collection
    # reports ACTIVE; retry with backoff.
    for attempt in range(1, 11):
        try:
            status, payload = _signed_request("PUT", INDEX_NAME, _index_body())
            print(f"Index create response {status}: {payload}")
            return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            # Already exists -> treat as success (idempotent Create/Update).
            if exc.code == 400 and "resource_already_exists_exception" in detail:
                print("Index already exists; nothing to do.")
                return
            last_err = f"HTTP {exc.code}: {detail}"
        except urllib.error.URLError as exc:
            last_err = str(exc)
        print(f"Attempt {attempt} failed ({last_err}); retrying...")
        time.sleep(min(5 * attempt, 30))
    raise RuntimeError(f"Failed to create index after retries: {last_err}")


def _delete_index():
    try:
        status, payload = _signed_request("DELETE", INDEX_NAME, None)
        print(f"Index delete response {status}: {payload}")
    except urllib.error.HTTPError as exc:
        # Already gone -> fine.
        print(f"Delete returned HTTP {exc.code} (ignored): {exc.read().decode('utf-8','ignore')}")


def on_event(event, _context):
    request_type = event.get("RequestType")
    print(f"Custom resource event: {request_type}")

    physical_id = f"aoss-index-{INDEX_NAME}"
    if request_type in ("Create", "Update"):
        _create_index()
    elif request_type == "Delete":
        _delete_index()

    return {"PhysicalResourceId": physical_id}
