#!/usr/bin/env python3
"""PreToolUse hook: Prevents infinite retry loops on repeatedly denied commands."""
import json
import os
import re
import sys
import time

LIMIT = int(os.environ.get("GEMINI_DENY_LIMIT", "2"))
STATE = os.path.expanduser("~/.gemini/tmp/denials")
REDIR = re.compile(r"\s*(?:\d?>>?&?\s*[\w/.\-]+|\d?>>?\s*&?\d)\s*$")
SPLIT = re.compile(r"&&|\|\||[;|\n]")

def fingerprints(tool, args):
    if tool == "run_command":
        cmd = args.get("CommandLine", "")
        out = []
        for seg in SPLIT.split(cmd):
            seg = " ".join(seg.split())
            while True:
                stripped = REDIR.sub("", seg).strip()
                if stripped == seg:
                    break
                seg = stripped
            seg = re.sub(r"^cd\s+\S+\s+", "", seg)
            if seg:
                out.append(f"run_command|{seg}")
        return out or [f"run_command|{cmd}"]
    for k in ("TargetFile", "AbsolutePath", "DirectoryPath", "SearchPath"):
        if args.get(k):
            return [f"{tool}|{args[k]}"]
    return [f"{tool}|{json.dumps(args, sort_keys=True)[:200]}"]

def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return

    tool_call = ev.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    args = tool_call.get("args", {})
    cid = ev.get("conversationId", "unknown")

    path = os.path.join(STATE, f"{cid}.json")
    keys = fingerprints(tool_name, args)

    try:
        counts = json.load(open(path, encoding="utf-8"))
    except Exception:
        counts = {}

    hit = next((k for k in keys if counts.get(k, 0) >= LIMIT), None)
    if hit is not None:
        cmd_repr = hit.split("|", 1)[1][:150]
        json.dump({
            "decision": "deny",
            "reason": (
                f"Circuit breaker: `{cmd_repr}` was denied {counts[hit]} times this session. "
                "Do NOT retry it or reword it. Proceed with another approach or inform the user."
            )
        }, sys.stdout)
    else:
        # Default allow / no-op
        json.dump({}, sys.stdout)

if __name__ == "__main__":
    main()
