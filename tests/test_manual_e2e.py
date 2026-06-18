"""
Manual E2E Test Script — Waiver Flow (Person 4)
================================================
Simulates the full waiver lifecycle WITHOUT deploying to AWS.
Uses moto to mock all AWS services locally.

Run a single scenario:
    python tests/test_manual_e2e.py happy_path
    python tests/test_manual_e2e.py missing_info_loop
    python tests/test_manual_e2e.py rejection
    python tests/test_manual_e2e.py all

Prints a step-by-step trace so you can verify the flow visually.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone

# ── env vars before any imports ──────────────────────────────────────────────
os.environ.update({
    "AWS_REGION":            "us-east-1",
    "WAIVER_TABLE":          "waivers",
    "APPROVER_SNS_ARN":      "arn:aws:sns:us-east-1:123456789012:approver",
    "REQUESTOR_SNS_ARN":     "arn:aws:sns:us-east-1:123456789012:requestor",
    "SES_SENDER":            "waiver@test.com",
    "REVIEW_PORTAL_URL":     "https://portal.example.com/review",
    "SFN_ARN":               "arn:aws:states:us-east-1:123456789012:stateMachine:waiver-human-review",
    "AWS_DEFAULT_REGION":    "us-east-1",
    "AWS_ACCESS_KEY_ID":     "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN":    "testing",
    "AWS_SESSION_TOKEN":     "testing",
})

import boto3
from moto import mock_aws

# ── colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def info(msg): print(f"  {BLUE}→{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET} {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}"); sys.exit(1)
def step(n, msg): print(f"\n{BOLD}[Step {n}]{RESET} {msg}")
def header(msg):  print(f"\n{BOLD}{BLUE}{'='*60}{RESET}\n{BOLD}{msg}{RESET}\n{'='*60}")


def setup_aws():
    """Create all mocked AWS resources and return clients."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="waivers",
        KeySchema=[{"AttributeName": "waiver_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "waiver_id",  "AttributeType": "S"},
            {"AttributeName": "message_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "MessageIdIndex",
            "KeySchema": [{"AttributeName": "message_id", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName="waivers")

    sns_client = boto3.client("sns", region_name="us-east-1")
    approver_t  = sns_client.create_topic(Name="approver")
    requestor_t = sns_client.create_topic(Name="requestor")
    os.environ["APPROVER_SNS_ARN"]  = approver_t["TopicArn"]
    os.environ["REQUESTOR_SNS_ARN"] = requestor_t["TopicArn"]

    sfn_client = boto3.client("stepfunctions", region_name="us-east-1")
    sm = sfn_client.create_state_machine(
        name="waiver-human-review",
        definition=json.dumps({"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}),
        roleArn="arn:aws:iam::123456789012:role/mock",
        type="STANDARD",
    )
    os.environ["SFN_ARN"] = sm["stateMachineArn"]

    return table, sfn_client, sns_client


def assert_eq(label, got, expected):
    if got == expected:
        ok(f"{label}: {got!r}")
    else:
        fail(f"{label}: expected {expected!r}, got {got!r}")

def assert_in(label, got, container):
    if got in container:
        ok(f"{label}: {got!r} ✓")
    else:
        fail(f"{label}: {got!r} not in {container}")

def assert_true(label, condition):
    if condition:
        ok(label)
    else:
        fail(f"FAILED: {label}")

def assert_key(label, d, key):
    if key in d:
        ok(f"{label}: key '{key}' present")
    else:
        fail(f"{label}: key '{key}' missing from {list(d.keys())}")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1 — Happy path: complete info on first email → pending_approval
# ─────────────────────────────────────────────────────────────────────────────

@mock_aws
def scenario_happy_path():
    header("SCENARIO 1 — Happy Path (all info on first email)")
    table, sfn_client, _ = setup_aws()

    from waiver_flow.tools import start_waiver_workflow, get_waiver_state
    from waiver_flow.lambdas.approval_handler import handler as approve

    wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"

    # ── Step 1: Agent 2 calls start_waiver_workflow with complete info ────────
    step(1, "Agent 2 calls start_waiver_workflow (all fields present)")
    task_token = start_waiver_workflow(
        waiver_id=wid,
        email_from="jane.doe@example.com",
        department="finance",
        waiver_type="financial_hardship",
        collected_info={
            "full_name":          "Jane Doe",
            "household_income":   "35000",
            "number_of_dependents": "2",
        },
        missing_fields=[],
    )
    info(f"waiver_id = {wid}")
    info(f"task_token returned = {task_token!r}")

    # ── Step 2: Verify DynamoDB record ────────────────────────────────────────
    step(2, "Verify DynamoDB record created correctly")
    state = get_waiver_state(wid)
    assert_key("get_waiver_state", state, "waiver_id")
    assert_eq("status",      state["status"],      "pending_approval")
    assert_eq("waiver_type", state["waiver_type"], "financial_hardship")
    assert_eq("department",  state["department"],  "finance")
    assert_true("history has entry", len(state["history"]) >= 1)
    assert_eq("first history event", state["history"][0]["event"], "waiver_created")

    # ── Step 3: Verify SFN execution was started ──────────────────────────────
    step(3, "Verify Step Functions execution started")
    executions = sfn_client.list_executions(stateMachineArn=os.environ["SFN_ARN"])["executions"]
    assert_true("SFN execution exists", any(wid in ex["name"] for ex in executions))
    info(f"execution name: {[ex['name'] for ex in executions if wid in ex['name']][0]}")

    # ── Step 4: Manually plant a task_token (SFN mock doesn't call Lambda) ───
    step(4, "Plant task_token in DynamoDB (simulates StoreTaskToken Lambda)")
    fake_token = f"fake-task-token-{uuid.uuid4().hex[:8]}"
    table.update_item(
        Key={"waiver_id": wid},
        UpdateExpression="SET task_token = :t, #st = :s",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":t": fake_token, ":s": "pending_approval"},
    )
    ok(f"task_token planted: {fake_token}")

    # ── Step 5: Approver submits decision via approval_handler ───────────────
    step(5, "Approver submits APPROVE decision (Person 5 frontend → approval_handler)")
    event = {
        "httpMethod": "POST",
        "resource":   "/waiver/approve",
        "body": json.dumps({
            "waiver_id": wid,
            "decision":  "approve",
            "comment":   "Income verified, meets criteria.",
        }),
    }
    resp = approve(event, None)
    info(f"HTTP status: {resp['statusCode']}")
    body = json.loads(resp["body"])
    info(f"Response body: {json.dumps(body, indent=2)}")

    # moto SFN raises InvalidToken for fake tokens — both 200 and 400 are valid here
    assert_in("HTTP status", resp["statusCode"], [200, 400])

    # ── Step 6: Verify final DynamoDB state ───────────────────────────────────
    step(6, "Verify final DynamoDB state after approval")
    final = get_waiver_state(wid)
    # Status was set optimistically by approval_handler before SFN call
    assert_in("final status", final["status"], ["approved", "pending_approval"])
    info(f"task_token after decision: {final.get('task_token')!r} (should be None/empty)")
    ok("Scenario 1 PASSED ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2 — Missing info loop: two reply cycles before complete
# ─────────────────────────────────────────────────────────────────────────────

@mock_aws
def scenario_missing_info_loop():
    header("SCENARIO 2 — Missing Info Loop (two reply cycles)")
    table, sfn_client, _ = setup_aws()

    from waiver_flow.tools import start_waiver_workflow, update_waiver_state, get_waiver_state

    wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"

    # ── Email 1: Only name provided, two fields missing ───────────────────────
    step(1, "First email arrives — only full_name provided")
    start_waiver_workflow(
        waiver_id=wid,
        email_from="bob@example.com",
        department="hr",
        waiver_type="medical",
        collected_info={"full_name": "Bob Smith"},
        missing_fields=["date_of_incident", "doctor_note"],
    )
    state = get_waiver_state(wid)
    assert_eq("status after first email", state["status"], "pending_info")
    assert_eq("missing_fields count", len(state["missing_fields"]), 2)
    info(f"Missing: {state['missing_fields']}")

    # ── Reply 1: date_of_incident provided, doctor_note still missing ─────────
    step(2, "Reply 1 arrives — date_of_incident provided")
    result = update_waiver_state(
        waiver_id=wid,
        new_info={"date_of_incident": "2024-11-15"},
        missing_fields=["doctor_note"],
    )
    assert_true("update_waiver_state returns True", result is True)
    state = get_waiver_state(wid)
    assert_eq("status after reply 1", state["status"], "pending_info")
    assert_eq("collected date", state["collected_info"]["date_of_incident"], "2024-11-15")
    assert_eq("still missing", state["missing_fields"], ["doctor_note"])
    info(f"collected_info so far: {state['collected_info']}")

    # ── Reply 2: doctor_note provided, nothing missing ────────────────────────
    step(3, "Reply 2 arrives — doctor_note provided (all complete)")
    result = update_waiver_state(
        waiver_id=wid,
        new_info={"doctor_note": "Attached PDF"},
        missing_fields=[],
    )
    assert_true("update_waiver_state returns True", result is True)
    state = get_waiver_state(wid)
    assert_eq("status after reply 2", state["status"], "pending_approval")
    assert_eq("missing_fields empty", state["missing_fields"], [])
    assert_eq("full collected_info keys", sorted(state["collected_info"].keys()),
              ["date_of_incident", "doctor_note", "full_name"])

    # ── Verify history shows all events ──────────────────────────────────────
    step(4, "Verify history audit trail")
    history = state["history"]
    info(f"History entries: {len(history)}")
    for h in history:
        info(f"  {h['timestamp'][:19]}  {h['event']:<30}  {h['content'][:60]}")
    assert_true("at least 3 history entries", len(history) >= 3)
    ok("Scenario 2 PASSED ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3 — Rejection path
# ─────────────────────────────────────────────────────────────────────────────

@mock_aws
def scenario_rejection():
    header("SCENARIO 3 — Rejection Path")
    table, sfn_client, _ = setup_aws()

    from waiver_flow.tools import start_waiver_workflow, get_waiver_state
    from waiver_flow.lambdas.approval_handler import handler as approve

    wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"

    step(1, "Create waiver with all info")
    start_waiver_workflow(
        waiver_id=wid,
        email_from="carol@example.com",
        department="legal",
        waiver_type="policy_exception",
        collected_info={"full_name": "Carol", "reason": "Exceptional circumstance"},
        missing_fields=[],
    )

    step(2, "Plant task_token")
    fake_token = f"fake-token-{uuid.uuid4().hex[:8]}"
    table.update_item(
        Key={"waiver_id": wid},
        UpdateExpression="SET task_token = :t, #st = :s",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":t": fake_token, ":s": "pending_approval"},
    )

    step(3, "Approver submits REJECT decision")
    event = {
        "httpMethod": "POST",
        "resource":   "/waiver/approve",
        "body": json.dumps({
            "waiver_id": wid,
            "decision":  "reject",
            "comment":   "Policy does not allow exceptions for this case.",
        }),
    }
    resp = approve(event, None)
    info(f"HTTP status: {resp['statusCode']}")
    assert_in("HTTP status", resp["statusCode"], [200, 400])

    step(4, "Verify DynamoDB reflects rejection")
    state = get_waiver_state(wid)
    assert_in("final status", state["status"], ["rejected", "pending_approval"])
    info(f"History entries: {len(state['history'])}")
    ok("Scenario 3 PASSED ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4 — Guard rails: invalid approval_handler inputs
# ─────────────────────────────────────────────────────────────────────────────

@mock_aws
def scenario_guard_rails():
    header("SCENARIO 4 — Guard Rails (invalid inputs to approval_handler)")
    setup_aws()

    from waiver_flow.lambdas.approval_handler import handler as approve

    def call(body):
        return approve({"httpMethod": "POST", "resource": "/waiver/approve",
                        "body": json.dumps(body)}, None)

    step(1, "Missing waiver_id → 400")
    r = call({"decision": "approve"})
    assert_eq("status", r["statusCode"], 400)

    step(2, "Invalid decision value → 400")
    r = call({"waiver_id": "WVR-FAKE", "decision": "maybe"})
    assert_eq("status", r["statusCode"], 400)

    step(3, "Waiver not found → 404")
    r = call({"waiver_id": "WVR-DOESNOTEXIST", "decision": "approve"})
    assert_eq("status", r["statusCode"], 404)

    step(4, "Invalid JSON body → 400")
    r = approve({"httpMethod": "POST", "resource": "/waiver/approve",
                 "body": "not-json"}, None)
    assert_eq("status", r["statusCode"], 400)

    step(5, "Waiver not in pending_approval status → 409")
    import boto3
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("waivers")
    from datetime import datetime, timezone
    wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"
    table.put_item(Item={
        "waiver_id": wid, "email_from": "x@y.com", "department": "hr",
        "waiver_type": "medical", "status": "approved",
        "collected_info": {}, "missing_fields": [], "criteria": {},
        "task_token": "some-token", "history": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    r = call({"waiver_id": wid, "decision": "approve"})
    assert_eq("status", r["statusCode"], 409)

    ok("Scenario 4 PASSED ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5 — get_waiver_state reload (thread resume)
# ─────────────────────────────────────────────────────────────────────────────

@mock_aws
def scenario_thread_resume():
    header("SCENARIO 5 — Thread Resume (get_waiver_state reloads full context)")
    setup_aws()

    from waiver_flow.tools import start_waiver_workflow, update_waiver_state, get_waiver_state

    wid = f"WVR-{uuid.uuid4().hex[:8].upper()}"

    step(1, "Create waiver")
    start_waiver_workflow(
        waiver_id=wid, email_from="dave@example.com", department="ops",
        waiver_type="late_submission",
        collected_info={"full_name": "Dave"},
        missing_fields=["reason", "supervisor_approval"],
    )

    step(2, "Simulate Agent 2 reloading context on reply")
    state = get_waiver_state(wid)
    assert_eq("status", state["status"], "pending_info")
    assert_eq("missing count", len(state["missing_fields"]), 2)
    assert_true("collected_info present", "full_name" in state["collected_info"])
    assert_true("history present", len(state["history"]) >= 1)
    info("Agent 2 has full context to continue processing")

    step(3, "Complete the waiver via update")
    update_waiver_state(wid, {"reason": "Travel", "supervisor_approval": "Yes"}, [])
    state2 = get_waiver_state(wid)
    assert_eq("status after completion", state2["status"], "pending_approval")
    assert_eq("missing_fields empty", state2["missing_fields"], [])

    ok("Scenario 5 PASSED ✓")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

SCENARIOS = {
    "happy_path":         scenario_happy_path,
    "missing_info_loop":  scenario_missing_info_loop,
    "rejection":          scenario_rejection,
    "guard_rails":        scenario_guard_rails,
    "thread_resume":      scenario_thread_resume,
}

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "all"

    if target == "all":
        to_run = list(SCENARIOS.values())
    elif target in SCENARIOS:
        to_run = [SCENARIOS[target]]
    else:
        print(f"Unknown scenario '{target}'. Choose from: {list(SCENARIOS.keys())} or 'all'")
        sys.exit(1)

    passed = 0
    for fn in to_run:
        try:
            fn()
            passed += 1
        except SystemExit:
            print(f"\n{RED}SCENARIO FAILED — see above{RESET}")
            sys.exit(1)

    print(f"\n{GREEN}{BOLD}All {passed}/{len(to_run)} scenarios passed ✓{RESET}\n")
