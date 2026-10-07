#!/usr/bin/env bash
# Link gemini-config into ~/.gemini (global rules) and ~/.gemini/config (global customizations).
set -euo pipefail
command -v jq >/dev/null || { echo "jq required" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GEMINI_DIR="${GEMINI_CONFIG_DIR:-$HOME/.gemini}"
CONFIG="$GEMINI_DIR/config"
AGENTS_SKILLS="${AGENTS_SKILLS_DIR:-$HOME/.agents/skills}"

# Git Bash without native symlinks makes ln -s copy instead: stop before anything is replaced.
probe="$(mktemp -d)"
ln -s "$REPO/install.sh" "$probe/link" 2>/dev/null || true
if [ ! -L "$probe/link" ]; then
  rm -rf "$probe"
  echo "ln -s made a copy, not a link (Git Bash without symlink support); run install.ps1 instead" >&2
  exit 1
fi
rm -rf "$probe"

mkdir -p "$CONFIG"

# A real directory at a link path holds files made in the app: never replace it.
link_dir() {
  if [ -d "$2" ] && [ ! -L "$2" ]; then
    echo "$2 is a real directory; move its files into $1, delete it, rerun" >&2
    exit 1
  fi
  ln -sfn "$1" "$2"
}

# 1. Global rules. agy loads ~/.gemini/config/rules; ~/.gemini/antigravity-cli/rules is not read.
ln -sfn "$REPO/GEMINI.md" "$GEMINI_DIR/GEMINI.md"
link_dir "$REPO/rules" "$CONFIG/rules"
[ -f "$REPO/rules/local.md" ] || echo "no rules/local.md; copy rules/local.example.md, set trigger: always_on, fill in paths" >&2

# 2. Global customizations
for d in agents hooks scripts skills; do link_dir "$REPO/$d" "$CONFIG/$d"; done
ln -sfn "$REPO/hooks.json" "$CONFIG/hooks.json"
# OpenRouter launchers and MCP server go to ~/scripts, where rules/openrouter.md calls them.
mkdir -p "$HOME/scripts"
for f in "$REPO"/scripts/openrouter/*; do
  if [ -f "$f" ]; then ln -sfn "$f" "$HOME/scripts/$(basename "$f")"; fi
done

# 3. agy scans skills in the global config dir only; ~/.agents/skills is workspace-scoped.
# Older installs linked every skill there. Drop those links, never another tool's.
if [ -d "$AGENTS_SKILLS" ]; then
  for l in "$AGENTS_SKILLS"/*; do
    if [ -L "$l" ]; then
      case "$(readlink "$l")" in "$REPO/skills/"*) rm "$l" ;; esac
    fi
  done
fi

# 4. MCP config: repo servers merged with machine-local mcp_config.local.json
LOCAL="$CONFIG/mcp_config.local.json"
# The agy MCP docs list no ~ expansion in args: repo entries say ~/ and get the real home here.
HOME_ARGS='.mcpServers |= map_values(if .args then .args |= map(if startswith("~/") then $home + .[1:] else . end) else . end)'
if [ -f "$LOCAL" ]; then
  jq -s --arg home "$HOME" ".[0] * .[1] | $HOME_ARGS" "$REPO/mcp_config.json" "$LOCAL" > "$CONFIG/mcp_config.json"
else
  echo "no $LOCAL; only repo MCP servers installed" >&2
  jq --arg home "$HOME" "$HOME_ARGS" "$REPO/mcp_config.json" > "$CONFIG/mcp_config.json"
fi

echo "installed into $GEMINI_DIR and $CONFIG"
