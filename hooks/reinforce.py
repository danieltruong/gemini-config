#!/usr/bin/env python3
"""PreInvocation hook for Antigravity: injects the working directives and pending verification."""
import json
import sys

import hookpaths
import transcript


BANNER = (
    "CAVEMAN and PONYTAIL mode active.\n"
    "- Reply terse. Keep every path, command, symbol, number, and negation exact.\n"
    "- Ship the shortest working diff. Reuse the repo, then stdlib, then native platform features.\n"
    "- Never add filler, pleasantries, hedging, or unrequested abstractions."
)

PENDING_NOTE = (
    "Verification and review are still pending in {ws}: the run before this one ended in an "
    "error after editing at step {step}. Run the verifier and a reviewer before you finish."
)


def pending_note(ev):
    """One line about edits a crashed run left unverified, said once per workspace."""
    steps, note = None, ""
    for ws in ev.get("workspacePaths") or []:
        marker = hookpaths.read_pending(ws)
        if not marker:
            continue
        if steps is None:
            steps = transcript.load(ev.get("transcriptPath")) or []
        if any(ok for _index, ok in transcript.verifier_runs(steps)):
            hookpaths.clear_pending(ws)
            continue
        if marker.get("said"):
            continue
        marker["said"] = True
        hookpaths.write_pending(ws, marker)
        note = PENDING_NOTE.format(ws=ws, step=marker.get("step"))
    return note


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
