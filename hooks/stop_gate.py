#!/usr/bin/env python3
"""Stop hook: blocks finishing until the transcript proves the changed code was verified."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import hookpaths
import transcript

LOG = os.path.join(hookpaths.TMP, "stop_gate.log")
LINT_TIMEOUT = 120
# one budget for the whole gate: every lint, verifier and docs run across every workspace
GATE_BUDGET = 800
MIN_STEP = 5
# one forced continue per conversation, counted by agy's 0-based executionNum
MAX_EXECUTIONS = 1
MAX_REPORTED = 40
VISUAL_REL = ".agents/visual.md"
# agy 1.2.6 sends "NO_TOOL_CALL" here; "model_stop" is only in the docs. Every other
# reason (ERROR, USER_CANCELED, MAX_*) means the agent did not choose to finish.
MODEL_FINISHED = {"model_stop", "NO_TOOL_CALL"}
ERRORED = {"error", "ERROR"}
VERIFY_SCRIPTS = ("verify.cmd", "verify.ps1", "verify.sh")
PYTEST_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg")
RENPY_LABEL = "renpy lint"
# marker files in the workspace root -> the steps that verify it, first match wins
MARKER_VERIFIERS = (
    (("Cargo.toml",), (("cargo test -q", ["cargo", "test", "-q"]),)),
    (("go.mod",), (("go vet ./...", ["go", "vet", "./..."]),
                   ("go test ./...", ["go", "test", "./..."]))),
    (("*.sln", "*.csproj"), (("dotnet test", ["dotnet", "test"]),)),
    (PYTEST_MARKERS, (("python -m pytest -q -x", [sys.executable, "-m", "pytest", "-q", "-x"]),)),
)
# lint report lines start with a project-relative path: "game/foo/bar.rpy:12 message"
LINT_LINE = re.compile(r"^(\S+\.rpym?):(\d+)\s")
IS_WINDOWS = os.name == "nt"
# on Windows renpy.sh is a Linux binary and always fails, so only renpy.exe counts there
SDK_EXE = "renpy.exe" if IS_WINDOWS else "renpy.sh"

VERIFIER_REASON = (
    "Blocked: no passing verifier run after your last edit (step {step}). "
    "Run {cmds}, fix what it reports, then finish."
)

REVIEWER_REASON = (
    "Blocked: no reviewer spawned after your last edit (step {step}). "
    "invoke_subagent with TypeName reviewer on `git diff HEAD` in {ws}, "
    "fix its findings, then finish."
)

VISUAL_REASON = (
    "Also {rel}: open_browser_url then capture_browser_screenshot every URL under "
    "## Pages, fix anything that fails a bullet under ## Accept."
)

HUMAN_NOTE = "Stop allowed after one forced retry, still unmet: {summary}"


def windows_path(p):
    """agy started from Git Bash sends MSYS paths, which Windows Python cannot open."""
    if not IS_WINDOWS or not p.startswith("/"):
        return p
    cygpath = shutil.which("cygpath")
    if cygpath:
        try:
            proc = subprocess.run([cygpath, "-w", p], capture_output=True, text=True, timeout=10)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip().replace("\\", "/")
        except Exception:
            pass
    m = re.match(r"^/([a-zA-Z])(/.*)?$", p)
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if m else p


def remaining(deadline):
    return deadline - time.monotonic()


def log(project, kind, detail):
    """One tab-separated line per decision: kind first, because a path can hold spaces."""
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        hookpaths.append(LOG, f"{stamp}\t{kind}\t{project}\t{detail}\n")
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
    """stdout, or None when git fails: no commits yet, not a repo, git missing."""
    try:
        proc = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=30)
    except Exception:
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def changed_files(project):
    """Project-relative paths git reports as modified or untracked, or None when not a git repo."""
    try:
        top = git(project, "rev-parse", "--show-toplevel")
        if not top or not top.strip():
            return None
        top = top.strip()
        rel = os.path.relpath(os.path.abspath(project), os.path.abspath(top)).replace("\\", "/")
        prefix = "" if rel == "." else rel + "/"
        names = (git(project, "diff", "--name-only", "HEAD") or "").splitlines()
        names += (git(project, "ls-files", "--others", "--exclude-standard") or "").splitlines()
        out = set()
        for n in names:
            n = n.strip().replace("\\", "/")
            if n and n.startswith(prefix):
                out.add(n[len(prefix):])
        return out
    except Exception:
        return None


def session_files(steps, project):
    """What this session wrote under project. None means unknown, so nothing gets filtered out."""
    touched = set(transcript.edits(steps, project)) if steps else None
    files = changed_files(project) if touched is None else touched
    return files if files is None else files - transcript.SCRATCH_FILES


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def script_argv(path):
    """Argv for a verify script, or None when the interpreter it needs is missing."""
    if path.endswith(".cmd"):
        # "call" so cmd returns the script's exit code instead of its own
        return ["cmd", "/c", "call", path]
    if path.endswith(".ps1"):
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", path]
    bash = shutil.which("bash")
    return [bash, path] if bash else None


def trusted(ws):
    """agy only runs commands in workspaces the user trusted; the gate honours that list."""
    roots = (read_json(hookpaths.CLI_SETTINGS) or {}).get("trustedWorkspaces") or []
    norm = transcript.norm
    target = norm(os.path.abspath(ws)).rstrip("/")
    return any(target == norm(r).rstrip("/") or target.startswith(norm(r).rstrip("/") + "/")
               for r in roots)


def execute(ws, label, argv, deadline):
    """(label, returncode, output lines). Timeout or launch failure counts as a failure."""
    left = remaining(deadline)
    if left < MIN_STEP:
        return label, 1, [f"verifier budget exhausted: {GATE_BUDGET}s spent before {label} ran"]
    try:
        proc = subprocess.run(argv, cwd=ws, capture_output=True, timeout=left)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        return label, proc.returncode, out.splitlines()
    except subprocess.TimeoutExpired:
        return label, 1, [f"timed out after {int(left)}s"]
    except Exception as exc:
        return label, 1, [f"could not run {label}: {exc}"]


def renpy_lint(ws, files, deadline):
    sdk = find_sdk(ws)
    if sdk is None:
        return None
    left = remaining(deadline)
    if left < MIN_STEP:
        return RENPY_LABEL, 1, [f"verifier budget exhausted: {GATE_BUDGET}s spent before lint ran"]
    # ponytail: re-lints the whole project on every Stop (~4 s); cache on .rpy mtimes if that drags
    try:
        proc = subprocess.run(
            [os.path.join(sdk, SDK_EXE), ws, "lint", "--error-code"],
            cwd=sdk, capture_output=True, timeout=min(LINT_TIMEOUT, max(1, deadline - time.monotonic())),
        )
    except Exception as exc:
        return RENPY_LABEL, 1, [f"could not run renpy lint: {exc}"]
    text = proc.stdout.decode("utf-8", "replace").lstrip("﻿")
    hits = []
    parsed_any = False
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        m = LINT_LINE.match(line)
        if m:
            parsed_any = True
            if files is None or m.group(1) in files:
                hits.append(line)
    if not parsed_any and proc.returncode != 0:
        # lint failed without a parseable report line: crash, bad project path, missing asset dir
        err = proc.stderr.decode("utf-8", "replace").splitlines()
        return RENPY_LABEL, 1, [f"renpy lint exited {proc.returncode}"] + err[-MAX_REPORTED:]
    return RENPY_LABEL, 1 if hits else 0, hits


def run_steps(ws, steps, deadline):
    """The first failing step, or a pass for the last one. None when there are no steps."""
    for label, argv in steps:
        found = execute(ws, label, argv, deadline)
        if found[1] != 0:
            return found
    return (steps[-1][0], 0, []) if steps else None


def verifier_steps(ws):
    """(label, argv) for what verifies this workspace, first match wins. renpy lint has no argv."""
    for name in VERIFY_SCRIPTS:
        if not os.path.isfile(os.path.join(ws, ".agents", name)):
            continue
        if not trusted(ws):
            log(ws, "trust", "untrusted workspace, verifier skipped")
            break
        argv = script_argv(os.path.join(".agents", name))
        if argv:
            return [(f".agents/{name}", argv)]
        break

    if os.path.isdir(os.path.join(ws, "game")) and find_sdk(ws):
        return [(RENPY_LABEL, None)]

    scripts = (read_json(os.path.join(ws, "package.json")) or {}).get("scripts") or {}
    npm = shutil.which("npm") or "npm"
    steps = []
    if "lint" in scripts:
        steps.append(("npm run lint", [npm, "run", "lint"]))
    if "test" in scripts:
        steps.append(("npm test", [npm, "test"]))
    if steps:
        return steps

    for markers, marker_steps in MARKER_VERIFIERS:
        if any(glob.glob(os.path.join(ws, m)) for m in markers):
            return list(marker_steps)
    return []


def verify(ws, files, deadline):
    """Run the first verifier that fits this workspace. None when the repo has none."""
    steps = verifier_steps(ws)
    if steps and steps[0][1] is None:
        return renpy_lint(ws, files, deadline)
    return run_steps(ws, steps, deadline)


def verifier_hint(ws):
    """The command the agent is expected to have run here, or '' when the repo has no verifier."""
    return " then ".join(f"`{label}`" for label, _argv in verifier_steps(ws))


def docs_findings(ws, files, deadline):
    """ai-docs-lint output for instruction docs this session wrote."""
    if not files:
        return []
    paths = [os.path.join(ws, rel) for rel in sorted(files) if hookpaths.is_instruction_doc(rel)]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return []
    left = remaining(deadline)
    if left < MIN_STEP:
        return [f"verifier budget exhausted: {GATE_BUDGET}s spent before ai-docs-lint ran"]
    try:
        proc = subprocess.run(
            [sys.executable, hookpaths.DOCS_LINT, *paths],
            capture_output=True, text=True, timeout=min(30, left),
        )
    except Exception as exc:
        return [f"could not run ai-docs-lint: {exc}"]
    return proc.stdout.splitlines() if proc.returncode else []


def leftovers(ws, files):
    """Backup files, unignored __pycache__ and empty dirs in the directories the session wrote."""
    rels = {os.path.dirname(f) for f in files} if files else {""}
    found = []
    for rel in sorted(rels):
        folder = os.path.join(ws, rel)
        try:
            entries = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in entries:
            path = os.path.join(folder, name)
            shown = f"{rel}/{name}" if rel else name
            if os.path.isdir(path):
                if name == "__pycache__" and git(ws, "check-ignore", path) is None:
                    found.append(shown)
                elif not os.listdir(path):
                    found.append(shown + "/")
            elif hookpaths.BACKUP_SUFFIX.search(name):
                found.append(shown)
    return found


def survey(spaces, steps, deadline):
    """(verifier failures, doc findings, leftovers) from the checks that run commands."""
    failures, docs, junk = [], [], []
    for ws in spaces:
        files = session_files(steps, ws)
        if files is not None and not files:
            continue
        found = verify(ws, files, deadline)
        if found and found[1] != 0:
            failures.append((found[0], found[2]))
        docs += docs_findings(ws, files, deadline)
        junk += leftovers(ws, files)
    return failures, docs, junk


def workspaces(raw_paths):
    """(usable Windows-form paths, paths dropped because they do not resolve)."""
    good, dropped = [], []
    for raw in raw_paths:
        ws = windows_path(raw)
        if os.path.isdir(ws):
            good.append(ws)
        else:
            dropped.append(raw)
    return good, dropped


def edited(steps, spaces, outside):
    """{workspace-relative path: last step index that wrote it} across every workspace."""
    outside = [p for p in outside if p]
    out = {}
    for ws in spaces:
        out.update(transcript.edits(steps, ws, outside))
    return out


def evidence_gap(spaces, files, steps, role):
    """What the transcript still fails to prove: (log kind, message), or None when it proves it."""
    if not files:
        return None
    last_edit = max(files.values())
    if not any(index > last_edit and ok for index, ok in transcript.verifier_runs(steps)):
        cmds = [f"{hint} in {ws}" for ws, hint in ((w, verifier_hint(w)) for w in spaces) if hint]
        if cmds:
            return "no-verifier", VERIFIER_REASON.format(step=last_edit, cmds="; ".join(cmds))
    # a subagent answers to the run that spawned it, so only a top-level run owes a reviewer
    code = any(not hookpaths.is_instruction_doc(rel) for rel in files)
    if role is None and code and not any(i > last_edit for i in transcript.reviewer_spawns(steps)):
        reason = REVIEWER_REASON.format(step=last_edit, ws=spaces[0] if spaces else ".")
        if any(os.path.isfile(os.path.join(ws, VISUAL_REL)) for ws in spaces):
            reason += " " + VISUAL_REASON.format(rel=VISUAL_REL)
        return "no-reviewer", reason
    return None


def mark_pending(spaces, files, cid):
    """Remember unverified edits a crashed run left, for the next invocation to announce."""
    step = max(files.values())
    for ws in spaces:
        hookpaths.write_pending(ws, {"workspace": ws, "step": step, "conversation": cid,
                                     "when": time.strftime("%Y-%m-%dT%H:%M:%S")})


def decide(ev, deadline):
    """(result, log kind, log detail, workspaces)."""
    spaces, dropped = workspaces(ev.get("workspacePaths") or [])
    if dropped:
        log(",".join(dropped), "unresolved", "workspace path dropped")
    brain = transcript.brain_root(ev.get("transcriptPath"))
    steps = transcript.load(ev.get("transcriptPath"))
    role = transcript.subagent_type(ev.get("conversationId"), brain)
    files = edited(steps, spaces, (ev.get("artifactDirectoryPath"), brain))
    gap = evidence_gap(spaces, files, steps, role)
    reason = ev.get("terminationReason")
    execution_num = ev.get("executionNum", 0)

    if reason not in MODEL_FINISHED:
        if gap and reason in ERRORED:
            mark_pending(spaces, files, ev.get("conversationId") or "")
            return {"decision": "stop"}, "pending", f"{gap[0]} step {max(files.values())}", spaces
        return ({"decision": "stop"}, "skip",
                f"reason={reason!r} idle={ev.get('fullyIdle')!r}", spaces)

    if gap:
        kind, message = gap
        detail = f"{'subagent ' + role if role else 'top-level'} step {max(files.values())}"
        if execution_num < MAX_EXECUTIONS:
            return {"decision": "continue", "reason": message}, kind, detail, spaces
        return ({"decision": "stop", "reason": HUMAN_NOTE.format(summary=message)},
                "release", f"{kind} {detail}", spaces)

    # only the checks below run commands of their own, so they wait for the background tasks
    if ev.get("fullyIdle") is False:
        return {"decision": "stop"}, "stop", "background tasks still running", spaces

    failures, docs, junk = survey(spaces, steps, deadline)
    budget = f"{int(max(0, remaining(deadline)))}s of the {GATE_BUDGET}s gate budget left."
    if failures:
        message = "\n".join(
            f"Verifier failed ({label}):\n" + "\n".join(lines[-MAX_REPORTED:])
            + f"\nFix, re-run {label}, then finish."
            for label, lines in failures
        )
        kind, detail = "verifier", ",".join(label for label, _ in failures)
    elif docs:
        message = ("ai-docs-lint failed in files you changed:\n"
                   + "\n".join(docs[:MAX_REPORTED]) + "\nFix them, re-run lint, then finish.")
        kind, detail = "docs", f"{len(docs)} findings"
    elif junk:
        message = ("delete leftovers: " + ", ".join(sorted(set(junk))[:MAX_REPORTED])
                   + "\nRemove them, then finish.")
        kind, detail = "leftovers", f"{len(junk)} paths"
    else:
        return {"decision": "stop"}, "stop", f"execution={execution_num}", spaces

    if execution_num < MAX_EXECUTIONS:
        return {"decision": "continue", "reason": message + "\n" + budget}, kind, detail, spaces
    return ({"decision": "stop", "reason": HUMAN_NOTE.format(summary=f"{kind}: {detail}")},
            "release", f"{kind} {detail}", spaces)


def main():
    result, kind, detail, spaces = {"decision": "stop"}, "stop", "", []
    try:
        ev = json.load(sys.stdin)
        result, kind, detail, spaces = decide(ev, time.monotonic() + GATE_BUDGET)
    except Exception as exc:
        result, kind, detail = {"decision": "stop"}, "stop", f"exception {exc!r}"

    log(",".join(spaces) or "-", kind, detail)
    if kind == "release":
        # the stop goes through, so the terminal is the only thing the human still reads
        print(result.get("reason", ""), file=sys.stderr)
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
