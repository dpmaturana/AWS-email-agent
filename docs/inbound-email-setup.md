# Real inbound email setup (SES + Route 53 domain)

By default the project simulates inbound email by dropping an `.eml` into the
`incoming/` prefix of the raw-emails bucket. To receive **real** email, SES needs
a domain you control (you can't receive at a `gmail.com` address). This runbook
wires a Route 53 domain into the existing ingestion pipeline.

Region used throughout: **eu-west-1** (SES inbound is only supported in certain
regions; eu-west-1 is one of them).

The flow once set up:

```
someone emails  anything@yourdomain
   → SES receipt rule  (writes raw email to s3://raw-emails-bucket/incoming/<msgId>)
   → S3 OBJECT_CREATED event
   → ingestion Lambda   (parses, builds payload, invokes agent)
```

---

## 1. Register the domain (manual, AWS console)

Route 53 → **Registered domains** → **Register domains**.
- Pick a cheap TLD (e.g. `.click`, `.link` — often ~$3–12/yr).
- Complete the purchase. Provisioning takes a few minutes to ~an hour.
- This automatically creates a **hosted zone** for the domain.

## 2. Verify the domain in SES (manual, AWS console)

SES (eu-west-1) → **Identities** → **Create identity** → **Domain**.
- Enter your domain.
- If the hosted zone is in the same account, choose **"Publish records to Route 53"**
  — SES adds the DKIM/verification records for you.
- Wait until the identity shows **Verified**.

## 3. Add the MX record (manual, Route 53)

In the domain's hosted zone, create an **MX** record so mail is delivered to SES:

| Field | Value |
|---|---|
| Record name | (leave blank — the apex domain) |
| Type | `MX` |
| Value | `10 inbound-smtp.eu-west-1.amazonaws.com` |
| TTL | 300 |

CLI alternative (replace ZONE_ID and DOMAIN):

```bash
aws route53 change-resource-record-sets --hosted-zone-id ZONE_ID --change-batch '{
  "Changes": [{
    "Action": "UPSERT",
    "ResourceRecordSet": {
      "Name": "DOMAIN.",
      "Type": "MX",
      "TTL": 300,
      "ResourceRecords": [{"Value": "10 inbound-smtp.eu-west-1.amazonaws.com"}]
    }
  }]
}'
```

## 4. Enable inbound in the stack

In `cdk.json`, set the domain:

```json
"inbound_domain": "yourdomain.click"
```

Deploy:

```bash
cdk deploy InfraStack
```

This creates an SES receipt rule set named **`email-agent-inbound`** with a single
rule that stores incoming mail to `s3://<raw-emails-bucket>/incoming/`.

## 5. Activate the rule set (manual, CLI)

SES allows only one *active* receipt rule set per region, and CDK can't set it
active. Activate it once:

```bash
aws ses set-active-receipt-rule-set --rule-set-name email-agent-inbound --region eu-west-1
```

Confirm:

```bash
aws ses describe-active-receipt-rule-set --region eu-west-1
```

## 6. Test it for real

From any email client (your Gmail), send an email to **`anything@yourdomain.click`**.
Within a minute:

```bash
# raw email stored by SES
aws s3 ls s3://<raw-emails-bucket>/incoming/ --region eu-west-1

# ingestion Lambda fired automatically
aws logs filter-log-events \
  --log-group-name /aws/lambda/<ingestion-fn-name> \
  --region eu-west-1 --start-time $(( ($(date +%s) - 300) * 1000 )) \
  --query "events[].message" --output text | grep "\[ingestion\]"
```

---

## Notes / gotchas

- **Disable to revert to simulation:** set `"inbound_domain": ""` and redeploy.
  The S3-drop simulation always works regardless.
- **No Lambda action on the rule** — only an S3 action. The existing S3 trigger
  does the invoke; adding a Lambda action too would double-invoke the Lambda.
- **Sending replies:** verifying the domain also lets the agents send *from*
  any `@yourdomain` address. You can leave `email_from` as the verified Gmail,
  or switch it to e.g. `agent@yourdomain` later.
- **Still in sandbox:** you can receive from anyone, but to *send* to arbitrary
  (unverified) recipients you'd still need SES production access.
