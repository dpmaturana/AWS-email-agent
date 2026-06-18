# Email Routing Agent — IE University Administrative Assistant

## Overview

This project implements a cloud-native intelligent email routing system built on AWS, designed for IE University's administrative operations. The system monitors a central student services inbox and autonomously processes incoming emails across four departments: **Admissions, Financial Aid, Academic Affairs, and Student Services**.

Each email is classified and routed through one of three flows:

- **Routing** — the agent identifies which department should handle the email (Admissions, Financial Aid, Academic Affairs, or Student Services) and forwards it to the appropriate team. No response is sent to the student — the relevant department takes it from there.
- **Automatic response** — if the email contains a question that can be answered with IE's internal documentation (policies, procedures, program requirements, deadlines, FAQs), the agent queries the knowledge base and sends a direct reply to the student without any human intervention.
- **Waiver processing** — the agent detects a waiver request (academic requirement waiver, tuition fee exception, admission requirement waiver, etc.), manages an iterative information-gathering loop with the requestor, and prepares the complete case for human approval by the relevant administrator.

The system reduces manual email triage and accelerates response times for the most common administrative requests, while ensuring that exceptions and waivers receive proper human oversight before a decision is made.

---

## Agentic Application

The core of the system is a multi-agent application built with **Strands Agents** and deployed on **Amazon Bedrock AgentCore**.

### Agent 1 — Email Router
Receives each parsed incoming email and is responsible for:
1. Identifying the department the email belongs to (Admissions, Financial Aid, Academic Affairs, Student Services)
2. Classifying the intent: forward to team, automatic RAG response, or waiver processing
3. Invoking the appropriate action or delegating to Agent 2

### Agent 2 — Waiver Processor (sub-agent)
Invoked by Agent 1 when a waiver request is detected. Manages the full waiver lifecycle:
1. Identifies the waiver type (e.g. academic requirement waiver, tuition exception, late enrollment waiver)
2. Fetches the criteria required for that waiver type — required documents, supporting information, eligibility conditions
3. Evaluates whether all required information is present in the email and attachments
4. If incomplete: emails the student asking for the specific missing items, saves state, and resumes when they reply
5. Once complete: initiates the human approval workflow and notifies the responsible administrator
6. Sends the final decision (approved or rejected) to the student

This iterative loop can run as many cycles as needed until the agent has sufficient information to proceed to a decision.

Both agents are configured with **Bedrock Guardrails** for PII filtering and topic grounding, and use **AgentCore session memory** to maintain full context across a multi-email conversation thread with the same student.

---

## Architecture

### Services used

| Layer | Service | Purpose |
|---|---|---|
| Ingestion | Amazon SES | Receives incoming emails |
| Ingestion | Amazon S3 | Stores raw emails and internal documents |
| Ingestion | AWS Lambda | Parses emails, detects reply threads, invokes AgentCore |
| Agents | Amazon Bedrock AgentCore | Hosts and runs both Strands agents |
| Agents | Amazon Bedrock Guardrails | PII filtering and topic enforcement |
| Knowledge | Amazon Bedrock Knowledge Bases | RAG retrieval over IE's internal policy documents |
| Knowledge | Amazon OpenSearch Serverless | Vector index for semantic search |
| State | Amazon DynamoDB | Waiver state, thread memory, conversation history |
| Workflow | AWS Step Functions | Human-in-the-loop approval workflow with waitForTaskToken |
| Notifications | Amazon SNS + SES | Notifies administrators and sends decision emails to students |
| Frontend | Amazon Cognito | Authenticates IE administrators |
| Frontend | Amazon API Gateway + Lambda | REST API for the approval platform |
| Frontend | Amazon S3 + CloudFront | Hosts the React web application |
| IaC | AWS CDK (Python) | All infrastructure defined as code |

### Execution flow

1. A student or applicant sends an email to IE's central administrative inbox via SES, which saves it to S3
2. A Lambda parses the email, reads the In-Reply-To header to detect if it belongs to an existing thread, and invokes Agent 1 via Bedrock AgentCore
3. Agent 1 classifies the email by department and intent:
   - If **forward**: routes the email to the appropriate department team via SES — no reply is sent to the student
   - If **RAG**: queries IE's internal knowledge base, composes a response, and sends it directly to the student
   - If **waiver**: delegates to Agent 2 for full waiver processing
4. If Agent 2 is invoked, it loads the thread memory from DynamoDB, identifies the waiver type, fetches the required criteria from S3, and evaluates completeness
5. If information is missing, Agent 2 emails the student and waits. When they reply, the loop resumes from step 1 with the same waiver_id
6. Once complete, Agent 2 starts a Step Functions execution that pauses with waitForTaskToken and notifies the responsible administrator
7. The administrator logs into the web platform, reviews the full case, and approves or rejects with an optional comment
8. The frontend calls the API which triggers sendTaskSuccess or sendTaskFailure, resuming the workflow
9. The student is notified of the final decision via email

---

## Infrastructure as Code

The entire infrastructure is defined using **AWS CDK (Python)** organized in five independent stacks:

- InfraStack — SES, S3 buckets, ingestion Lambda
- RagStack — Bedrock Knowledge Base, OpenSearch Serverless, retrieval Lambda
- WaiverStack — DynamoDB, Step Functions, waiver tool Lambdas, approval Lambda
- AgentStack — both Strands agents on AgentCore, Guardrails
- FrontendStack — Cognito, API Gateway, S3 + CloudFront

All stacks are account-agnostic and parameterized via CDK context variables.

### Deploy

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cdk bootstrap
cdk deploy --all
```

---

## Cost Model

The system follows a fully serverless, pay-per-use model. The natural unit of cost is **cost per email processed**.

Fixed costs are minimal — primarily the OpenSearch Serverless collection (active during business hours) and the CloudFront distribution.

| Component | Cost driver |
|---|---|
| Bedrock AgentCore | Per agent invocation + input/output tokens |
| Bedrock Knowledge Bases | Per retrieval query |
| Lambda | Per invocation + duration |
| SES | Per email sent and received |
| Step Functions | Per state transition |
| DynamoDB | Per read/write unit |

A detailed cost breakdown per email processed — separating simple routing, RAG responses, and waiver flows — is included in the project report.

---

## Limitations and Assumptions

- The system operates in SES sandbox mode for development — all sender and recipient addresses must be manually verified. In production, IE's institutional domain would be used instead.
- Waiver criteria are defined as static JSON files in S3, representing IE's current policy requirements per waiver type. Updates to criteria require a manual file upload.
- The administrator approval platform has no role-based access control beyond Cognito authentication — all authenticated administrators can review any department's waivers. Department-level access control is out of scope for this version.
- AgentCore session memory is scoped to a single session. Cross-session memory (across multiple emails from the same student) relies on DynamoDB thread history loaded at the start of each invocation.
- The RAG knowledge base is populated with manually uploaded policy documents. Integration with IE's document management systems is out of scope.
