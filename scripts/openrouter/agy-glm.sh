#!/usr/bin/env bash
export OPENROUTER_MODEL="z-ai/glm-5.3-flash"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/agy-deepseek.sh" "$@"
