#!/usr/bin/env bash
# Builds the React SPA (frontend/dist) that FrontendStack's BucketDeployment
# uploads. Reads the API URL + Cognito IDs from the deployed FrontendStack's
# CloudFormation outputs and bakes them into the Vite build via .env.production.
#
# FrontendStack must already exist (deploy once to create the API/Cognito/
# CloudFront resources, then run this and deploy again to upload the app).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK="${FRONTEND_STACK:-FrontendStack}"
REGION="${AWS_REGION:-eu-west-1}"

get() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

API_URL="$(get ApiUrl)"
POOL_ID="$(get UserPoolId)"
CLIENT_ID="$(get UserPoolClientId)"

if [ -z "$API_URL" ] || [ "$API_URL" = "None" ]; then
  echo "ERROR: could not read $STACK outputs (ApiUrl). Deploy $STACK once first, then re-run." >&2
  exit 1
fi

cat > "$ROOT/frontend/.env.production" <<EOF
VITE_API_URL=${API_URL%/}
VITE_COGNITO_USER_POOL_ID=$POOL_ID
VITE_COGNITO_CLIENT_ID=$CLIENT_ID
EOF
echo "Wrote frontend/.env.production:"
sed 's/^/  /' "$ROOT/frontend/.env.production"

cd "$ROOT/frontend"
npm install
npm run build
echo "Built frontend/dist ($(du -sh dist | cut -f1))"
