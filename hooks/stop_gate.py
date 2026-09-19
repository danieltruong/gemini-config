#!/usr/bin/env python3
"""Stop hook: blocks finishing until the transcript proves the changed code was verified."""
import collections
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
# forced continues per conversation, counted separately for each kind of gap
MAX_RETRIES = 1
EVIDENCE_RETRY = "evidence"
CHECK_RETRY = "checks"
MAX_REPORTED = 40
VISUAL_REL = ".agents/visual.md"
# agy 1.2.6 sends "NO_TOOL_CALL" here; "model_stop" is only in the docs. Every other
# reason (ERROR, USER_CANCELED, MAX_*) means the agent did not choose to finish.
MODEL_FINISHED = {"model_stop", "NO_TOOL_CALL"}
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
    "Blocked: no passing verifier run after your last change (step {step}). "
    "Run {cmds}, fix what it reports, then finish."
)

REVIEWER_REASON = (
    "Blocked: {why}. invoke_subagent with TypeName reviewer and a real Prompt on "
    "`git diff HEAD` in {ws}, fix its findings, then finish."
)

VISUAL_QA_REASON = (
    "Blocked: no visual-qa subagent after your last change (step {step}). "
    "invoke_subagent with TypeName visual-qa and a real Prompt on the URLs in {rel} "
    "in {ws}, fix what fails ## Accept, then finish."
)

NO_TRANSCRIPT_REASON = (
    "Blocked: the gate could not read this conversation's transcript ({path}), so nothing "
    "proves the work was checked. Run the verifier and a reviewer, then finish."
)

HUMAN_NOTE = "Stop allowed after one forced retry, still unmet: {summary}"

# what the transcript says this conversation changed: the step span, the files each
# workspace saw, and the steps whose changes cannot be tied to a file
Changes = collections.namedtuple("Changes", "first last files opaque spaces unknown")


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


def session_files(ch, ws):
    """What this session changed under ws. None means unknown, so nothing gets filtered out."""
    files = set(ch.files.get(ws) or ())
    if not files and (ch.unknown or ch.opaque):
        # delegated work and shell writes name no files, so git is the only witness
        return changed_files(ws)
    return files


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


def verify(ws, files, steps, deadline):
    """Run the verifier this workspace selected. None when the repo has none."""
    if steps and steps[0][1] is None:
        return renpy_lint(ws, files, deadline)
    return run_steps(ws, steps, deadline)


def verifier_hint(steps):
    """The command the agent is expected to have run, or '' when the repo has no verifier."""
    return " then ".join(f"`{label}`" for label, _argv in steps)


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


def survey(ch, verifiers, deadline):
    """(verifier failures, doc findings, leftovers) from the checks that run commands."""
    failures, docs, junk = [], [], []
    for ws in ch.spaces:
        files = session_files(ch, ws)
        if files is not None and not files:
            continue
        found = verify(ws, files, verifiers[ws], deadline)
        if found and found[1] != 0:
            failures.append((found[0], found[2]))
        docs += docs_findings(ws, files, deadline)
        junk += leftovers(ws, files)
    return failures, docs, junk


def workspaces(raw_paths):
    """(usable Windows-form paths, paths dropped because they do not resolve)."""
    good, dropped = [], []
    for raw in raw_paths:
        ws = hookpaths.real_path(raw)
        if os.path.isdir(ws):
            good.append(ws)
        else:
            dropped.append(raw)
    return good, dropped


def changes(spaces, calls, outside, unknown):
    """Everything this conversation changed, whether or not an edit tool named the file."""
    files = {ws: transcript.edits(calls, ws, outside) for ws in spaces}
    opaque = transcript.opaque_changes(calls, spaces)
    marks = [i for ws in spaces for pair in files[ws].values() for i in pair] + opaque
    return Changes(min(marks, default=None), max(marks, default=None), files, opaque,
                   spaces, unknown)


def docs_only(ch):
    """Did this conversation change nothing but instruction docs? ai-docs-lint covers those."""
    if ch.opaque:
        return False
    return all(hookpaths.is_instruction_doc(rel) for ws in ch.spaces for rel in ch.files[ws])


def stale_review(ch, reviews):
    """Why the review no longer covers the tree, or '' when it does."""
    if not reviews:
        return f"no reviewer spawned after your first change (step {ch.first})"
    newest = max(reviews)
    late = [i for i in ch.opaque if i > newest]
    if late:
        return (f"a delegated or shell change at step {max(late)} came after the review at step "
                f"{newest}, so what was reviewed is not what is in the tree")
    fresh = sorted(rel for ws in ch.spaces for rel, (first, _last) in ch.files[ws].items()
                   if first > newest)
    if fresh:
        return f"{fresh[0]} was first changed after the review at step {newest}"
    return ""


def verifier_gap(ch, calls, verifiers):
    """The verifier evidence still missing: (log kind, message), or None."""
    if any(index > ch.last and ok
           for ws in ch.spaces
           for index, ok in transcript.verifier_runs(
               calls, [label for label, _argv in verifiers[ws]], ws)):
        return None
    cmds = [f"{verifier_hint(verifiers[ws])} in {ws}" for ws in ch.spaces if verifiers[ws]]
    if not cmds:
        return None
    return "no-verifier", VERIFIER_REASON.format(step=ch.last, cmds="; ".join(cmds))


def review_gap(ch, calls):
    """The review a top-level run still owes: (log kind, message), or None."""
    ws = ch.spaces[0] if ch.spaces else "."
    # a review must cover every change, so it counts from the first one, and later work
    # may only touch files that review already saw
    why = stale_review(ch, [i for i in transcript.spawns(calls, "reviewer") if i > ch.first])
    if why:
        return "no-reviewer", REVIEWER_REASON.format(why=why, ws=ws)
    specs = [w for w in ch.spaces if os.path.isfile(os.path.join(w, VISUAL_REL))]
    if specs and not any(i > ch.last for i in transcript.spawns(calls, "visual-qa")):
        return "no-visual", VISUAL_QA_REASON.format(step=ch.last, rel=VISUAL_REL, ws=specs[0])
    return None


def gap_of(ch, calls, verifiers, subagent):
    """The first piece of missing evidence, or None when the transcript proves the work."""
    if ch.last is None or docs_only(ch):
        return None
    # a subagent answers to the run that spawned it, so only a top-level run owes a review
    return verifier_gap(ch, calls, verifiers) or (None if subagent else review_gap(ch, calls))


def changed_spaces(ch):
    """Workspaces this conversation actually changed."""
    return [ws for ws in ch.spaces if ch.files[ws] or ch.opaque or ch.unknown]


def mark_pending(ch, note):
    for ws in changed_spaces(ch):
        hookpaths.write_pending(ws, [note])


def decide(ev, deadline):
    """(result, log kind, log detail, workspaces)."""
    spaces, dropped = workspaces(ev.get("workspacePaths") or [])
    if dropped:
        log(",".join(dropped), "unresolved", "workspace path dropped")
    tpath = hookpaths.real_path(ev.get("transcriptPath") or "")
    artifact = hookpaths.real_path(ev.get("artifactDirectoryPath") or "")
    brain = transcript.brain_root(tpath)
    cid = ev.get("conversationId") or ""
    steps = transcript.load(tpath)
    subagent = transcript.is_subagent(cid, brain)
    verifiers = {ws: verifier_steps(ws) for ws in spaces}
    calls = transcript.calls(steps)
    ch = changes(spaces, calls, (artifact, brain), steps is None)
    finished = ev.get("terminationReason") in MODEL_FINISHED

    if tpath and steps is None:
        # a transcript the gate cannot read is missing evidence, not evidence of nothing to do
        gap = ("no-transcript", NO_TRANSCRIPT_REASON.format(path=tpath))
    else:
        gap = gap_of(ch, calls, verifiers, subagent)

    if gap:
        kind, message = gap
        detail = f"{'subagent' if subagent else 'top-level'} step {ch.last}"
        if finished and hookpaths.take_retry(cid, EVIDENCE_RETRY, MAX_RETRIES):
            return {"decision": "continue", "reason": message}, kind, detail, spaces
        mark_pending(ch, message)
        if not finished:
            return ({"decision": "stop"}, "pending",
                    f"{kind} reason={ev.get('terminationReason')!r}", spaces)
        return ({"decision": "stop", "reason": HUMAN_NOTE.format(summary=message)},
                "release", f"{kind} {detail}", spaces)

    if not finished:
        return ({"decision": "stop"}, "skip",
                f"reason={ev.get('terminationReason')!r} idle={ev.get('fullyIdle')!r}", spaces)

    # only the checks below run commands of their own, so they wait for the background tasks
    if ev.get("fullyIdle") is False:
        return {"decision": "stop"}, "stop", "background tasks still running", spaces

    failures, docs, junk = survey(ch, verifiers, deadline)
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
        for ws in spaces:
            hookpaths.clear_pending(ws)
        return {"decision": "stop"}, "stop", "verified", spaces

    if hookpaths.take_retry(cid, CHECK_RETRY, MAX_RETRIES):
        return {"decision": "continue", "reason": message + "\n" + budget}, kind, detail, spaces
    mark_pending(ch, f"{kind}: {detail}")
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
        # the stop goes through: the terminal and the pending marker both carry the news
        print(result.get("reason", ""), file=sys.stderr)
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
