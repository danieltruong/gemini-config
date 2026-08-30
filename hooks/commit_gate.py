#!/usr/bin/env python3
"""PreToolUse hook: Verifies git commit messages follow Conventional Commits format."""
import json
import re
import sys

def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        json.dump({"decision": "allow"}, sys.stdout)
        return

    tool_call = ev.get("toolCall", {})
    if tool_call.get("name") != "run_command":
        json.dump({"decision": "allow"}, sys.stdout)
        return

    cmd = tool_call.get("args", {}).get("CommandLine", "")
    if "git commit" not in cmd:
        json.dump({"decision": "allow"}, sys.stdout)
        return

    # Extract -m message
    m = re.search(r'-m\s+([\'"])(.*?)\1', cmd, re.DOTALL)
    if not m:
        # Check heredoc form
        m_here = re.search(r'-m\s+["\']?\$\(cat\s+<<[\'"]?(\w+)[\'"]?\n(.*?)\n\1', cmd, re.DOTALL)
        if m_here:
            lines = m_here.group(2).strip().split('\n')
            subject = lines[0].strip()
        else:
            json.dump({"decision": "allow"}, sys.stdout)
            return
    else:
        subject = m.group(2).strip().split('\n')[0]

    cc_pattern = r'^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-zA-Z0-9._/-]+\))?!?: .+'
    if not re.match(cc_pattern, subject):
        json.dump({
            "decision": "deny",
            "reason": f"Commit subject is not Conventional Commits: '{subject}'. Invoke the caveman-commit skill to write a compliant message."
        }, sys.stdout)
        return

    if len(subject) > 50:
        json.dump({
            "decision": "deny",
            "reason": f"Commit subject is {len(subject)} chars (limit 50): '{subject}'. Shorten it using caveman-commit format."
        }, sys.stdout)
        return

    json.dump({"decision": "allow"}, sys.stdout)

if __name__ == "__main__":
    main()
