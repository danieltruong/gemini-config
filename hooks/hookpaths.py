#!/usr/bin/env python3
"""Paths and helpers shared by the Antigravity hooks."""
import glob
import hashlib
import json
import os
import re
import sys
import time

GEMINI_HOME = os.environ.get("GEMINI_HOME") or os.path.expanduser("~/.gemini")
TMP = os.environ.get("GEMINI_HOOK_TMP") or os.path.join(GEMINI_HOME, "tmp")
CLI_SETTINGS = (os.environ.get("GEMINI_CLI_SETTINGS")
                or os.path.join(GEMINI_HOME, "antigravity-cli", "settings.json"))
# the repo the hooks really live in, so a write through the installed junction lands here too
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
DOCS_LINT = os.path.join(REPO, "scripts", "ai-docs-lint.py")
# only the owner edits these: the gate's own code and scripts, its state, and the CLI's config
OWNER_ROOTS = (GEMINI_HOME, os.path.join(REPO, "hooks"), os.path.join(REPO, "hooks.json"),
               os.path.join(REPO, "scripts"))
# a workspace's verifier definition, held as suffixes so any workspace is covered
OWNER_RELS = (".agents/verify.cmd", ".agents/verify.ps1", ".agents/verify.sh",
              ".agents/visual.md")
OWNER_REL_DIRS = (".agents/rules/",)
# the owner's escape hatch for editing a protected file from inside a run
GATE_OFF = "AGY_WRITE_GATE_OFF"
# files the stop gate trusts, so one that moves between baseline and stop is worth reporting
GUARD_GLOBS = ("hooks/*.py", "hooks.json")

# work a run left unverified: one marker per workspace, read on the next invocation
PENDING = os.path.join(TMP, "pending")
# forced continues already spent, one file per conversation
RETRIES = os.path.join(TMP, "retries")
# forced continues per gap kind and fingerprint, then per conversation whatever the gap
MAX_PER_GAP = 1
MAX_FORCED = 3
# ledger key for that total; every gap kind carries a ":" so neither can shadow the other
TOTAL_KEY = "total"
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
    """Run a PreToolUse check(tool, args, event) returning a deny reason, or None to allow."""
    try:
        event = json.load(sys.stdin) or {}
    except Exception as exc:
        event = {}
        print(f"{os.path.basename(sys.argv[0])}: unreadable payload {exc!r}", file=sys.stderr)
    call = event.get("toolCall") or {} if isinstance(event, dict) else {}
    try:
        reason = check(call.get("name") or "", call.get("args") or {}, event)
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


def norm(path):
    """Case-folded, forward-slash form for comparing two paths on either platform."""
    return os.path.normcase(path).replace("\\", "/")


def resolved(path, workspace=""):
    """An edit target as one absolute path: ~, MSYS drive, workspace-relative, junction, case."""
    full = real_path(path)
    if not full:
        return ""
    if not os.path.isabs(full) and workspace:
        full = os.path.join(real_path(workspace), full)
    try:
        return norm(os.path.realpath(os.path.abspath(full)))
    except (OSError, ValueError):
        return norm(os.path.abspath(full))


def inside(target, root):
    """Is an already resolved path inside this root? The root is resolved the same way."""
    if not target or not root:
        return False
    parent = resolved(root)
    if not parent:
        return False
    return target == parent or target.rstrip("/").startswith(parent.rstrip("/") + "/")


def guard_paths(workspace=""):
    """The files the stop gate trusts: its own code, plus this workspace's verifier definition."""
    out = []
    for pattern in GUARD_GLOBS:
        out.extend(sorted(glob.glob(os.path.join(REPO, pattern))))
    for rel in OWNER_RELS:
        full = os.path.join(workspace, rel.replace("/", os.sep)) if workspace else ""
        if full and os.path.isfile(full):
            out.append(full)
    return out


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
    """A readable file name for a path or id, with a hash so two long values cannot collide."""
    value = os.path.normcase(value)
    tag = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return f"{SLUG.sub('-', value).strip('-')[-110:] or 'none'}-{tag}"


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


def retry_path(ledger):
    return os.path.join(RETRIES, slug_of(ledger) + ".json")


def retry_ledger(ledger):
    """Forced continues already spent under this key. None when the file is unusable."""
    path = retry_path(ledger)
    data = read_json_file(path)
    if data is None and os.path.exists(path):
        return None
    return {} if not isinstance(data, dict) or stale(data) else data


def forced_count(ledger):
    """How many forced continues this ledger has spent in total."""
    data = retry_ledger(ledger) or {}
    try:
        return int(data.get(TOTAL_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def take_retry(ledger, kind):
    """Spend one forced continue for this (ledger, gap kind, fingerprint). False when spent.

    An unreadable file counts as spent: a conversation that corrupts its own retry state must
    not win an extra continue, and must not crash the gate into releasing the stop either.
    """
    if not ledger:
        return False
    data = retry_ledger(ledger)
    if data is None:
        return False
    try:
        used = int(data.get(kind) or 0)
        total = int(data.get(TOTAL_KEY) or 0)
    except (TypeError, ValueError):
        return False
    if used >= MAX_PER_GAP or total >= MAX_FORCED:
        return False
    data[kind] = used + 1
    data[TOTAL_KEY] = total + 1
    data["at"] = time.time()
    write_json_file(retry_path(ledger), data)
    return True


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
