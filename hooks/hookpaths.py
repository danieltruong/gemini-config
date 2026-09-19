#!/usr/bin/env python3
"""Paths and helpers shared by the Antigravity hooks."""
import json
import os
import re
import shutil
import subprocess
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


def real_path(path):
    """A path agy sends turned into one this Python can open: ~, MSYS drive, absolute form."""
    if not path:
        return ""
    path = os.path.expanduser(path)
    if IS_WINDOWS and path.startswith("/"):
        cygpath = shutil.which("cygpath")
        if cygpath:
            try:
                proc = subprocess.run([cygpath, "-w", path], capture_output=True, text=True,
                                      timeout=10)
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip().replace("\\", "/")
            except Exception:
                pass
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def slug_of(value):
    return SLUG.sub("-", os.path.normcase(value)).strip("-")[-120:] or "none"


def pending_path(workspace):
    """One marker per workspace, keyed on the same normalised path in every hook."""
    return os.path.join(PENDING, slug_of(os.path.abspath(real_path(workspace))) + ".json")


def read_pending(workspace):
    """The marker for this workspace, or None. A marker older than a day is stale and removed."""
    data = read_json_file(pending_path(workspace))
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("at") or 0) > MARKER_MAX_AGE:
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


def mark_said(workspace, marker, cid):
    marker["said"] = sorted(set(marker.get("said") or []) | {cid})
    write_json_file(pending_path(workspace), marker)


def clear_pending(workspace):
    try:
        os.remove(pending_path(workspace))
    except OSError:
        pass


def take_retry(conversation, kind, limit=1):
    """Spend one forced continue of this kind for this conversation. False when it is used up."""
    path = os.path.join(RETRIES, slug_of(conversation or "none") + ".json")
    data = read_json_file(path) or {}
    used = int(data.get(kind) or 0)
    if used >= limit:
        return False
    data[kind] = used + 1
    write_json_file(path, data)
    return True


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
