#!/usr/bin/env python3
"""PostToolUse hook: Lints modified AI instruction docs (GEMINI.md, SKILL.md, AGENTS.md, agents/*.md)."""
import json
import os
import subprocess
import sys

def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        json.dump({}, sys.stdout)
        return

    # Check if tool modified a doc file
    tool_call = ev.get("toolCall", {})
    args = tool_call.get("args", {})
    f = args.get("TargetFile") or args.get("AbsolutePath") or ""

    if not f:
        json.dump({}, sys.stdout)
        return

    norm_f = f.replace('\\', '/')
    if any(norm_f.endswith(suffix) for suffix in ('GEMINI.md', 'AGENTS.md', 'SKILL.md')) or '/agents/' in norm_f:
        lint_script = os.path.expanduser("~/.gemini/config/scripts/ai-docs-lint.py")
        if not os.path.exists(lint_script):
            lint_script = os.path.join(os.path.dirname(__file__), "..", "scripts", "ai-docs-lint.py")

        if os.path.exists(lint_script):
            subprocess.run([sys.executable, lint_script, f], capture_output=True, text=True)

    json.dump({}, sys.stdout)

if __name__ == "__main__":
    main()
