#!/usr/bin/env python3
"""Paths and helpers shared by the Antigravity hooks."""
import json
import os
import re
import sys
import time

TMP = os.environ.get("GEMINI_HOOK_TMP") or os.path.expanduser("~/.gemini/tmp")
CLI_SETTINGS = (os.environ.get("GEMINI_CLI_SETTINGS")
                or os.path.expanduser("~/.gemini/antigravity-cli/settings.json"))
DOCS_LINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "ai-docs-lint.py")

# work a run left unverified: one marker per workspace, read on the next invocation
PENDING = os.path.join(TMP, "pending")
# forced continues already spent, one file per conversation
RETRIES = os.path.join(TMP, "retries")
MARKER_MAX_AGE = 86400
SLUG = re.compile(r"[^A-Za-z0-9]+")
IS_WINDOWS = os.name == "nt"
MSYS_DRIVE = re.compile(r"^/([a-zA-Z])(/.*)?$")

INSTRUCTION_DOCS = {"GEMINI.md", "AGENTS.md", "SKILL.md"}
# directories of instruction docs, counted at the repo root or under .agents/ only,
# so src/agents/x.py is source code
INSTRUCTION_DIRS = {"agents", "skills", "rules"}
# leftovers the write gate refuses to create and the stop gate refuses to leave behind
BACKUP_SUFFIX = re.compile(r"\.(?:bak|orig|old|tmp)$", re.I)
BACKUP_NAME = re.compile(BACKUP_SUFFIX.pattern
                         + r"|-v2\b|_backup\b|[ _-]copy(?:\s*\(\d+\))?(?:\.|$)", re.I)


def is_instruction_doc(path):
    """True for a Markdown file the docs linter owns: a named doc, or one in an instruction dir."""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if not parts or not parts[-1].lower().endswith(".md"):
        return False
    if parts[-1] in INSTRUCTION_DOCS:
        return True
    head = parts[1:] if parts[0] == ".agents" else parts
    return bool(head[:-1]) and head[0] in INSTRUCTION_DIRS


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


def real_path(path):
    """A path agy sends turned into one this Python can open: ~, MSYS drive, absolute form."""
    if not path:
        return ""
    path = os.path.expanduser(path)
    if IS_WINDOWS and path.startswith("/"):
        m = MSYS_DRIVE.match(path)
        if m:
            return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json_file(path, data):
    """Write through a temp file, so a hook killed mid-write leaves the old state readable."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(temp, path)


def stale(data):
    """True when a state file's timestamp is missing, unreadable, or more than a day old."""
    try:
        return time.time() - float(data.get("at") or 0) > MARKER_MAX_AGE
    except (TypeError, ValueError):
        return True


def slug_of(value):
    return SLUG.sub("-", os.path.normcase(value)).strip("-")[-120:] or "none"


def ws_key(workspace):
    """One state-file name per workspace, the same in every hook and every state directory."""
    return slug_of(os.path.abspath(real_path(workspace)))


def pending_path(workspace):
    return os.path.join(PENDING, ws_key(workspace) + ".json")


def read_pending(workspace):
    """The marker for this workspace, or None. A marker older than a day is stale and removed."""
    data = read_json_file(pending_path(workspace))
    if not isinstance(data, dict):
        return None
    if stale(data):
        clear_pending(workspace)
        return None
    return data


def write_pending(workspace, notes):
    """Record what this workspace still owes, keeping whoever has already been told."""
    old = read_pending(workspace) or {}
    write_json_file(pending_path(workspace),
                    {"at": time.time(), "workspace": workspace,
                     "notes": sorted(set(old.get("notes") or []) | set(notes)),
                     "said": list(old.get("said") or [])})


def mark_said(workspace, cid):
    """Note that this conversation has been told. Re-reads first: the gate may have written since."""
    marker = read_pending(workspace)
    if not marker:
        return
    marker["said"] = sorted(set(marker.get("said") or []) | {cid})
    write_json_file(pending_path(workspace), marker)


def clear_pending(workspace):
    try:
        os.remove(pending_path(workspace))
    except OSError:
        pass


def take_retry(conversation, kind, limit=1):
    """Spend one forced continue for this (conversation, gap kind, fingerprint). False when spent.

    An unreadable file counts as spent: a conversation that corrupts its own retry state must
    not win an extra continue, and must not crash the gate into releasing the stop either.
    """
    if not conversation:
        return False
    path = os.path.join(RETRIES, slug_of(conversation) + ".json")
    data = read_json_file(path)
    if data is None and os.path.exists(path):
        return False
    if not isinstance(data, dict) or stale(data):
        data = {}
    try:
        used = int(data.get(kind) or 0)
    except (TypeError, ValueError):
        return False
    if used >= limit:
        return False
    data[kind] = used + 1
    data["at"] = time.time()
    write_json_file(path, data)
    return True


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
