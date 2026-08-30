#!/usr/bin/env bash
# Pre-push verifier for gemini-config
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== 1. AI Docs Lint ==="
python3 "$REPO_ROOT/scripts/ai-docs-lint.py" --all

echo "=== 2. Hook Unit Tests ==="
python3 -m unittest discover -s "$REPO_ROOT/hooks/tests"

echo "=== All pre-push checks passed ==="
