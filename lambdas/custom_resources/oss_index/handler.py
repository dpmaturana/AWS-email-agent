import json
import time
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

_http = URLLib3Session()

_MAX_ATTEMPTS = 12
_RETRY_DELAY_S = 15  # AOSS data access policies can take ~1-2 min to propagate


def handler(event, context):
    props = event["ResourceProperties"]
    endpoint = props["CollectionEndpoint"].rstrip("/")
    index_name = props["IndexName"]
    region = props["Region"]

    # Diagnostic: log caller identity and deployed data access policy
    sts = boto3.client("sts", region_name=region)
    identity = sts.get_caller_identity()
    print(f"[DIAG] CallerArn={identity['Arn']}")
    print(f"[DIAG] Target={endpoint}/{index_name} Region={region}")

    aoss_mgmt = boto3.client("opensearchserverless", region_name=region)
    try:
        policy = aoss_mgmt.get_access_policy(name="email-agent-data", type="data")
        detail = policy.get("accessPolicyDetail", {})
        print(f"[DIAG] DataAccessPolicy={json.dumps(detail, default=str)}")
    except Exception as e:
        print(f"[DIAG] DataAccessPolicy not found or error: {e}")

    request_type = event["RequestType"]
    if request_type == "Create":
        _create_index(endpoint, index_name, region)
    elif request_type == "Delete":
        _delete_index(endpoint, index_name, region)
    # Update: no-op

    return {"PhysicalResourceId": f"{endpoint}/{index_name}"}


def _signed_request(method, url, region, body=None):
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    body_bytes = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    headers = {"Content-Type": "application/json"} if body_bytes else {}

    aws_req = AWSRequest(method=method, url=url, data=body_bytes, headers=headers)
    SigV4Auth(credentials, "aoss", region).add_auth(aws_req)
    return _http.send(aws_req.prepare())


def _create_index(endpoint, index_name, region):
    body = json.dumps({
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 1024,
                    "method": {
                        "name": "hnsw",
                        "space_type": "l2",
                        "engine": "nmslib",
                        "parameters": {"ef_construction": 512, "m": 16},
                    },
                },
                "text": {"type": "text"},
                "metadata": {"type": "text"},
            }
        },
    })
    url = f"{endpoint}/{index_name}"
    for attempt in range(_MAX_ATTEMPTS):
        resp = _signed_request("PUT", url, region, body)
        print(f"[DIAG] Attempt {attempt + 1}: status={resp.status_code} body={resp.text[:300]}")
        if resp.status_code in (200, 201):
            return
        if resp.status_code == 403 and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_DELAY_S)
            continue
        raise Exception(f"Index creation failed ({resp.status_code}): {resp.text}")


def _delete_index(endpoint, index_name, region):
    try:
        _signed_request("DELETE", f"{endpoint}/{index_name}", region)
    except Exception:
        pass