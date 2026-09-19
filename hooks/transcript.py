#!/usr/bin/env python3
"""Reads an Antigravity transcript for the two things git cannot tell the gate.

Which workspaces a conversation touched, and whether a conversation is a subagent and of
what type. Everything else the gate judges by looking at the repository instead.

One parser for the hooks and for scripts/agy-audit.py.
"""
import glob
import json
import os

import hookpaths

EDIT_TOOLS = {"replace_file_content", "write_to_file", "multi_replace_file_content",
              "sed_file", "edit_file", "create_file"}
RUN_TOOLS = {"run_command", "call_mcp_tool"}
SPAWN_TOOL = "invoke_subagent"
# a call that could have changed a tree, whatever it was actually spelled to do
WORK_TOOLS = EDIT_TOOLS | RUN_TOOLS | {SPAWN_TOOL}
# a subagent record with no readable type still marks a subagent, which owes no review
UNKNOWN_TYPE = "subagent"
# delegation that judges work instead of changing it
JUDGE_TYPES = ("reviewer", "visual-qa")
# the agent's own answer to the gate, not work the gate should judge
SCRATCH_FILES = {".agents/DECISIONS.md"}
PATH_KEYS = ("TargetFile", "AbsolutePath")
FULL_TRANSCRIPT = "transcript_full.jsonl"
GENERATED_DIR = ".system_generated"
SUBAGENT_RECORDS = os.path.join("*", GENERATED_DIR, "subagents")


def norm(path):
    """Case-folded, forward-slash form for comparing two paths on either platform."""
    return os.path.normcase(path).replace("\\", "/")


def under(path, root):
    """Is path inside root? Both are compared as absolute, case-folded paths."""
    if not path or not root:
        return False
    try:
        target = norm(os.path.abspath(path)).rstrip("/") + "/"
        parent = norm(os.path.abspath(root)).rstrip("/") + "/"
    except (OSError, ValueError):
        return False
    return target.startswith(parent)


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
    # agy appends a tool result before the step that called the tool, so file order is
    # not conversation order; step_index is, even though it has holes
    ordered = sorted(enumerate(steps), key=lambda pair: (pair[1].get("step_index", pair[0]),
                                                        pair[0]))
    return [step for _position, step in ordered], bad


def full_path(path):
    """The untruncated transcript beside the one the hook payload names."""
    sibling = os.path.join(os.path.dirname(path), FULL_TRANSCRIPT)
    return sibling if os.path.isfile(sibling) else path


def load(path):
    """Steps of a transcript, or None when it cannot be read or holds nothing usable."""
    if not path:
        return None
    try:
        steps, _bad = read_transcript(full_path(path))
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


def conversation_of(path):
    """The conversation id the transcript's own directory names, or ''.

    agy omits conversationId from most Stop payloads, and a shared fallback key would let
    one conversation spend another one's retry.
    """
    generated = os.path.dirname(os.path.dirname(path or ""))
    if os.path.basename(generated) != GENERATED_DIR:
        return ""
    return os.path.basename(os.path.dirname(generated))


def conversation_id(event):
    """The conversation this hook payload belongs to, the same way in every hook.

    agy omits conversationId from most Stop payloads, and a shared fallback key would let
    one conversation spend another one's retry, so the transcript's own directory answers.
    """
    return (event.get("conversationId")
            or conversation_of(hookpaths.real_path(event.get("transcriptPath") or "")))


def subagent_type(cid, root):
    """The typeName the parent recorded for this conversation, or '' when it is top-level.

    The record's state field stays ALIVE after the subagent finishes, so only existence counts.
    """
    if not cid or not root:
        return ""
    for path in glob.glob(os.path.join(root, SUBAGENT_RECORDS, f"{cid}.json")):
        data = hookpaths.read_json_file(path) or {}
        name = str((data.get("subagentDescriptor") or {}).get("typeName") or "").strip()
        return name or UNKNOWN_TYPE
    return ""


def unwrap(value):
    """Tool args arrive plain or JSON-encoded: '"C:\\\\a\\\\b.rpy"' -> 'C:\\a\\b.rpy'."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return value if isinstance(value, str) and value else None


def calls(steps):
    """(step index, tool name, args) for every tool call in the transcript."""
    out = []
    for i, step in enumerate(steps or []):
        for call in step.get("tool_calls") or []:
            out.append((step.get("step_index", i), call.get("name") or "", call.get("args") or {}))
    return out


def did_work(calls_made):
    """Did this conversation call anything that could have written to a tree?

    The cheap signal for a stop with no baseline of its own. The command text is never read:
    matching it is what the old transcript-reading gate did, and it was gamed.
    """
    return any(name in WORK_TOOLS for _index, name, _args in calls_made or ())


def run_dirs(calls_made, outside=()):
    """Directories a run tool worked in, artifact and brain directories left out.

    A shell write names no file, so its working directory is the only workspace it points at.
    """
    out = []
    for _index, name, args in calls_made or ():
        if name not in RUN_TOOLS:
            continue
        path = unwrap(args.get("Cwd"))
        if not path:
            continue
        full = os.path.abspath(hookpaths.real_path(path)).replace("\\", "/")
        if not any(under(full, root) for root in outside if root) and full not in out:
            out.append(full)
    return out


def targets(args):
    """Absolute forward-slash paths a file tool wrote."""
    out = []
    for key in PATH_KEYS:
        path = unwrap(args.get(key))
        if path:
            out.append(os.path.abspath(hookpaths.real_path(path)).replace("\\", "/"))
    return out


def written_paths(calls_made, outside=()):
    """Absolute paths every edit tool named, artifact and brain directories left out."""
    out = []
    for _index, name, args in calls_made or ():
        if name not in EDIT_TOOLS:
            continue
        for path in targets(args):
            if not any(under(path, p) for p in outside if p) and path not in out:
                out.append(path)
    return out


def edits(calls_made, workspace, outside=()):
    """Workspace-relative paths an edit tool named inside one workspace."""
    root = norm(os.path.abspath(workspace)).rstrip("/") + "/"
    out = set()
    for path in written_paths(calls_made, outside):
        if norm(path).startswith(root):
            rel = path[len(root):]
            if rel not in SCRATCH_FILES:
                out.add(rel)
    return out
