# Person 3 — RAG + Knowledge Base (`RagStack`)

This module owns the retrieval-augmented-generation subsystem of the email
routing agent. When the Router agent (Person 2) classifies an email as `rag`, it
calls `query_knowledge_base(...)`, which retrieves the most relevant internal
documentation so the agent can draft a grounded reply.

Everything here is deployed by a single account-agnostic CDK stack — no manual
console steps.

---

## 1. What this builds

| Component | Service | Purpose |
|---|---|---|
| Vector store | OpenSearch Serverless (`CfnCollection`, `VECTORSEARCH`) | Stores chunk embeddings + metadata |
| Vector index | Custom resource (SigV4-signed HTTP) | Creates the kNN index Bedrock requires |
| Knowledge Base | Bedrock Knowledge Bases (`CfnKnowledgeBase`) | Orchestrates chunk → embed → index → retrieve |
| Embeddings | Titan Embeddings v2 (1024-dim) | Turns text into vectors |
| Data sources (×4) | `CfnDataSource`, one per dept prefix | Scopes ingestion to `hr/`, `legal/`, `it/`, `general/` |
| Retrieval tool | `query_kb` Lambda (`bedrock:Retrieve`) | The tool Person 2's agent calls |
| Auto-sync | EventBridge rule + `sync_trigger` Lambda | New doc → metadata sidecar → ingestion job |

## 2. Architecture & execution flow

**Ingestion (write path)**
```
Doc uploaded to documents-bucket/<dept>/file.pdf
   └─ S3 "Object Created" → EventBridge rule → sync_trigger Lambda
        ├─ writes <key>.metadata.json  { "metadataAttributes": { "department": "<dept>" } }
        └─ bedrock.StartIngestionJob(KB, dataSource[<dept>])
             └─ Bedrock chunks (512 tok / 20% overlap) → Titan v2 embeds → AOSS index
```

**Retrieval (read path)**
```
Person 2 agent: query_knowledge_base(query, department, top_k)
   └─ invokes query_kb Lambda
        └─ bedrock.Retrieve(KB, query, filter: department == <dept>)
             └─ returns [ { content, source, score }, ... ]  → agent drafts reply
```

## 3. Key technical decisions

- **`Retrieve` (step 02), not `RetrieveAndGenerate`.** Session 09 frames RAG as
  three steps — **01 Ingest → 02 Retrieve → 03 Generate** (Deck 16, "Retrieval-
  augmented generation, managed"). This tool implements **step 02**: semantic
  search that "returns the passages most relevant to the user question", i.e. the
  `Retrieve` API returning `content/source/score`. **Step 03 (Generate) is done
  by Person 2's agent** — which is precisely the Strands agent loop from Session
  10 (*reason → invoke tool → observe → generate*). Doing it this way means the
  agent's own system prompt and Bedrock **guardrailConfig** (Deck 16, "Attach
  guardrails & retrieval inline") apply to the final answer; `RetrieveAndGenerate`
  would generate inside the KB call, bypassing the agent and not matching the
  per-chunk return schema. Both APIs are the same managed service (Bedrock
  Knowledge Bases) — no new tool is introduced.
- **One data source per department + metadata filtering.** Each `<dept>/` prefix
  is a separate `CfnDataSource`, and every chunk is tagged `department=<dept>`
  via a sidecar. Retrieval passes a `filter: {equals: {key: department}}` so HR
  docs can never surface in an IT answer. Separate sources also let us re-sync
  one department without touching the others.
- **Metadata via sidecar files, injected at sync time.** Bedrock reads
  `<object>.metadata.json` next to each document. The `sync_trigger` Lambda
  derives the department from the S3 prefix and writes that sidecar
  automatically, so uploaders don't have to.
- **Vector index via a dependency-free custom resource.** Bedrock does *not*
  create the AOSS index; it must pre-exist with the correct kNN mapping. The
  custom resource signs `aoss` requests with botocore's `SigV4Auth` and sends
  them with `urllib` — **no `opensearch-py` / Docker bundling**, so the deploy is
  hermetic. faiss/HNSW, L2 space, 1024 dims (matches Titan v2).
- **EventBridge over direct S3 notifications.** The bucket is owned by Person 1.
  Subscribing through EventBridge avoids mutating another stack's bucket from
  this stack and is the more decoupled, scalable pattern.

## 4. Security / least privilege (rubric)

- The KB service role can invoke **only** the specific Titan model ARN, reach
  **only** this collection (`aoss:APIAccessAll` scoped to the collection ARN),
  and read **only** the documents bucket.
- The query Lambda holds **only** `bedrock:Retrieve` on this one KB ARN — and no
  direct AOSS access (Bedrock uses the KB role to reach AOSS).
- The sync Lambda holds `s3:PutObject/GetObject` on the bucket and
  `bedrock:StartIngestionJob` on this KB only.
- AOSS data-access policy lists exactly two principals: the KB role and the
  index-setup role. No `Resource: "*"` on any data action.

## 5. Cost estimation (meaningful unit)

The dominant cost of any Bedrock-KB-on-AOSS design is the **always-on
OpenSearch Serverless OCU floor**, not the per-query work.

**Fixed (monthly, illustrative us-east-1):**
- OpenSearch Serverless: minimum **2 OCU** dev (redundancy off) ≈ `2 × $0.24/h × 730 h ≈ $350/mo`;
  **4 OCU** with prod redundancy ≈ `$700/mo`. This is ~all of the fixed cost.
- S3 storage for docs + embeddings: cents/GB-month — negligible.

**Variable (per unit):**
- **Per document indexed:** ≈ document tokens × Titan v2 rate (~`$0.02 / 1M tokens`).
  A 2,000-token doc ≈ **$0.00004** to embed. Re-embedded on each re-sync.
- **Per RAG query answered:** embed the query (~30 tokens ≈ `$0.0000006`) + an
  AOSS vector search (covered by the OCU floor) + a Lambda invocation
  (~$0.0000002). **Marginal cost ≈ a tiny fraction of a cent.**

**Takeaway:** cost is **fixed-dominated**. At 1 query/min (~43k/mo) the effective
cost is ≈ `$350 / 43,000 ≈ $0.008 per query` — almost entirely the OCU floor.
The design is cheap at scale and (relatively) expensive when idle; the lever for
a low-traffic deployment is the OCU floor, not per-query optimization.

## 6. Limitations / assumptions

- **AOSS OCU floor** makes a fully idle deployment cost ~$350/mo. For a pure demo
  this could be swapped for `pgvector` on a small RDS/Aurora Serverless v2, at
  the cost of more wiring. Documented as a cost trade-off, not implemented.
- **Network policy is `AllowFromPublic`.** Access is still gated by SigV4 + the
  data-access policy, but a production hardening would place the collection
  behind a VPC endpoint (`AllowFromPublic:false`). Encryption uses the AWS-owned
  AOSS key; a CMK could be substituted.
- **Ingestion is not debounced.** If many files land within one in-flight
  ingestion job, the `ConflictException` is swallowed and files added after a job
  starts are picked up on the next upload. A production version would debounce
  via an SQS FIFO queue or a short Step Functions wait.
- **Assumes Person 1 enables EventBridge** on the documents bucket
  (`event_bridge_enabled=True`) and that documents are stored under one of the
  four department prefixes; objects outside a known prefix are skipped.
- **Full re-embed on re-sync** of a data source. Fine at this scale; large
  corpora would benefit from incremental/changed-only sync.

## 7. Integration points

- **Person 1 (InfraStack):** owns `documents-bucket`. Pass it in:
  `RagStack(app, "RagStack", documents_bucket=infra.documents_bucket, env=env)`.
  Set `event_bridge_enabled=True` on that bucket.
- **Person 2 (agents):** the query Lambda can be consumed two deck-aligned ways:
  1. **Strands `@tool` wrapper** (Session 10, "Creating custom tools"): import
     [integration/query_knowledge_base_tool.py](integration/query_knowledge_base_tool.py)
     and set env `QUERY_KB_LAMBDA_ARN` to the exported `RagQueryToolLambdaArn`.
  2. **AgentCore Gateway target** (Session 10, "Tool integration flow"): register
     the Lambda as a Gateway tool target so it's exposed over MCP with auth +
     semantic discovery — no wrapper code in the agent.

  Either way the return schema is frozen: `{content, source, score}`. Mock it
  during dev.
- **Exports:** `RagKnowledgeBaseId`, `RagQueryToolLambdaArn`, `RagCollectionEndpoint`.

## 8. Deploy / test

```bash
cd person3_rag
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Standalone (against an existing documents bucket):
cdk deploy -c documentsBucketName=<your-documents-bucket>

# Seed sample docs (triggers auto-sync) and query:
aws s3 cp sample_docs/hr/leave_policy.md     s3://<bucket>/hr/leave_policy.md
aws s3 cp sample_docs/it/access_requests.md  s3://<bucket>/it/access_requests.md

python integration/test_query_kb.py <RagQueryToolLambdaArn> "how many vacation days" hr
```

## 9. Mapping to course decks (Sessions 09 & 10)

Every service used here is from the material covered in class; nothing is a
different tool. Quick trace:

| Component | Deck reference |
|---|---|
| Bedrock Knowledge Base, Ingest→Retrieve→Generate | Deck 16 §05 "Retrieval-augmented generation, managed" |
| OpenSearch Serverless as vector store | Deck 16 §05 — vector stores list (OpenSearch Serverless, Aurora PostgreSQL, Pinecone, S3 Vectors) |
| Fixed-size chunking, 512 tokens / 20% overlap | Deck 16 §05 "Chunking strategies" + Deck 16 takeaway "Start with fixed-size (512 tokens, 20% overlap)" |
| S3 as the data source | Deck 16 §05 — data sources list (Amazon S3, …) |
| `Retrieve` step (returns passages) | Deck 16 §05 slide — step "02 Retrieve" |
| Agent generates over retrieved chunks; guardrails at the agent | Deck 16 §04/§06 (guardrailConfig, `stopReason`) + Deck 17 agent loop |
| `query_knowledge_base` as a Strands `@tool` (boto3 inside) | Deck 17 §05 "Creating custom tools" |
| Tool logic in a Lambda, optionally exposed via AgentCore Gateway | Deck 17 §03 "Tool integration flow" (Lambda → MCP tool) |
| Embeddings (Titan Embeddings v2) | Deck 16 §05 — the "generates embeddings" step (Amazon's standard Bedrock embedder) |

Plumbing not specific to the AI decks — S3 events via **EventBridge**, a CDK
custom resource to create the AOSS index, and IAM — is standard AWS covered in
earlier sessions, not a substitute AI service.

## 10. File map

```
person3_rag/
  rag_stack.py                          # the RagStack (drop into the team CDK app)
  app.py                                # standalone entry point for isolated deploy
  cdk.json, requirements.txt
  lambdas/
    query_kb/handler.py                 # bedrock:Retrieve, dept-filtered
    sync_trigger/handler.py             # S3 event → metadata sidecar → ingestion job
    index_setup/handler.py              # custom resource: create AOSS kNN index (no deps)
  integration/
    query_knowledge_base_tool.py        # Strands @tool wrapper for Person 2
    test_query_kb.py                    # smoke test
  sample_docs/{hr,it}/...               # demo documents
```
