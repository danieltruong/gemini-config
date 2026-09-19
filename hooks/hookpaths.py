#!/usr/bin/env python3
"""Paths and helpers shared by the Antigravity hooks."""
import json
import os
import re
import sys

TMP = os.environ.get("GEMINI_HOOK_TMP") or os.path.expanduser("~/.gemini/tmp")
CLI_SETTINGS = (os.environ.get("GEMINI_CLI_SETTINGS")
                or os.path.expanduser("~/.gemini/antigravity-cli/settings.json"))
DOCS_LINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "ai-docs-lint.py")

# work a crashed run left unverified: one marker per workspace, read on the next invocation
PENDING = os.path.join(TMP, "pending")
SLUG = re.compile(r"[^A-Za-z0-9]+")

INSTRUCTION_DOCS = {"GEMINI.md", "AGENTS.md", "SKILL.md"}
# leftovers the write gate refuses to create and the stop gate refuses to leave behind
BACKUP_SUFFIX = re.compile(r"\.(?:bak|orig|old|tmp)$", re.I)
BACKUP_NAME = re.compile(BACKUP_SUFFIX.pattern
                         + r"|-v2\b|_backup\b|[ _-]copy(?:\s*\(\d+\))?(?:\.|$)", re.I)


def is_instruction_doc(path):
    """True for GEMINI.md, AGENTS.md, SKILL.md, or any file under a dir named agents."""
    parts = path.replace("\\", "/").split("/")
    return parts[-1] in INSTRUCTION_DOCS or "agents" in parts[:-1]


PATH_KEYS = ("TargetFile", "AbsolutePath", "DirectoryPath", "SearchPath", "FilePath")


def tool_text(args):
    """Every string an agy tool call would write, path arguments left out."""
    out = []

    def walk(node, key=""):
        if isinstance(node, str):
            if key not in PATH_KEYS:
                out.append(node)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key)

    walk(args)
    return "\n".join(out)


def pre_tool_gate(check):
    """Run a PreToolUse check(tool, args) that returns a deny reason, or None to allow."""
    try:
        call = (json.load(sys.stdin) or {}).get("toolCall") or {}
    except Exception:
        call = {}
    try:
        reason = check(call.get("name") or "", call.get("args") or {})
    except Exception as exc:
        # a broken gate must not stop the agent working, so it logs and allows
        reason = None
        print(f"{os.path.basename(sys.argv[0])}: {exc!r}", file=sys.stderr)
    json.dump({"decision": "deny", "reason": reason} if reason else {"decision": "allow"},
              sys.stdout)


def pending_path(workspace):
    slug = SLUG.sub("-", os.path.normcase(os.path.abspath(workspace))).strip("-")
    return os.path.join(PENDING, slug[-120:] + ".json")


def read_pending(workspace):
    try:
        with open(pending_path(workspace), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_pending(workspace, data):
    os.makedirs(PENDING, exist_ok=True)
    with open(pending_path(workspace), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def clear_pending(workspace):
    try:
        os.remove(pending_path(workspace))
    except OSError:
        pass


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
