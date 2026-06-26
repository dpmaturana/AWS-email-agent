"""
End-to-end email flow test against the deployed AWS stack.

Reads .eml files from tests/emails/, sends each one through the ingestion
Lambda, and shows which action the router agent took and what reply was sent.

Usage:
    python tests/test_e2e_emails.py                        # run all emails
    python tests/test_e2e_emails.py rag-mcsbt-calendar     # run one by prefix
    python tests/test_e2e_emails.py routing waiver          # run several

Requires:
    - AWS credentials with access to the deployed stack
    - Set AWS_PROFILE=amiga (or export before running)

Example:
    AWS_PROFILE=amiga python tests/test_e2e_emails.py
"""

import json
import os
import sys
import time
import pathlib
import boto3
from datetime import datetime, timezone

# ── constants ─────────────────────────────────────────────────────────────────

REGION             = "eu-west-1"
INGESTION_LAMBDA   = "InfraStack-IngestionLambdaEF25F265-yjKP1oma9Yi7"
RAW_EMAILS_BUCKET  = "infrastack-rawemailsbucket9b2c7ce4-bszikb2v3fnu"
EMAILS_DIR         = pathlib.Path(__file__).parent / "emails"

# ── colours ───────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def ok(msg):      print(f"  {GREEN}✓{RESET} {msg}")
def info(msg):    print(f"  {BLUE}→{RESET} {msg}")
def warn(msg):    print(f"  {YELLOW}⚠{RESET} {msg}")
def fail(msg):    print(f"  {RED}✗{RESET} {msg}")
def detail(msg):  print(f"  {DIM}{msg}{RESET}")
def header(msg):  print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{msg}{RESET}\n{CYAN}{'─'*60}{RESET}")
def subhead(msg): print(f"\n{BOLD}{msg}{RESET}")


# ── helpers ───────────────────────────────────────────────────────────────────

def _s3_keys_after(client, prefix, since_ts):
    """Return S3 keys under prefix modified after since_ts (epoch float)."""
    resp = client.list_objects_v2(Bucket=RAW_EMAILS_BUCKET, Prefix=prefix)
    found = []
    for obj in resp.get("Contents", []):
        if obj["LastModified"].timestamp() > since_ts:
            found.append(obj["Key"])
    return found


def _detect_action(agent_response: str) -> str:
    """Guess which tool the agent used from its response text."""
    r = agent_response.lower()
    if "knowledge base" in r or "replied" in r or "query" in r:
        return "rag"
    if "waiver" in r:
        return "waiver"
    if "routed" in r or "forwarded" in r or "program management" in r:
        return "route"
    return "unknown"


def _read_s3_json(s3, key):
    obj = s3.get_object(Bucket=RAW_EMAILS_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


# ── main test runner ──────────────────────────────────────────────────────────

def run_email(eml_path: pathlib.Path, lambda_client, s3) -> bool:
    header(f"EMAIL: {eml_path.name}")

    raw_email = eml_path.read_text(encoding="utf-8")

    # Print what we're sending
    subhead("Sending:")
    for line in raw_email.splitlines()[:5]:
        detail(f"  {line}")
    if raw_email.count("\n") > 5:
        detail("  ...")

    # Snapshot S3 state before invocation
    before_ts = time.time() - 1

    # Invoke ingestion Lambda
    info("Invoking ingestion Lambda...")
    t0 = time.time()
    resp = lambda_client.invoke(
        FunctionName=INGESTION_LAMBDA,
        InvocationType="RequestResponse",
        Payload=json.dumps({"raw_email": raw_email}).encode("utf-8"),
    )
    elapsed = time.time() - t0
    result = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        fail(f"Lambda error: {result}")
        return False

    ok(f"Lambda returned in {elapsed:.1f}s")

    # Parse agent response
    agent_response = result.get("agent_response", "")
    action = _detect_action(agent_response)

    action_labels = {"rag": "RAG reply", "route": "Forwarded", "waiver": "Waiver agent", "unknown": "?"}
    action_colors = {"rag": GREEN, "route": BLUE, "waiver": YELLOW, "unknown": DIM}
    color = action_colors[action]
    info(f"Router action: {color}{BOLD}{action_labels[action]}{RESET}")

    # Wait a moment for S3 writes to settle
    time.sleep(2)

    # Check which S3 file was created
    new_responses = _s3_keys_after(s3, "responses/", before_ts)
    new_routed    = _s3_keys_after(s3, "routed/", before_ts)

    if new_responses:
        subhead("Reply sent to student:")
        for key in new_responses:
            mail = _read_s3_json(s3, key)
            detail(f"  From:    {mail.get('from', '—')}")
            detail(f"  To:      {mail.get('to', '—')}")
            detail(f"  Subject: {mail.get('subject', '—')}")
            body_preview = mail.get("body", "").replace("\n", " ")[:200]
            detail(f"  Body:    {body_preview}...")
        ok(f"Response saved → {new_responses[0]}")

    elif new_routed:
        subhead("Forwarded to department:")
        for key in new_routed:
            mail = _read_s3_json(s3, key)
            detail(f"  From:    {mail.get('from', '—')}")
            detail(f"  To:      {mail.get('to', '—')}")
            detail(f"  CC:      {mail.get('cc', '—')}")
            detail(f"  Subject: {mail.get('subject', '—')}")
        ok(f"Record saved → {new_routed[0]}")

    elif action == "waiver":
        ok("Waiver agent invoked asynchronously (check DynamoDB/Step Functions)")

    else:
        warn("No S3 record found — SES may have sent directly or action was async")

    return True


def main():
    profile = os.environ.get("AWS_PROFILE", "amiga")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    lambda_client = session.client("lambda")
    s3 = session.client("s3")

    # Collect .eml files to run
    all_emls = sorted(EMAILS_DIR.glob("*.eml"))
    if not all_emls:
        print(f"{RED}No .eml files found in {EMAILS_DIR}{RESET}")
        sys.exit(1)

    filters = sys.argv[1:]
    if filters:
        selected = [e for e in all_emls if any(f in e.name for f in filters)]
        if not selected:
            print(f"{RED}No .eml files match: {filters}{RESET}")
            print(f"Available: {[e.name for e in all_emls]}")
            sys.exit(1)
    else:
        selected = all_emls

    print(f"\n{BOLD}Email E2E Tests — {len(selected)} file(s){RESET}")
    print(f"{DIM}AWS profile: {profile} | Region: {REGION}{RESET}")

    passed, failed = 0, 0
    for eml in selected:
        try:
            ok_result = run_email(eml, lambda_client, s3)
            if ok_result:
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            fail(f"Unexpected error: {exc}")
            failed += 1

    print(f"\n{BOLD}{'─'*60}{RESET}")
    if failed == 0:
        print(f"{GREEN}{BOLD}All {passed}/{len(selected)} tests passed ✓{RESET}\n")
    else:
        print(f"{RED}{BOLD}{failed} failed, {passed} passed{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()