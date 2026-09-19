#!/usr/bin/env python3
"""Reads an Antigravity transcript: what a conversation edited, verified and reviewed.

One parser for the hooks and for scripts/agy-audit.py.
"""
import glob
import json
import os
import re

# what a run-style call executed, matched against the things that decide whether work passed
VERIFIERS = (
    ("verify.cmd", re.compile(r"(?i)verify\.cmd")),
    ("verify.ps1", re.compile(r"(?i)verify\.ps1")),
    ("verify.sh", re.compile(r"(?i)verify\.sh")),
    ("audit_project.py", re.compile(r"(?i)audit_project\.py")),
    ("renpy lint", re.compile(r"(?i)renpy(\.exe)?[^\n]{0,40}lint|renpy_run_lint")),
    ("pytest", re.compile(r"(?i)\bpytest\b")),
    ("npm test", re.compile(r"(?i)\bnpm\s+(?:run\s+)?(?:lint|test)\b")),
    ("cargo test", re.compile(r"(?i)\bcargo\s+test\b")),
    ("go test", re.compile(r"(?i)\bgo\s+(test|vet)\b")),
    ("dotnet test", re.compile(r"(?i)\bdotnet\s+test\b")),
    ("simulate_birth.py", re.compile(r"(?i)simulate_birth\.py")),
    ("test_*.py", re.compile(r"(?i)\btest_[a-z0-9_]+\.py")),
)
EDIT_TOOLS = {"replace_file_content", "write_to_file", "multi_replace_file_content",
              "sed_file", "edit_file", "create_file"}
# only these actually execute something, so only these can be a verifier run
RUN_TOOLS = {"run_command", "call_mcp_tool"}
# the agent's own answers to the gate, not work the gate should judge
SCRATCH_FILES = {".agents/audit.json", ".agents/DECISIONS.md"}
PATH_KEYS = ("TargetFile", "AbsolutePath")
RESULT_TYPE = "GENERIC"
SPAWN_TOOL = "invoke_subagent"
REVIEWER = re.compile(r"(?i)review")
FULL_TRANSCRIPT = "transcript_full.jsonl"
GENERATED_DIR = ".system_generated"
SUBAGENT_RECORDS = os.path.join("*", GENERATED_DIR, "subagents")

EXIT_CODE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
# run_command reports its status as prose at the head of the result, not as JSON;
# anchored so the same sentence quoted inside a file being read is not a failure
COMMAND_EXIT = re.compile(r"(?i)^\s*the command exited with code (-?\d+)")
SUCCESS_FLAG = re.compile(r'"success"\s*:\s*(true|false)')
TOOL_HEADER = re.compile(r"^Created At: \S+\nCompleted At: \S+\n?")


def norm(path):
    """Case-folded, forward-slash form for comparing two paths on either platform."""
    return os.path.normcase(path).replace("\\", "/")


def read_transcript(path):
    """Parse one transcript JSONL. Returns (steps, bad_line_count)."""
    steps, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                steps.append(json.loads(line))
            except ValueError:
                bad += 1
    steps.sort(key=lambda s: s.get("step_index", 0))
    return steps, bad


def full_path(path):
    """The untruncated transcript beside the one the hook payload names."""
    sibling = os.path.join(os.path.dirname(path), FULL_TRANSCRIPT)
    return sibling if os.path.isfile(sibling) else path


def load(path):
    """Steps of a transcript, or None when there is none to read, so git can decide instead."""
    if not path:
        return None
    try:
        steps, _bad = read_transcript(full_path(os.path.expanduser(path)))
    except OSError:
        return None
    return steps or None


def brain_root(path):
    """brain/ above a transcript at brain/<cid>/.system_generated/logs/transcript.jsonl."""
    logs = os.path.dirname(path or "")
    generated = os.path.dirname(logs)
    if os.path.basename(logs) != "logs" or os.path.basename(generated) != GENERATED_DIR:
        return ""
    return os.path.dirname(os.path.dirname(generated))


def subagent_type(cid, root):
    """Type name when this conversation is some parent's subagent, else None.

    The record's state field stays ALIVE after the subagent finishes, so only its existence counts.
    """
    if not cid or not root:
        return None
    for path in glob.glob(os.path.join(root, SUBAGENT_RECORDS, f"{cid}.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        return str((data.get("subagentDescriptor") or {}).get("typeName") or "") or "subagent"
    return None


def unwrap(value):
    """Transcript tool args are JSON-encoded strings: '"C:\\\\a\\\\b.rpy"' -> 'C:\\a\\b.rpy'."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value if isinstance(value, str) and value else None


def strip_header(content):
    """A tool result without its Created At/Completed At header."""
    content = content or ""
    match = TOOL_HEADER.match(content)
    return content[match.end():] if match else content


def result_failed(body):
    """Did a tool result say it failed? Only self-reported signals count."""
    head = body[:4000]
    exited = COMMAND_EXIT.match(body)
    if exited and int(exited.group(1)) != 0:
        return f"exit code {exited.group(1)}"
    code = EXIT_CODE.search(head)
    if code and int(code.group(1)) != 0:
        return f"exit_code {code.group(1)}"
    flag = SUCCESS_FLAG.search(head)
    if flag and flag.group(1) == "false":
        return "success false"
    return ""


def executed_text(name, args):
    """What a run-style tool actually executed, or '' for tools that run nothing."""
    if name == "run_command":
        return str(args.get("CommandLine") or "")
    if name == "call_mcp_tool":
        return f"{args.get('ServerName') or ''} {args.get('ToolName') or ''}"
    return ""


def verifier_label(name, args):
    """Which verifier a tool call ran, or '' when it ran none."""
    ran = executed_text(name, args) if name in RUN_TOOLS else ""
    return next((label for label, pattern in VERIFIERS if pattern.search(ran)), "") if ran else ""


def targets(args):
    """Absolute forward-slash paths a file tool wrote."""
    out = []
    for key in PATH_KEYS:
        path = unwrap(args.get(key))
        if path:
            out.append(os.path.abspath(path).replace("\\", "/"))
    return out


def edits(steps, workspace, outside=()):
    """{workspace-relative path: last step index that wrote it}, artifact writes left out."""
    root = norm(os.path.abspath(workspace)).rstrip("/") + "/"
    blocked = tuple(norm(os.path.abspath(p)).rstrip("/") + "/" for p in outside if p)
    out = {}
    for step in steps or ():
        index = step.get("step_index", 0)
        for call in step.get("tool_calls") or []:
            if (call.get("name") or "") not in EDIT_TOOLS:
                continue
            for path in targets(call.get("args") or {}):
                low = norm(path)
                if not low.startswith(root) or any(low.startswith(b) for b in blocked):
                    continue
                rel = path[len(root):]
                if rel not in SCRATCH_FILES:
                    out[rel] = index
    return out


def verifier_runs(steps):
    """[(step index, whether it passed)] for every verifier this conversation ran.

    Each tool call is answered by the next result step, so the calls queue in order.
    """
    out, pending = [], []
    for step in steps or ():
        if step.get("type") == RESULT_TYPE and pending:
            index, label = pending.pop(0)
            if label:
                body = strip_header(step.get("content"))
                out.append((index, not result_failed(body) and step.get("status") != "ERROR"))
        for call in step.get("tool_calls") or []:
            pending.append((step.get("step_index", 0),
                            verifier_label(call.get("name") or "", call.get("args") or {})))
    return out


def reviewer_spawns(steps):
    """Step indexes where this conversation spawned a reviewer subagent."""
    out = []
    for step in steps or ():
        for call in step.get("tool_calls") or []:
            if (call.get("name") or "") != SPAWN_TOOL:
                continue
            specs = (call.get("args") or {}).get("Subagents")
            for spec in specs if isinstance(specs, list) else ():
                if isinstance(spec, dict) and REVIEWER.search(
                        f"{spec.get('TypeName') or ''} {spec.get('Role') or ''}"):
                    out.append(step.get("step_index", 0))
    return out
