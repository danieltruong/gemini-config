#!/usr/bin/env python3
"""PostToolUse hook: lints an AI instruction doc right after it is written.

agy 1.1.27 never dispatches PostToolUse, so stop_gate.py does the same check at Stop.
Kept wired for the build that ships it.
"""
import json
import os
import subprocess
import sys

import hookpaths


def target(args):
    for k in ("TargetFile", "AbsolutePath"):
        if args.get(k):
            return args[k]
    return None


def main():
    try:
        ev = json.load(sys.stdin)
        path = target(ev.get("toolCall", {}).get("args", {}))
        if path and hookpaths.is_instruction_doc(path) and os.path.isfile(path):
            proc = subprocess.run(
                [sys.executable, hookpaths.DOCS_LINT, path], capture_output=True, text=True, timeout=10
            )
            if proc.returncode != 0:
                findings = (
                    f"ai-docs-lint failed on {path}:\n{proc.stdout}{proc.stderr}"
                    "Fix these before continuing.\n"
                )
                hookpaths.append(hookpaths.PENDING_FINDINGS, findings)
                sys.stderr.write(findings)
    except Exception:
        pass
    json.dump({}, sys.stdout)


if __name__ == "__main__":
    main()
