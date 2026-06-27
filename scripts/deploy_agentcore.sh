#!/usr/bin/env bash
# Deploys the Waiver Processor Strands agent to Amazon Bedrock AgentCore Runtime
# using the bedrock-agentcore starter toolkit (direct_code_deploy — ARM64 deps
# are cross-compiled in the cloud, no local Docker required).
#
# Why an isolated build dir: the toolkit's dependency builder otherwise picks up
# the project-root requirements.txt (aws-cdk-lib) instead of the agent's deps, so
# we stage just the agent code + a minimal requirements.txt in a clean directory.
#
# After this runs, copy the printed Agent ARN into cdk.json
# ("waiver_agent_runtime_arn") and `cdk deploy AgentStack` so the router invokes
# the AgentCore runtime. The agent's execution role also needs the tool
# permissions in scripts/agentcore_exec_policy.json (attached automatically below).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-eu-west-1}"
NAME="${AGENT_NAME:-waiveragent}"
SRC="$ROOT/lambdas/agents/waiver"
BUILD="$(mktemp -d)/acwaiver"
export AGENTCORE_SUPPRESS_RECOMMENDATION=1 AWS_REGION="$REGION"

mkdir -p "$BUILD"
cp "$SRC/runtime_app.py" "$SRC/waiver_agent.py" "$SRC/waiver_tools.py" "$BUILD/"
printf 'strands-agents>=0.1.0\nbedrock-agentcore>=1.0.0\n' > "$BUILD/requirements.txt"

# Resolve the tool ARNs / bucket the agent needs at runtime.
acct="$(aws sts get-caller-identity --query Account --output text)"
criteria_bucket="$(aws cloudformation describe-stacks --stack-name InfraStack --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='WaiverCriteriaBucketName'].OutputValue" --output text)"
email_from="$(python3 -c "import json;print(json.load(open('$ROOT/cdk.json'))['context']['email_from'])")"
guardrail_id="$(aws cloudformation describe-stacks --stack-name AgentStack --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='GuardrailId'].OutputValue" --output text)"

cd "$BUILD"
agentcore configure -ni -r "$REGION" -dt direct_code_deploy \
  -e runtime_app.py -n "$NAME" -rf requirements.txt -do -dm

agentcore deploy -a "$NAME" --force-rebuild-deps -auc \
  --env "EMAIL_FROM=$email_from" \
  --env "WAIVER_CRITERIA_BUCKET=$criteria_bucket" \
  --env "START_WAIVER_LAMBDA_ARN=arn:aws:lambda:${REGION}:${acct}:function:waiver-start-workflow" \
  --env "UPDATE_WAIVER_LAMBDA_ARN=arn:aws:lambda:${REGION}:${acct}:function:waiver-update-state" \
  --env "GET_WAIVER_LAMBDA_ARN=arn:aws:lambda:${REGION}:${acct}:function:waiver-get-state" \
  --env "GUARDRAIL_ID=${guardrail_id}" --env "GUARDRAIL_VERSION=DRAFT"

# Grant the auto-created execution role access to the agent's tools.
role_arn="$(grep -E 'execution_role:' "$BUILD/.bedrock_agentcore.yaml" | grep -v null | head -1 | awk '{print $2}')"
role_name="${role_arn##*role/}"
sed -e "s/\${ACCT}/$acct/g" -e "s/\${REGION}/$REGION/g" -e "s/\${CRITERIA_BUCKET}/$criteria_bucket/g" -e "s/\${GUARDRAIL_ID}/$guardrail_id/g" \
  "$ROOT/scripts/agentcore_exec_policy.json" > "$BUILD/policy.json"
aws iam put-role-policy --role-name "$role_name" --policy-name WaiverAgentCoreToolAccess \
  --policy-document "file://$BUILD/policy.json" --region "$REGION"

echo
echo "Done. Copy the Agent ARN above into cdk.json -> context.waiver_agent_runtime_arn, then:"
echo "  npx aws-cdk@2.1128.1 deploy AgentStack"
