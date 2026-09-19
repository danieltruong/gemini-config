#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: injects the working directives and pending verification."""
import json
import os
import sys

import gitstate
import hookpaths
import transcript


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


def note_start(cid, spaces):
    """Fingerprint each workspace before the turn runs.

    This is the baseline the stop gate compares against: what a reviewer saw when it started,
    and which untracked files were already there before anyone wrote anything.
    """
    for ws in spaces:
        if not gitstate.seen(cid, ws):  # only the first turn pays for the git calls
            gitstate.note_seen(cid, ws, gitstate.fingerprint(ws))


def pending_note(cid, spaces):
    """One line per workspace whose last run left a gap, said once per conversation.

    Only the stop gate clears a marker, because only it can prove the work was checked.
    """
    notes = []
    for ws in spaces:
        marker = hookpaths.read_pending(ws)
        if not marker or cid in (marker.get("said") or []):
            continue
        hookpaths.mark_said(ws, cid)
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
    cid = transcript.conversation_id(ev)
    spaces = [os.path.abspath(hookpaths.real_path(w)) for w in ev.get("workspacePaths") or []]
    note = ""
    try:
        note_start(cid, spaces)
        note = pending_note(cid, spaces)
    except Exception:
        # a broken marker or an unreadable repo must not stop the agent working
        pass
    if note:
        steps.append({"ephemeralMessage": note})
    json.dump({"injectSteps": steps}, sys.stdout)


if __name__ == "__main__":
    main()
