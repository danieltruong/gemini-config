#!/usr/bin/env python3
"""Reads an Antigravity transcript: what a conversation changed, verified and reviewed.

One parser for the hooks and for scripts/agy-audit.py.
"""
import glob
import json
import os
import re

import hookpaths

EDIT_TOOLS = {"replace_file_content", "write_to_file", "multi_replace_file_content",
              "sed_file", "edit_file", "create_file"}
# only these actually execute something, so only these can be a verifier run
RUN_TOOLS = {"run_command", "call_mcp_tool"}
SPAWN_TOOL = "invoke_subagent"
# delegation that judges work instead of changing it
JUDGE_TYPES = {"reviewer", "visual-qa"}
# the agent's own answer to the gate, not work the gate should judge
SCRATCH_FILES = {".agents/DECISIONS.md"}
PATH_KEYS = ("TargetFile", "AbsolutePath")
# every tool result is a step of this type; the other types are messages and checkpoints
RESULT_TYPE = "GENERIC"
FULL_TRANSCRIPT = "transcript_full.jsonl"
GENERATED_DIR = ".system_generated"
SUBAGENT_RECORDS = os.path.join("*", GENERATED_DIR, "subagents")

# the label stop_gate.verifier_steps picked for a workspace -> what it looks like once run
VERIFIER_COMMANDS = (
    (".agents/verify", re.compile(r"(?i)verify\.(?:cmd|ps1|sh)\b")),
    ("renpy lint", re.compile(r"(?i)renpy(?:\.exe)?[^\n]{0,60}\blint\b|renpy_run_lint")),
    ("npm", re.compile(r"(?i)\bnpm\s+(?:run\s+)?(?:lint|test)\b")),
    ("python -m pytest", re.compile(r"(?i)\bpytest\b")),
    ("cargo test", re.compile(r"(?i)\bcargo\s+test\b")),
    ("go ", re.compile(r"(?i)\bgo\s+(?:vet|test)\b")),
    ("dotnet test", re.compile(r"(?i)\bdotnet\s+test\b")),
)
# a command that only prints or searches for the verifier has not run it
QUOTING = re.compile(r"(?i)^\s*(?:echo|printf|cat|type|less|more|head|tail|grep|rg|findstr"
                     r"|git\s+(?:log|grep|show|diff))\b")
INSPECT_ONLY = re.compile(r"(?i)--collect-only|--co\b|--help\b|--version\b|--dry-run\b|\s-h\b")
# writes whose target is not a path the command line spells out, so they always count
WRITE_COMMAND = re.compile(
    r"(?i)\bsed\s+-i\b|\bgit\s+(?:apply|checkout|restore|stash\s+pop)\b"
    r"|\bpython\d?(?:\.exe)?\s+-c\b[^\n]*open\([^\n]*['\"][wax]")
# writes that name where they go: the target decides whether the tree really changed
TARGETED_WRITE = re.compile(
    r"(?i)(?:^|\s)\d?>>?\s*(?!&)[^\s&]|\|\s*tee\b"
    r"|\bSet-Content\b|\bAdd-Content\b|\bOut-File\b|\bCopy-Item\b|\bMove-Item\b")
QUOTED = r"(?:\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)"
WRITE_TARGETS = (
    re.compile(r"(?:^|\s)\d?>>?\s*(?!&)(" + QUOTED + ")"),
    re.compile(r"(?i)\|\s*tee\s+(?:-a\s+)?(" + QUOTED + ")"),
    re.compile(r"(?i)\b(?:Set-Content|Add-Content|Out-File)\b"
               r"(?:\s+-(?:Encoding|Force|Append|NoNewline|NoClobber)(?:\s+\S+)?)*"
               r"\s+(?:-(?:FilePath|Path|LiteralPath)\s+)?(" + QUOTED + ")"),
    re.compile(r"(?i)\b(?:Copy-Item|Move-Item)\b(?:\s+-\w+)*\s+" + QUOTED
               + r"\s+(?:-(?:Destination|LiteralPath)\s+)?(" + QUOTED + ")"),
)
# a target built from a variable or a substitution cannot be resolved, so it stays a change
UNRESOLVABLE = re.compile(r"[$%`]|\(\)")
DISCARDED = ("/dev/null", "nul", "$null")

EXIT_CODE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
# run_command reports its status as prose at the head of the result, not as JSON;
# anchored so the same sentence quoted inside a file being read is not a failure
COMMAND_EXIT = re.compile(r"(?i)^\s*the command exited with code (-?\d+)")
SUCCESS_FLAG = re.compile(r'"success"\s*:\s*(true|false)')
TOOL_HEADER = re.compile(r"^Created At: \S+\nCompleted At: \S+\n?")


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


def is_subagent(cid, root):
    """Was this conversation spawned by another one?

    The record's state field stays ALIVE after the subagent finishes, so only existence counts.
    """
    if not cid or not root:
        return False
    return bool(glob.glob(os.path.join(root, SUBAGENT_RECORDS, f"{cid}.json")))


def unwrap(value):
    """Tool args arrive plain or JSON-encoded: '"C:\\\\a\\\\b.rpy"' -> 'C:\\a\\b.rpy'."""
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


def calls(steps):
    """(step index, tool name, args, result step or None) for every call in the transcript.

    agy leaves gaps in step_index, so calls pair with results by position: the k-th call
    of a step takes the k-th result step that follows it, before the next step that calls
    anything. Injected messages and checkpoints sit in between, and a call whose result
    never arrived gets None rather than the next call's answer.
    """
    out = []
    steps = steps or []
    callers = [i for i, step in enumerate(steps) if step.get("tool_calls")]
    for n, i in enumerate(callers):
        end = callers[n + 1] if n + 1 < len(callers) else len(steps)
        answers = [steps[j] for j in range(i + 1, end)
                   if steps[j].get("type") == RESULT_TYPE]
        for k, call in enumerate(steps[i].get("tool_calls") or []):
            out.append((steps[i].get("step_index", i), call.get("name") or "",
                        call.get("args") or {}, answers[k] if k < len(answers) else None))
    return out


def result_ok(name, result):
    """True only when the result says the call succeeded; unknown counts as not succeeded."""
    if not isinstance(result, dict) or result.get("status") == "ERROR":
        return False
    body = strip_header(result.get("content"))
    if result_failed(body):
        return False
    if name == "run_command":
        # a backgrounded or truncated run never reports its code, so it proves nothing
        exited = COMMAND_EXIT.match(body)
        return bool(exited) and int(exited.group(1)) == 0
    return True


def targets(args):
    """Absolute forward-slash paths a file tool wrote."""
    out = []
    for key in PATH_KEYS:
        path = unwrap(args.get(key))
        if path:
            out.append(os.path.abspath(path).replace("\\", "/"))
    return out


def edits(calls_made, workspace, outside=()):
    """{workspace-relative path: (first step index, last step index)} for one workspace."""
    root = norm(os.path.abspath(workspace)).rstrip("/") + "/"
    out = {}
    for index, name, args, _result in calls_made or ():
        if name not in EDIT_TOOLS:
            continue
        for path in targets(args):
            if not norm(path).startswith(root) or any(under(path, p) for p in outside if p):
                continue
            rel = path[len(root):]
            if rel in SCRATCH_FILES:
                continue
            first, last = out.get(rel, (index, index))
            out[rel] = (min(first, index), max(last, index))
    return out


def spec_list(args):
    """The subagent specs an invoke_subagent call carried."""
    specs = args.get("Subagents")
    return [s for s in specs if isinstance(s, dict)] if isinstance(specs, list) else []


def write_targets(text):
    """Paths a targeted write names, as written on the command line."""
    out = []
    for pattern in WRITE_TARGETS:
        for match in pattern.finditer(text):
            out.append(match.group(1).strip("\"'"))
    return out


def resolve(token, cwd):
    """The absolute path a target token means, or None when the line does not say."""
    if not token or UNRESOLVABLE.search(token):
        return None
    path = hookpaths.real_path(token)
    if not os.path.isabs(path) and ":" not in path[:2]:
        if not cwd:
            return None
        path = os.path.join(hookpaths.real_path(cwd), path)
    return os.path.abspath(path)


def wrote_in_tree(text, cwd, spaces, artifact="", ignored=None):
    """Did this command change a file the gate has to account for?

    A write whose target lands outside every workspace, in the conversation's artifact
    directory, or on a path git ignores leaves the tree the reviewer saw intact. A target
    the line does not spell out stays a change, because nothing rules it out.
    """
    if WRITE_COMMAND.search(text):
        return True
    if not TARGETED_WRITE.search(text):
        return False
    targets = write_targets(text)
    if not targets:
        return True
    for token in targets:
        if token.lower() in DISCARDED:
            continue
        path = resolve(token, cwd)
        if path is None:
            return True
        if not any(under(path, ws) for ws in spaces or ()):
            continue
        if artifact and under(path, artifact):
            continue
        if ignored and ignored(path):
            continue
        return True
    return False


def opaque_changes(calls_made, spaces=(), artifact="", ignored=None):
    """Step indexes of changes the transcript cannot attribute to a file.

    Delegated work and shell writes change the tree without an edit tool, so the gate
    can see that something changed but not what.
    """
    out = []
    for index, name, args, _result in calls_made or ():
        if name == SPAWN_TOOL:
            if any(str(spec.get("TypeName") or "").strip() not in JUDGE_TYPES
                   for spec in spec_list(args)):
                out.append(index)
        elif name == "run_command":
            cwd = unwrap(args.get("Cwd"))
            if spaces and not any(under(cwd, ws) for ws in spaces):
                continue
            if wrote_in_tree(str(args.get("CommandLine") or ""), cwd, spaces, artifact, ignored):
                out.append(index)
    return out


def verifier_patterns(labels):
    """(label, pattern) for the verifiers a workspace picked; labels None means any known one."""
    if labels is None:
        return VERIFIER_COMMANDS
    out = []
    for label in labels:
        found = next((p for prefix, p in VERIFIER_COMMANDS if label.startswith(prefix)), None)
        if found:
            out.append((label, found))
    return out


def ran_in(name, args, workspace):
    """Did this call run against the given workspace?"""
    if not workspace:
        return True
    if name == "run_command":
        return under(unwrap(args.get("Cwd")), workspace)
    # an MCP verifier names the project it checks in one of its arguments
    return any(under(unwrap(value), workspace) for value in args.values()
               if isinstance(value, str))


def verifier_of(name, args, labels, workspace=""):
    """The verifier label this call ran, or '' when it ran none for this workspace."""
    if name not in RUN_TOOLS:
        return ""
    text = executed_text(name, args)
    if not text or QUOTING.match(text) or INSPECT_ONLY.search(text):
        return ""
    label = next((lab for lab, pattern in verifier_patterns(labels) if pattern.search(text)), "")
    return label if label and ran_in(name, args, workspace) else ""


def verifier_runs(calls_made, labels, workspace=""):
    """[(step index, whether it passed)] for every run of this workspace's own verifier."""
    out = []
    for index, name, args, result in calls_made or ():
        if verifier_of(name, args, labels, workspace):
            out.append((index, result_ok(name, result)))
    return out


def spawns(calls_made, type_name):
    """Step indexes where a subagent of exactly this type was spawned, briefed and answered."""
    out = []
    for index, name, args, result in calls_made or ():
        if name != SPAWN_TOOL or not result_ok(name, result):
            continue
        for spec in spec_list(args):
            if (str(spec.get("TypeName") or "").strip() == type_name
                    and str(spec.get("Prompt") or "").strip()):
                out.append(index)
                break
    return out
