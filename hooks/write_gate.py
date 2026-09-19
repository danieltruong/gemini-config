#!/usr/bin/env python3
"""PreToolUse hook: the gate's own files, the verifier, and tests are not the agent's to write.

Four rules, in this order: nothing writes under ~/.gemini or the gate's own code and scripts,
nothing writes a workspace's verifier definition, only a `tester` subagent writes test files,
and a run command naming a protected location is refused as a tripwire. Then the older rules:
no backup-copy files, and no loosening a lint, format or type config.
"""
import fnmatch
import os
import re

import hookpaths
import transcript

CONFIGS = (
    ".eslintrc*", "eslint.config.*",
    ".prettierrc*", "prettier.config.*",
    "tsconfig*.json", "biome.json*",
    "ruff.toml", ".ruff.toml", "mypy.ini", ".editorconfig", ".stylelintrc*",
)
TOOL_SECTION = re.compile(r"^\[tool\.(?:ruff|mypy)\b", re.M)
CONFIG_REASON = ("{name} is a lint, format or type config; config protected, fix the code, "
                 "not the linter. Daniel changes this file by hand if it really must change.")

OWNER_REASON = ("{target} is owner-only: it is the gate's own code, state or config. "
                "The gate cannot judge a run that writes what the gate reads. Daniel changes "
                "these by hand, outside agy. Report what needs changing and why instead.")
VERIFIER_REASON = ("{target} defines what checks this workspace, so a run cannot write it. "
                   "Daniel changes these by hand. Make the check pass instead, or report why "
                   "the check itself is wrong.")
TEST_REASON = ("{target} is a test file, and this conversation is {who}. Delegate the test "
               "change to the `tester` subagent: invoke_subagent with TypeName tester, naming "
               "the file and the behaviour it has to cover.")
TRIPWIRE_REASON = ("This command names {hit}, which is a protected location: the gate's own "
                   "code, state or config. Read-only inspection is fine, writing there is not. "
                   "Say what needs changing instead.")

# tests are only the tester's to write; a Ren'Py testcase file counts as one
TEST_NAMES = ("test_*.py", "*_test.py", "*.test.*", "*.spec.*", "conftest.py")
TEST_DIR = "tests"
RENPY_TEST_DIRS = {"tests", "testcases"}
TESTER = "tester"

# a shell line is never parsed for what it does; these literals say it is aiming at the gate
# itself, and the tripwire reads them as plain substrings
NEEDLES = (".gemini/tmp", ".gemini/config", "gemini-config/hooks", "stop_gate", "gitstate",
           ".system_generated")
SEGMENT = re.compile(r"&&|\|\||[;|\n]")
# any redirection can write, so a read-only command carrying one is not read-only
REDIRECT = re.compile(r">")
# the read-only inspection and verifier invocations settings.json already allows, so
# `cat docs/stop_gate_notes.md` and `.agents/verify.sh` pass the tripwire
READ_ONLY = re.compile(
    r"^(?:cd|cat|type|head|tail|less|more|ls|dir|wc|stat|file|grep|egrep|rg|findstr|diff"
    r"|git\s+(?:-C\s+\S+\s+)?(?:status|diff|log|show|blame|ls-files|rev-parse)"
    r"|pytest|python\s+-m\s+(?:pytest|unittest)"
    r"|npm\s+(?:test|run\s+(?:lint|test|build))"
    r"|(?:bash\s+|cmd\s+/c\s+)?\S*[\\/]?\.agents[\\/]verify\.(?:cmd|ps1|sh))\b", re.I)


def workspace_of(event):
    """The workspace a relative edit target belongs to: agy sends the active one first."""
    for raw in event.get("workspacePaths") or []:
        if raw:
            return hookpaths.real_path(raw)
    return ""


def owner_reason(full):
    """Rules 1 and 2: the gate's own files, and the definition of what checks a workspace."""
    if any(hookpaths.inside(full, root) for root in hookpaths.OWNER_ROOTS):
        return OWNER_REASON
    tail = full.rstrip("/")
    if any(tail.endswith("/" + rel) for rel in hookpaths.OWNER_RELS) \
            or any(("/" + d) in tail for d in hookpaths.OWNER_REL_DIRS):
        return VERIFIER_REASON
    return None


def is_test_path(full):
    parts = full.split("/")
    name, dirs = parts[-1], parts[:-1]
    if any(fnmatch.fnmatch(name, pat) for pat in TEST_NAMES):
        return True
    if TEST_DIR in dirs:
        return True
    return name.endswith(".rpy") and bool(RENPY_TEST_DIRS & set(dirs))


def who_is(event):
    """This conversation's subagent type, as its parent recorded it, or '' for the main agent."""
    tpath = hookpaths.real_path(event.get("transcriptPath") or "")
    return transcript.subagent_type(transcript.conversation_id(event),
                                    transcript.brain_root(tpath))


def read_only(line):
    """Is every segment of this command an inspection or verifier call that writes nothing?"""
    if REDIRECT.search(line):
        return False
    parts = [seg.strip() for seg in SEGMENT.split(line) if seg.strip()]
    return bool(parts) and all(READ_ONLY.match(seg) for seg in parts)


def tripwire(args):
    """Rule 4: a run command naming a protected location, unless it only reads."""
    line = args.get("CommandLine") or ""
    flat = hookpaths.norm(line)
    hit = next((needle for needle in NEEDLES if needle in flat), None)
    if not hit or read_only(line):
        return None
    return TRIPWIRE_REASON.format(hit=hit)


def check(tool, args, event):
    if os.environ.get(hookpaths.GATE_OFF):
        return None  # the owner's own edit of a protected file
    if tool in transcript.RUN_TOOLS:
        return tripwire(args)
    if tool not in transcript.EDIT_TOOLS:
        return None
    path = args.get("TargetFile") or args.get("AbsolutePath") or ""
    full = hookpaths.resolved(path, workspace_of(event))
    artifacts = hookpaths.real_path(event.get("artifactDirectoryPath") or "")
    if not hookpaths.inside(full, artifacts):
        reason = owner_reason(full)
        if reason:
            return reason.format(target=path)
        who = who_is(event)
        if is_test_path(full) and who != TESTER:
            return TEST_REASON.format(target=path,
                                      who=f"the `{who}` subagent" if who else "the main agent")
    name = os.path.basename(path.replace("\\", "/"))
    if hookpaths.BACKUP_NAME.search(name):
        return (f"{name} is a backup or copy file. Git holds history: edit the original, "
                "delete the copy.")
    if name == "pyproject.toml":
        return CONFIG_REASON.format(name="[tool.ruff]/[tool.mypy] in pyproject.toml") \
            if TOOL_SECTION.search(hookpaths.tool_text(args)) else None
    if not any(fnmatch.fnmatch(name, pat) for pat in CONFIGS):
        return None
    # writing the first config a repo has is fine; only loosening an existing one is not
    return CONFIG_REASON.format(name=name) if os.path.isfile(path) else None


if __name__ == "__main__":
    hookpaths.pre_tool_gate(check)
