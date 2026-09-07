#!/usr/bin/env python3
"""PreToolUse hook: git commits follow Conventional Commits and no gate gets skipped."""
import re

import hookpaths

SUBJECT_LIMIT = 50
CC = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
                r"(\([a-zA-Z0-9._/-]+\))?!?: .+")
MESSAGE = re.compile(r"-m\s+([\'\"])(.*?)\1", re.S)
HEREDOC_MESSAGE = re.compile(r"-m\s+[\"\']?\$\(cat\s+<<[\'\"]?(\w+)[\'\"]?\n(.*?)\n\1", re.S)
NO_VERIFY = re.compile(r"\bgit\s+(?:commit|push|merge)\b[^|;&]*\s--no-verify\b")
# -n means --no-verify for commit and merge; for push it is --dry-run, which is harmless
SHORT_NO_VERIFY = re.compile(r"\bgit\s+(?:commit|merge)\b[^|;&]*\s-n\b")
FORCE_PUSH = re.compile(r"\bgit\s+push\b[^|;&]*\s(?:--force(?:-with-lease)?(?:=\S+)?|-f)\b")
PROTECTED = {"main", "master"}


def subject(cmd):
    m = MESSAGE.search(cmd)
    if m:
        return m.group(2).strip().split("\n")[0]
    m = HEREDOC_MESSAGE.search(cmd)
    return m.group(2).strip().split("\n")[0] if m else None


def force_target(cmd):
    """The branch a force push names, or None when it names none."""
    tail = cmd.split("push", 1)[1].split("|")[0].split(";")[0].split("&")[0]
    refs = [t for t in tail.split() if not t.startswith("-")]
    return refs[-1].split(":")[-1] if len(refs) > 1 else None


def check(tool, args):
    if tool != "run_command":
        return None
    cmd = args.get("CommandLine", "")
    if NO_VERIFY.search(cmd) or SHORT_NO_VERIFY.search(cmd):
        return ("--no-verify skips the checks that keep the branch green. Fix what the hook "
                "reports, then commit again.")
    if FORCE_PUSH.search(cmd):
        branch = force_target(cmd)
        if branch is None or branch in PROTECTED:
            return (f"Force push to {branch or 'the current branch'} is not allowed. Name your "
                    "own branch, or rebase and push normally.")
    if "git commit" not in cmd:
        return None
    head = subject(cmd)
    if head is None:
        return None
    if not CC.match(head):
        return (f"Commit subject is not Conventional Commits: '{head}'. Invoke the "
                "caveman-commit skill to write a compliant message.")
    if len(head) > SUBJECT_LIMIT:
        return (f"Commit subject is {len(head)} chars (limit {SUBJECT_LIMIT}): '{head}'. "
                "Shorten it using caveman-commit format.")
    return None


if __name__ == "__main__":
    hookpaths.pre_tool_gate(check)
