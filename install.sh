#!/usr/bin/env bash
# Symlink gemini-config into ~/.agents and ~/.gemini
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="${AGENTS_CONFIG_DIR:-$HOME/.agents}"
GEMINI_DIR="${GEMINI_CONFIG_DIR:-$HOME/.gemini}"

mkdir -p "$AGENTS_DIR" "$GEMINI_DIR" "$AGENTS_DIR/skills"

# Link global rules
for doc in GEMINI.md AGENTS.md; do
  ln -sfn "$REPO/$doc" "$GEMINI_DIR/$doc"
  ln -sfn "$REPO/$doc" "$AGENTS_DIR/$doc"
done

# Link shared directories
for d in agents hooks scripts; do
  ln -sfn "$REPO/$d" "$AGENTS_DIR/$d"
done

# Link skills individually
for s in "$REPO"/skills/*/; do
  name="$(basename "$s")"
  ln -sfn "${s%/}" "$AGENTS_DIR/skills/$name"
done

# Link hooks and mcp config
for cfg in hooks.json mcp_config.json; do
  if [ -f "$REPO/$cfg" ]; then
    cp "$REPO/$cfg" "$AGENTS_DIR/$cfg"
  fi
done

echo "gemini-config installed into $AGENTS_DIR and $GEMINI_DIR"
