# Interface Contracts

All cross-person interfaces. Do not change without team consensus.

---

## Contract 1 — Person 1 → Person 2
**Ingestion Lambda → AgentCore payload**

```json
{
  "message_id": "string",
  "thread_id": "string | null",
  "in_reply_to": "string | null",
  "timestamp": "ISO8601 string",
  "from": "string",
  "to": "string",
  "subject": "string",
  "body_text": "string",
  "attachments": [
    { "filename": "string", "s3_key": "string", "content_type": "string" }
  ],
  "is_new_thread": "boolean"
}
```

`thread_id` is the `waiver_id` if this is a reply to an existing thread, null otherwise.

---

## Contract 2 — Person 2 ↔ Person 3
**query_knowledge_base tool**

```python
@tool
def query_knowledge_base(
    query: str,
    department: str,  # "hr" | "legal" | "it" | "general"
    top_k: int = 5
) -> list[dict]:
    # Returns: [{ "content": str, "source": str, "score": float }]
```

Person 3 owns the implementation in `lambdas/rag/handler.py`.
Person 2 calls it as a tool from Agent 1 and Agent 2.

---

## Contract 3 — Person 2 ↔ Person 4
**Waiver state tools**

```python
@tool
def start_waiver_workflow(
    waiver_id: str,
    email_from: str,
    department: str,
    waiver_type: str,
    collected_info: dict,
    missing_fields: list[str]
) -> str:
    # Returns task_token from Step Functions

@tool
def update_waiver_state(
    waiver_id: str,
    new_info: dict,
    missing_fields: list[str]
) -> bool:
    # Returns True on success

@tool
def get_waiver_state(
    waiver_id: str
) -> dict:
    # Returns full DynamoDB item
    # { waiver_id, status, collected_info, missing_fields, history: [] }
```

Person 4 owns all three implementations.
Person 2 calls them as tools from Agent 2.

---

## Contract 4 — Person 4 ↔ Person 5
**REST API**

```
GET  /waivers
     Query: status?, department?, page?, limit?
     Returns: { items: [WaiverSummary], total, page, limit }

GET  /waivers/{waiver_id}
     Returns: WaiverDetail

POST /waivers/{waiver_id}/decide
     Body: { decision: "approve"|"reject", comment: string }
     Returns: { success: boolean }
```

```typescript
type WaiverSummary = {
  waiver_id: string
  email_from: string
  department: string
  waiver_type: string
  status: "pending_info" | "pending_approval" | "approved" | "rejected"
  created_at: string
  updated_at: string
}

type WaiverDetail = WaiverSummary & {
  collected_info: Record<string, any>
  missing_fields: string[]
  history: Array<{ timestamp: string; event: string; content: string }>
  attachments: Array<{ filename: string; s3_presigned_url: string }>
}
```

Person 4 owns the DynamoDB schema and the approval Lambda.
Person 5 owns the API Gateway + Lambda implementations that read from DynamoDB.
