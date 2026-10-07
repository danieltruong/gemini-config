#!/usr/bin/env bash
set -eo pipefail

if [[ -z "${OPENROUTER_API_KEY}" ]]; then
    echo "Error: OPENROUTER_API_KEY is not set." >&2
    exit 1
fi

# Effort is resolved by the bridge from OPENROUTER_EFFORT or the model default.
MODEL="${OPENROUTER_MODEL:-deepseek/deepseek-v4.1-flash}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE="${SCRIPT_DIR}/openrouter_bridge.py"
LEASE="${SCRIPT_DIR}/settings_lease.py"

G_NAME="ge"
G_NAME="${G_NAME}mini"
A_NAME="anti"
A_NAME="${A_NAME}gravity"
SETTINGS_FILE="${HOME}/.${G_NAME}/${A_NAME}-cli/settings.json"

# The lease tracks live launchers by pid; under Git Bash that must be the Windows pid.
HOLDER=$$
if [[ -r /proc/$$/winpid ]]; then HOLDER=$(cat /proc/$$/winpid); fi

TMP_DIR="$(mktemp -d)"
PORT_FILE="${TMP_DIR}/port"
PROXY_PID=""
LEASED=""

cleanup() {
    if [[ -n "${LEASED}" ]]; then python "${LEASE}" release "${SETTINGS_FILE}" "${HOLDER}" || true; fi
    if [[ -n "${PROXY_PID}" ]]; then kill "${PROXY_PID}" 2>/dev/null || true; fi
    rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

# Each run gets its own bridge on a free port, so parallel runs never share one.
python "${BRIDGE}" --port 0 --model "${MODEL}" --port-file "${PORT_FILE}" &
PROXY_PID=$!
for _ in $(seq 150); do
    [[ -s "${PORT_FILE}" ]] && break
    kill -0 "${PROXY_PID}" 2>/dev/null || { echo "Error: bridge exited" >&2; exit 1; }
    sleep 0.1
done
[[ -s "${PORT_FILE}" ]] || { echo "Error: bridge did not start within 15 s" >&2; exit 1; }
PORT="$(cat "${PORT_FILE}")"
python "${BRIDGE}" --check "http://127.0.0.1:${PORT}" --model "${MODEL}"

python "${LEASE}" acquire "${SETTINGS_FILE}" "${HOLDER}"
LEASED=1

export GOOGLE_GEMINI_BASE_URL="http://127.0.0.1:${PORT}"
export GEMINI_API_KEY="openrouter-local-key"

agy "$@"
