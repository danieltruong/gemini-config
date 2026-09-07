#!/usr/bin/env python3
"""Stop hook: blocks finishing while the repo's own verifier fails on files the agent changed."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import hookpaths

LOG = os.path.join(hookpaths.TMP, "stop_gate.log")
LINT_TIMEOUT = 120
# one budget for the whole gate: every lint, verifier and docs run across every workspace
GATE_BUDGET = 800
MIN_STEP = 5
MAX_EXECUTIONS = 3
MAX_AUDIT_ROUNDS = 5
MAX_REPORTED = 40
AUDIT_REL = ".agents/audit.json"
VISUAL_REL = ".agents/visual.md"
DECISIONS_REL = ".agents/DECISIONS.md"
# the agent's own answers to the gate, not work the gate should judge or date-stamp against
SCRATCH_FILES = {AUDIT_REL, DECISIONS_REL}
# agy 1.1.27 sends "NO_TOOL_CALL" here; "model_stop" is only in the docs. Every other
# reason (ERROR, USER_CANCELED, MAX_*) means the agent did not choose to finish.
MODEL_FINISHED = {"model_stop", "NO_TOOL_CALL"}
WRITE_TOOLS = {"replace_file_content", "write_to_file", "multi_replace_file_content", "sed_file"}
VERIFY_SCRIPTS = ("verify.cmd", "verify.ps1", "verify.sh")
PYTEST_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg")
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

AUDIT_REASON = (
    "Audit round {round}: review `git diff HEAD` plus untracked files you created. "
    "Delegate: reviewer for findings, linter for lint fixes, tester for missing tests, "
    "visual-qa for .agents/visual.md. Work inline only when subagents are unavailable. "
    "Fix every bug, security, wrong-result, dead-code and over-engineering finding; "
    "re-run the verifier; then write .agents/audit.json {{clean, findings, round}} and "
    "finish. clean is true only when the last audit found nothing to fix."
)

VISUAL_REASON = (
    "Visual audit: for each URL in .agents/visual.md, open_browser_url, "
    "capture_browser_screenshot, check every bullet under ## Accept against the screenshot, "
    "fix defects, repeat until every page passes. Record visual results in audit.json under "
    '"visual": {"pages": n, "failed": m}.'
)


def norm(path):
    """Case-folded, forward-slash form for comparing two paths on either platform."""
    return os.path.normcase(path).replace("\\", "/")


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
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        hookpaths.append(LOG, f"{stamp} {project} {kind} {detail}\n")
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
    root = norm(os.path.abspath(project)).rstrip("/") + "/"
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
                if norm(path).startswith(root):
                    out.add(path[len(root):])
    return out if parsed else None


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


def session_files(transcript, project):
    """What this session wrote under project. None means unknown, so nothing gets filtered out."""
    touched = touched_files(transcript, project) if transcript else None
    files = changed_files(project) if touched is None else touched
    return files if files is None else files - SCRATCH_FILES


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
        return "renpy lint", 1, [f"verifier budget exhausted: {GATE_BUDGET}s spent before lint ran"]
    # ponytail: re-lints the whole project on every Stop (~4 s); cache on .rpy mtimes if that drags
    try:
        proc = subprocess.run(
            [os.path.join(sdk, SDK_EXE), ws, "lint", "--error-code"],
            cwd=sdk, capture_output=True, timeout=min(LINT_TIMEOUT, max(1, deadline - time.monotonic())),
        )
    except Exception as exc:
        return "renpy lint", 1, [f"could not run renpy lint: {exc}"]
    text = proc.stdout.decode("utf-8", "replace").lstrip("\ufeff")
    hits = []
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        m = LINT_LINE.match(line)
        if m and (files is None or m.group(1) in files):
            hits.append(line)
    if not hits and proc.returncode != 0:
        # lint failed without a parseable report line: crash, bad project path, missing asset dir
        err = proc.stderr.decode("utf-8", "replace").splitlines()
        return "renpy lint", 1, [f"renpy lint exited {proc.returncode}"] + err[-MAX_REPORTED:]
    return "renpy lint", 1 if hits else 0, hits


def run_steps(ws, steps, deadline):
    """The first failing step, or a pass for the last one. None when there are no steps."""
    for label, argv in steps:
        found = execute(ws, label, argv, deadline)
        if found[1] != 0:
            return found
    return (steps[-1][0], 0, []) if steps else None


def verify(ws, files, deadline):
    """Run the first verifier that fits this workspace. None when the repo has none."""
    for name in VERIFY_SCRIPTS:
        if not os.path.isfile(os.path.join(ws, ".agents", name)):
            continue
        if not trusted(ws):
            log(ws, "trust", "untrusted workspace, verifier skipped")
            break
        argv = script_argv(os.path.join(".agents", name))
        if argv:
            return execute(ws, f".agents/{name}", argv, deadline)

    if os.path.isdir(os.path.join(ws, "game")):
        found = renpy_lint(ws, files, deadline)
        if found:
            return found

    pkg = read_json(os.path.join(ws, "package.json"))
    scripts = (pkg or {}).get("scripts") or {}
    npm = shutil.which("npm") or "npm"
    steps = []
    if "lint" in scripts:
        steps.append(("npm run lint", [npm, "run", "lint"]))
    if "test" in scripts:
        steps.append(("npm test", [npm, "test"]))
    found = run_steps(ws, steps, deadline)
    if found:
        return found

    for markers, marker_steps in MARKER_VERIFIERS:
        if any(glob.glob(os.path.join(ws, m)) for m in markers):
            return run_steps(ws, marker_steps, deadline)

    return None


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


def newest_mtime(ws, files):
    stamps = []
    for rel in files or ():
        try:
            stamps.append(os.path.getmtime(os.path.join(ws, rel)))
        except OSError:
            pass
    return max(stamps) if stamps else None


def audit_report(ws):
    data = read_json(os.path.join(ws, AUDIT_REL))
    return data if isinstance(data, dict) else None


def audit_clean(ws, files):
    """True when the audit report says the last round found nothing and predates no edit."""
    data = audit_report(ws)
    if not data or not data.get("clean"):
        return False
    newest = newest_mtime(ws, files)
    try:
        if newest is not None and os.path.getmtime(os.path.join(ws, AUDIT_REL)) < newest:
            return False
    except OSError:
        return False
    if os.path.isfile(os.path.join(ws, VISUAL_REL)):
        return (data.get("visual") or {}).get("failed") == 0
    return True


def next_round(ws):
    """The round number the agent is being asked to run now."""
    try:
        return int((audit_report(ws) or {}).get("round", 0)) + 1
    except (TypeError, ValueError):
        return 1


def audit_reason(spaces):
    text = AUDIT_REASON.format(round=max(next_round(ws) for ws in spaces))
    if any(os.path.isfile(os.path.join(ws, VISUAL_REL)) for ws in spaces):
        text += "\n" + VISUAL_REASON
    return text


def clear_audits(spaces):
    """The audit report is per-run scratch, so the next run cannot inherit a stale pass."""
    for ws in spaces:
        try:
            os.remove(os.path.join(ws, AUDIT_REL))
        except OSError:
            pass


def survey(spaces, transcript, deadline):
    """(verifier failures, doc findings, workspaces still owing an audit round)."""
    failures, docs, audits = [], [], []
    for ws in spaces:
        files = session_files(transcript, ws)
        if files is not None and not files:
            continue
        found = verify(ws, files, deadline)
        if found and found[1] != 0:
            failures.append((found[0], found[2]))
        docs += docs_findings(ws, files, deadline)
        code = files is None or any(not hookpaths.is_instruction_doc(f) for f in files)
        if code and not audit_clean(ws, files):
            audits.append(ws)
    return failures, docs, audits


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


def decide(ev, deadline):
    """(result, log kind, log detail, workspaces, whether this is a clean stop)."""
    if ev.get("terminationReason") not in MODEL_FINISHED or ev.get("fullyIdle") is False:
        detail = f"reason={ev.get('terminationReason')!r} idle={ev.get('fullyIdle')!r}"
        return {"decision": "stop"}, "stop", detail, [], False

    spaces, dropped = workspaces(ev.get("workspacePaths") or [])
    if dropped:
        log(",".join(dropped), "unresolved", "workspace path dropped")
    execution_num = ev.get("executionNum", 0)
    failures, docs, audits = survey(spaces, ev.get("transcriptPath"), deadline)
    budget = f"{int(max(0, remaining(deadline)))}s of the {GATE_BUDGET}s gate budget left."
    rounds = max((next_round(ws) for ws in audits), default=0)

    if failures and execution_num <= MAX_EXECUTIONS:
        reason = "\n".join(
            f"Verifier failed ({label}):\n" + "\n".join(lines[-MAX_REPORTED:])
            + f"\nFix, re-run {label}, then finish."
            for label, lines in failures
        )
        kind, detail = "verifier", ",".join(label for label, _ in failures)
    elif docs and execution_num <= MAX_EXECUTIONS:
        reason = ("ai-docs-lint failed in files you changed:\n"
                  + "\n".join(docs[:MAX_REPORTED]) + "\nFix them, re-run lint, then finish.")
        kind, detail = "docs", f"{len(docs)} findings"
    elif audits and rounds <= MAX_AUDIT_ROUNDS:
        reason = audit_reason(audits)
        kind, detail = "audit", f"round {rounds}"
    else:
        detail = "audit cap" if audits else f"execution={execution_num}"
        return {"decision": "stop"}, "stop", detail, spaces, True

    return {"decision": "continue", "reason": reason + "\n" + budget}, kind, detail, spaces, False


def main():
    result, kind, detail, spaces, clean_stop = {"decision": "stop"}, "stop", "", [], False
    project = "-"
    try:
        ev = json.load(sys.stdin)
        result, kind, detail, spaces, clean_stop = decide(ev, time.monotonic() + GATE_BUDGET)
        project = ",".join(spaces) or "-"
    except Exception as exc:
        result, kind, detail, clean_stop = {"decision": "stop"}, "stop", f"exception {exc!r}", False

    # only a clean stop retires the audit report; an exception must not hide the last round
    if clean_stop:
        clear_audits(spaces)
    log(project, kind, detail)
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
