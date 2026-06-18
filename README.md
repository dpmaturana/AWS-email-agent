# Email Routing Agent — AWS CDK Monorepo

Cloud-native email routing agent built with Strands Agents and Amazon Bedrock AgentCore.

## Architecture

Incoming emails are parsed by an ingestion Lambda, classified by an AI agent, and routed to one of three flows:
- **Forward** — email is routed to the appropriate team
- **RAG** — agent queries the internal knowledge base and sends an auto-reply
- **Waiver** — a dedicated agent manages a multi-step approval workflow with a human in the loop

## Project structure

```
email-agent/
├── app.py                        # CDK app entry point
├── cdk.json                      # CDK config + context params
├── requirements.txt
├── stacks/
│   ├── infra_stack.py            # SES, S3, ingestion Lambda        → Person 1
│   ├── rag_stack.py              # Bedrock KB, OpenSearch, RAG tool  → Person 3
│   ├── waiver_stack.py           # DynamoDB, Step Functions, tools   → Person 4
│   ├── agent_stack.py            # Both agents, Guardrails           → Person 2
│   └── frontend_stack.py         # Cognito, API GW, CloudFront       → Person 5
└── lambdas/
    ├── ingestion/                 # Parse email + invoke agent        → Person 1
    ├── agents/
    │   ├── router/                # Agent 1 logic                     → Person 2
    │   └── waiver/                # Agent 2 logic                     → Person 2
    ├── rag/                       # query_knowledge_base tool         → Person 3
    ├── waiver_tools/
    │   ├── start_workflow/        # start_waiver_workflow tool        → Person 4
    │   ├── update_state/          # update_waiver_state tool          → Person 4
    │   └── get_state/             # get_waiver_state tool             → Person 4
    ├── approval/                  # Human approve/reject handler      → Person 4
    └── api/
        ├── list_waivers/          # GET /waivers                      → Person 5
        ├── get_waiver/            # GET /waivers/{id}                 → Person 5
        └── decide_waiver/         # POST /waivers/{id}/decide         → Person 5
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Bootstrap CDK (first time only)
cdk bootstrap

# Deploy all stacks
cdk deploy --all

# Deploy a single stack
cdk deploy InfraStack
```

## Context parameters (cdk.json)

Before deploying, update these values in `cdk.json`:

| Parameter | Description |
|---|---|
| `email_from` | SES-verified sender address |
| `email_demo_recipient` | SES-verified test recipient |
| `approver_email` | Email address that receives waiver approval notifications |

## Deployment order

Stacks have dependencies — CDK handles this automatically with `cdk deploy --all`.
Manual order if deploying individually: `InfraStack` → `RagStack` + `WaiverStack` → `AgentStack` → `FrontendStack`

## Ownership by person

| Person | Stack(s) | Lambda(s) |
|---|---|---|
| Person 1 | InfraStack (scaffold + S3 + SES) | lambdas/ingestion |
| Person 2 | AgentStack | lambdas/agents/router, lambdas/agents/waiver |
| Person 3 | RagStack | lambdas/rag |
| Person 4 | WaiverStack | lambdas/waiver_tools/*, lambdas/approval |
| Person 5 | FrontendStack | lambdas/api/*, frontend/ |

## Interface contracts

All team contracts are documented in `docs/contracts.md`.
**Do not change a contract without team consensus** — other people's code depends on it.
