#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: Injects Caveman & Ponytail directives."""
import json
import sys


BANNER = (
    "CAVEMAN and PONYTAIL mode active.\n"
    "- Reply terse. Keep every path, command, symbol, number, and negation exact.\n"
    "- Ship the shortest working diff. Reuse the repo, then stdlib, then native platform features.\n"
    "- Never add filler, pleasantries, hedging, or unrequested abstractions."
)


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}

    try:
        num = int(ev.get("invocationNum", 1))
    except (TypeError, ValueError):
        num = 1

    # every turn would re-pay for a banner the model already read; every tenth keeps it in view
    steps = [{"ephemeralMessage": BANNER}] if num == 1 or num % 10 == 0 else []
    json.dump({"injectSteps": steps}, sys.stdout)


if __name__ == "__main__":
    main()
