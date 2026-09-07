#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: Injects Caveman & Ponytail directives."""
import json
import os
import sys

import hookpaths


def pending_findings():
    """Findings left by PostToolUse hooks, consumed once."""
    try:
        with open(hookpaths.PENDING_FINDINGS, encoding="utf-8") as fh:
            text = fh.read().strip()
        os.remove(hookpaths.PENDING_FINDINGS)
        return text
    except Exception:
        return ""


def main():
    try:
        # Consume stdin context
        _ = json.load(sys.stdin)
    except Exception:
        pass

    steps = [
        {
            "ephemeralMessage": (
                "CAVEMAN and PONYTAIL mode active.\n"
                "- Reply terse. Keep every path, command, symbol, number, and negation exact.\n"
                "- Ship the shortest working diff. Reuse the repo, then stdlib, then native platform features.\n"
                "- Never add filler, pleasantries, hedging, or unrequested abstractions."
            )
        }
    ]
    findings = pending_findings()
    if findings:
        steps.append({"ephemeralMessage": findings})

    json.dump({"injectSteps": steps}, sys.stdout)


if __name__ == "__main__":
    main()
