#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: injects the working directives and pending verification."""
import json
import sys

import hookpaths


BANNER = (
    "CAVEMAN and PONYTAIL mode active.\n"
    "- Reply terse. Keep every path, command, symbol, number, and negation exact.\n"
    "- Ship the shortest working diff. Reuse the repo, then stdlib, then native platform features.\n"
    "- Never add filler, pleasantries, hedging, or unrequested abstractions."
)

PENDING_NOTE = (
    "Work in {ws} is still unverified from an earlier run:\n{notes}\n"
    "Deal with it before you finish."
)


def pending_note(ev):
    """One line per workspace whose last run left a gap, said once per conversation.

    Only the stop gate clears a marker, because only it can read the evidence.
    """
    cid = ev.get("conversationId") or ""
    notes = []
    for ws in ev.get("workspacePaths") or []:
        marker = hookpaths.read_pending(ws)
        if not marker or cid in (marker.get("said") or []):
            continue
        hookpaths.mark_said(ws, marker, cid)
        lines = "\n".join(f"- {n}" for n in marker.get("notes") or [])
        notes.append(PENDING_NOTE.format(ws=ws, notes=lines))
    return "\n".join(notes)


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
    try:
        note = pending_note(ev)
    except Exception:
        # a broken marker must not stop the agent working
        note = ""
    if note:
        steps.append({"ephemeralMessage": note})
    json.dump({"injectSteps": steps}, sys.stdout)


if __name__ == "__main__":
    main()
