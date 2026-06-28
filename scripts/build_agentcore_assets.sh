#!/usr/bin/env bash
# Builds the flat AgentCore code packages (deps + source) that AgentStack's
# CfnRuntime constructs upload as S3 assets. Linux/ARM64 (aarch64) wheels are
# fetched with pip's --platform flag, so no Docker is required.
#
# Output dirs (git-ignored, rebuilt before every cdk synth/deploy):
#   build/ac_waiver  build/ac_router
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

build_one() {
  local name="$1" src="$2"
  local out="$ROOT/build/ac_$name"
  rm -rf "$out"; mkdir -p "$out"
  # AgentCore runtime is PYTHON_3_10 on ARM64.
  pip install --quiet \
    --platform manylinux2014_aarch64 --implementation cp --python-version 3.10 \
    --only-binary=:all: --target "$out" \
    strands-agents bedrock-agentcore
  cp "$ROOT/lambdas/agents/$src"/*.py "$out"/
  # Ship any bundled data files (e.g. waiver_request_guidelines.md) too.
  cp "$ROOT/lambdas/agents/$src"/*.md "$out"/ 2>/dev/null || true
  echo "built $out ($(du -sh "$out" | cut -f1))"
}

build_one waiver waiver
build_one router router
