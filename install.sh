#!/usr/bin/env bash
# Link gemini-config into ~/.gemini (global rules) and ~/.gemini/config (global customizations).
set -euo pipefail
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEMINI_DIR="${GEMINI_CONFIG_DIR:-$HOME/.gemini}"
CONFIG="$GEMINI_DIR/config"
AGENTS_SKILLS="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"

mkdir -p "$CONFIG" "$AGENTS_SKILLS"

# 1. Global rules
ln -sfn "$REPO/GEMINI.md" "$GEMINI_DIR/GEMINI.md"

# 2. Global customizations
for d in agents hooks scripts; do ln -sfn "$REPO/$d" "$CONFIG/$d"; done
ln -sfn "$REPO/hooks.json" "$CONFIG/hooks.json"

# 3. Skills into the global root and the cross-agent ~/.agents/skills dir
ln -sfn "$REPO/skills" "$CONFIG/skills"
for s in "$REPO"/skills/*/; do
  name="$(basename "$s")"
  ln -sfn "${s%/}" "$AGENTS_SKILLS/$name"
done

# 4. MCP config: repo servers merged with machine-local mcp_config.local.json
LOCAL="$CONFIG/mcp_config.local.json"
if [ -f "$LOCAL" ]; then
  jq -s '.[0] * .[1]' "$REPO/mcp_config.json" "$LOCAL" > "$CONFIG/mcp_config.json"
else
  echo "no $LOCAL; only repo MCP servers installed" >&2
  cp "$REPO/mcp_config.json" "$CONFIG/mcp_config.json"
fi

echo "installed into $GEMINI_DIR and $CONFIG"
