#!/usr/bin/env bash
# Builds the Strands Lambda layer consumed by AgentStack (layers/strands).
# The layer is git-ignored (see .gitignore) — every developer / CI run must
# rebuild it before `cdk synth` / `cdk deploy`, or AgentStack fails with
# "CannotFindAsset ... layers/strands".
#
# Uses --platform manylinux2014_x86_64 so the wheels match the Lambda runtime
# (linux/x86_64) even when building on macOS, without needing Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$ROOT/layers/strands/python"

rm -rf "$TARGET"
mkdir -p "$TARGET"

pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --target "$TARGET" \
  strands-agents

echo "Strands layer built at $TARGET ($(du -sh "$TARGET" | cut -f1))"
