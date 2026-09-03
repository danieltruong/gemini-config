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
                    "CAVEMAN and PONYTAIL mode active.\n"
                    "- Reply terse. Keep every path, command, symbol, number, and negation exact.\n"
                    "- Ship the shortest working diff. Reuse the repo, then stdlib, then native platform features.\n"
                    "- Never add filler, pleasantries, hedging, or unrequested abstractions."
                )
            }
        ]
    }
    json.dump(out, sys.stdout)

if __name__ == "__main__":
    main()
