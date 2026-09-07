#!/usr/bin/env python3
"""Stop hook: blocks finishing while lint errors sit in files the agent changed."""
import json
import os
import re
import subprocess
import sys
import time

import hookpaths

LOG = os.path.join(hookpaths.TMP, "stop_gate.log")
LINT_TIMEOUT = 120
MAX_EXECUTIONS = 3
MAX_REPORTED = 40
# agy 1.1.27 sends "NO_TOOL_CALL" here; "model_stop" is only in the docs. Every other
# reason (ERROR, USER_CANCELED, MAX_*) means the agent did not choose to finish.
MODEL_FINISHED = {"model_stop", "NO_TOOL_CALL"}
WRITE_TOOLS = {"replace_file_content", "write_to_file", "multi_replace_file_content", "sed_file"}
# lint report lines start with a project-relative path: "game/foo/bar.rpy:12 message"
LINT_LINE = re.compile(r"^(\S+\.rpym?):(\d+)\s")
IS_WINDOWS = os.name == "nt"
# on Windows renpy.sh is a Linux binary and always fails, so only renpy.exe counts there
SDK_EXE = "renpy.exe" if IS_WINDOWS else "renpy.sh"


def log(project, rc, errors, decision):
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        hookpaths.append(LOG, f"{stamp} {project} rc={rc} errors={errors} {decision}\n")
    except Exception:
        pass


def find_sdk(project):
    env = os.environ.get("RENPY_SDK_PATH")
    if env and os.path.isfile(os.path.join(env, SDK_EXE)):
        return env
    d = os.path.abspath(project)
    while True:
        if os.path.isfile(os.path.join(d, SDK_EXE)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def git(project, *args):
    return subprocess.run(
        ["git", *args], cwd=project, capture_output=True, timeout=30
    ).stdout.decode("utf-8", "replace")


def unwrap(v):
    """Transcript tool args are JSON-encoded strings: '"C:\\\\a\\\\b.rpy"' -> 'C:\\a\\b.rpy'."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            pass
    return v if isinstance(v, str) and v else None


def touched_files(transcript, project):
    """Project-relative paths written this session, or None when the transcript is unusable."""
    try:
        with open(os.path.expanduser(transcript), encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except Exception:
        return None
    root = os.path.abspath(project).replace("\\", "/").rstrip("/") + "/"
    out, parsed = set(), 0
    for line in lines:
        try:
            step = json.loads(line)
        except Exception:
            continue
        parsed += 1
        for call in step.get("tool_calls") or []:
            if call.get("name") not in WRITE_TOOLS:
                continue
            args = call.get("args") or {}
            for key in ("TargetFile", "AbsolutePath"):
                path = unwrap(args.get(key))
                if not path:
                    continue
                path = os.path.abspath(path).replace("\\", "/")
                if path.startswith(root):
                    out.add(path[len(root):])
    return out if parsed else None


def changed_files(project):
    """Project-relative paths git reports as modified or untracked, or None when not a git repo."""
    try:
        top = git(project, "rev-parse", "--show-toplevel").strip()
        if not top:
            return None
        rel = os.path.relpath(os.path.abspath(project), os.path.abspath(top)).replace("\\", "/")
        prefix = "" if rel == "." else rel + "/"
        names = git(project, "diff", "--name-only", "HEAD").splitlines()
        names += git(project, "ls-files", "--others", "--exclude-standard").splitlines()
        out = set()
        for n in names:
            n = n.strip().replace("\\", "/")
            if n and n.startswith(prefix):
                out.add(n[len(prefix):])
        return out
    except Exception:
        return None


def lint(project, transcript):
    """(rc, lint report lines for files this session wrote; git-dirty files when no transcript)."""
    sdk = find_sdk(project)
    if sdk is None:
        return None, []
    # ponytail: re-lints the whole project on every Stop (~4 s); cache on .rpy mtimes if that drags
    proc = subprocess.run(
        [os.path.join(sdk, SDK_EXE), project, "lint", "--error-code"],
        cwd=sdk, capture_output=True, timeout=LINT_TIMEOUT,
    )
    text = proc.stdout.decode("utf-8", "replace").lstrip("﻿")
    changed = touched_files(transcript, project) if transcript else None
    if changed is None:
        changed = changed_files(project)
    hits = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        m = LINT_LINE.match(line)
        if m and (changed is None or m.group(1) in changed):
            hits.append(line)
    return proc.returncode, hits


def docs_findings(project, transcript):
    """ai-docs-lint output for instruction docs this session wrote."""
    touched = touched_files(transcript, project) if transcript else None
    if not touched:
        return []
    paths = [os.path.join(project, rel) for rel in sorted(touched) if hookpaths.is_instruction_doc(rel)]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return []
    proc = subprocess.run(
        [sys.executable, hookpaths.DOCS_LINT, *paths], capture_output=True, text=True, timeout=30
    )
    return proc.stdout.splitlines() if proc.returncode else []


def main():
    stop = {"decision": "stop"}
    project, rc, hits, note = "-", "-", [], ""
    try:
        ev = json.load(sys.stdin)
        if ev.get("terminationReason") not in MODEL_FINISHED or ev.get("fullyIdle") is False:
            note = f" reason={ev.get('terminationReason')!r} idle={ev.get('fullyIdle')!r}"
        else:
            execution_num = ev.get("executionNum", 0)
            transcript = ev.get("transcriptPath")
            spaces = ev.get("workspacePaths") or []
            projects = [ws for ws in spaces if os.path.isdir(os.path.join(ws, "game"))]
            project = ",".join(projects) or "-"

            renpy_hits, doc_hits = [], []
            for ws in projects:
                rc, found = lint(ws, transcript)
                renpy_hits += found
            for ws in spaces:
                doc_hits += docs_findings(ws, transcript)
            hits = renpy_hits + doc_hits

            parts = []
            if renpy_hits:
                parts.append("Ren'Py lint failed in files you changed:\n" + "\n".join(renpy_hits[:MAX_REPORTED]))
            if doc_hits:
                parts.append("ai-docs-lint failed in files you changed:\n" + "\n".join(doc_hits[:MAX_REPORTED]))
            if parts and execution_num <= MAX_EXECUTIONS:
                reason = "\n".join(parts) + "\nFix them, re-run lint, then finish."
                log(project, rc, len(hits), "continue")
                json.dump({"decision": "continue", "reason": reason}, sys.stdout)
                return
    except Exception:
        note = " exception"

    log(project, rc, len(hits), "stop" + note)
    json.dump(stop, sys.stdout)


if __name__ == "__main__":
    main()
