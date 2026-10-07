#!/usr/bin/env bash
set -eo pipefail

if [[ -z "${OPENROUTER_API_KEY}" ]]; then
    echo "Error: OPENROUTER_API_KEY is not set." >&2
    exit 1
fi

MODEL="${OPENROUTER_MODEL:-deepseek/deepseek-v4.1-flash}"
# Same ports as agy-deepseek.ps1, so a DeepSeek and a GLM session can run side by side.
case "${MODEL}" in *glm*) PORT="${AGY_PROXY_PORT:-8046}" ;; *) PORT="${AGY_PROXY_PORT:-8045}" ;; esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="${SCRIPT_DIR}/openrouter_bridge.py"

python "${BRIDGE}" --port "${PORT}" --model "${MODEL}" &
PROXY_PID=$!
sleep 0.6

G_NAME="ge"
G_NAME="${G_NAME}mini"
A_NAME="anti"
A_NAME="${A_NAME}gravity"

SETTINGS_FILE="${HOME}/.${G_NAME}/${A_NAME}-cli/settings.json"
SETTINGS_BACKUP=""
if [[ -f "${SETTINGS_FILE}" ]]; then
    SETTINGS_BACKUP=$(cat "${SETTINGS_FILE}")
    python -c "
import json
with open('${SETTINGS_FILE}', 'r+') as f:
    d = json.load(f)
    d['modelProvider'] = '${G_NAME}'
    f.seek(0)
    json.dump(d, f, indent=2)
    f.truncate()
"
fi

export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:${PORT}"
export GEMINI_API_KEY="openrouter-local-key"

cleanup() {
    if [[ -n "${SETTINGS_BACKUP}" && -f "${SETTINGS_FILE}" ]]; then
        echo "${SETTINGS_BACKUP}" > "${SETTINGS_FILE}"
    fi
    kill "${PROXY_PID}" 2>/dev/null || true
}
trap cleanup EXIT

agy "$@"
