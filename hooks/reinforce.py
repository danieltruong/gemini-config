#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: Injects Caveman & Ponytail directives."""
import json
import sys

def main():
    try:
        # Consume stdin context
        _ = json.load(sys.stdin)
    except Exception:
        pass

    out = {
        "injectSteps": [
            {
                "ephemeralMessage": (
                    "CAVEMAN & PONYTAIL MODE ACTIVE.\n"
                    "- Terse output: drop filler, pleasantries, conversational hedging.\n"
                    "- Exact technical details: preserve all paths, commands, code symbols, numbers, and negations.\n"
                    "- Laziest working solution (YAGNI): minimal diff, stdlib/native features first, no unrequested abstractions."
                )
            }
        ]
    }
    json.dump(out, sys.stdout)

if __name__ == "__main__":
    main()
