#!/usr/bin/env python3
"""PreToolUse hook: a command may not wait on the clock, only on an event."""
import re

import hookpaths

SLEEP = re.compile(r"(?<![\w-])sleep\s+[\d$]")
# a bounded wrapper turns a sleep into a capped wait, so it stays allowed
TIMEOUT_WRAP = re.compile(r"(?:^|[;&|]\s*)timeout\s+(?:-k\s+\S+\s+)?\d+\s+\S")
WIN_TIMEOUT = re.compile(r"\btimeout\s+/t\b", re.I)
PS_SLEEP = re.compile(r"\bStart-Sleep\b", re.I)
PING_DELAY = re.compile(r"\bping\s+(?:-n|-c)\s+\d+\s+(?:127\.0\.0\.1|localhost)", re.I)
POLL_LOOP = re.compile(r"\b(?:while|until)\b.*?\bsleep\b", re.S)
REASON = ("Clock wait. Use command_status with WaitDurationSeconds or the wait tool; a "
          "background command notifies you when it finishes. A sleep is only allowed "
          "inside a bounded `timeout <sec>` wrapper.")


def check(tool, args):
    if tool != "run_command":
        return None
    cmd = args.get("CommandLine", "")
    if WIN_TIMEOUT.search(cmd) or PS_SLEEP.search(cmd) or PING_DELAY.search(cmd):
        return REASON
    if POLL_LOOP.search(cmd):
        return REASON
    if SLEEP.search(cmd) and not TIMEOUT_WRAP.search(cmd):
        return REASON
    return None


if __name__ == "__main__":
    hookpaths.pre_tool_gate(check)
