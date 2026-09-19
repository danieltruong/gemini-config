#!/usr/bin/env python3
"""Stop hook: blocks finishing until the gate itself has seen the changed workspace pass.

Nothing here reads the transcript for evidence. A workspace changed when its git
fingerprint moved; it is verified when this hook ran its verifier and got exit 0 on that
fingerprint; it is reviewed when a reviewer subagent's own Stop recorded that fingerprint.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

import gitstate
import hookpaths
import transcript

LOG = os.path.join(hookpaths.TMP, "stop_gate.log")
LINT_TIMEOUT = 120
DOCS_TIMEOUT = 30
# hooks.json allows the Stop hook 900s. The verifier gets the bulk of this budget; the docs
# lint, every git call and the renpy lint are capped on their own, so the gate always
# answers before agy kills it and loses the decision.
GATE_BUDGET = 700
MIN_STEP = 5
# forced continues per conversation, counted per gap kind and per fingerprint
MAX_RETRIES = 1
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
# on Windows renpy.sh is a Linux binary and always fails, so only renpy.exe counts there
SDK_EXE = "renpy.exe" if hookpaths.IS_WINDOWS else "renpy.sh"
PYCACHE = "__pycache__"

VERIFIER_REASON = (
    "Verifier failed ({label}) in {ws}:\n{lines}\nFix what it reports, then finish."
)

UNTRUSTED_REASON = (
    "Blocked once: {ws} changed but is not in trustedWorkspaces in {settings}, so the gate "
    "will not run anything there. Run {cmds} yourself and fix what it reports, or add the "
    "workspace to that list so the gate can check it."
)

REVIEW_REASON = (
    "Blocked: {why} in {ws}. invoke_subagent with TypeName {kind} and a real Prompt on "
    "`git diff HEAD`, fix its findings, then finish."
)

VISUAL_REASON = (
    "Blocked: {why} in {ws}. invoke_subagent with TypeName visual-qa and a real Prompt on "
    "the URLs in {rel}, fix what fails ## Accept, then finish."
)

DOCS_REASON = ("ai-docs-lint failed in files you changed:\n{lines}\n"
               "Fix them, then finish.")

LEFTOVERS_REASON = "delete leftovers: {paths}\nRemove them, then finish."

UNFINISHED_NOTE = "changed but not verified: this run ended as {reason}"

HUMAN_NOTE = "Stop allowed after one forced retry, still unmet: {summary}"


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
    roots = (hookpaths.read_json_file(hookpaths.CLI_SETTINGS) or {}).get("trustedWorkspaces") or []
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
    # ponytail: re-lints the whole project on every changed fingerprint (~4 s); cache per
    # fingerprint with the verified record if that drags
    try:
        proc = subprocess.run(
            [os.path.join(sdk, SDK_EXE), ws, "lint", "--error-code"],
            cwd=sdk, capture_output=True, timeout=min(LINT_TIMEOUT, max(1, left)),
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
        argv = script_argv(os.path.join(".agents", name))
        if argv:
            return [(f".agents/{name}", argv)]
        break

    if os.path.isdir(os.path.join(ws, "game")) and find_sdk(ws):
        return [(RENPY_LABEL, None)]

    scripts = (hookpaths.read_json_file(os.path.join(ws, "package.json")) or {}).get("scripts") or {}
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
    """The command the agent is expected to run, or '' when the repo has no verifier."""
    return " then ".join(f"`{label}`" for label, _argv in steps)


def work_names(names):
    """Changed paths the gate judges: the answer file it asked for is not work."""
    return set(names or ()) - transcript.SCRATCH_FILES


def docs_only(names):
    """Did this change touch nothing but instruction docs? ai-docs-lint covers those."""
    work = work_names(names)
    return bool(work) and all(hookpaths.is_instruction_doc(rel) for rel in work)


def docs_findings(ws, names, deadline):
    """ai-docs-lint output for instruction docs this change touched."""
    paths = [os.path.join(ws, rel) for rel in sorted(work_names(names))
             if hookpaths.is_instruction_doc(rel)]
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        return []
    left = remaining(deadline)
    if left < MIN_STEP:
        return [f"verifier budget exhausted: {GATE_BUDGET}s spent before ai-docs-lint ran"]
    try:
        proc = subprocess.run(
            [sys.executable, hookpaths.DOCS_LINT, *paths],
            capture_output=True, text=True, timeout=min(DOCS_TIMEOUT, left),
        )
    except Exception as exc:
        return [f"could not run ai-docs-lint: {exc}"]
    return proc.stdout.splitlines() if proc.returncode else []


def is_junk(rel):
    """A path the gate refuses to leave behind: a backup file or a committed cache."""
    parts = rel.split("/")
    return PYCACHE in parts or bool(hookpaths.BACKUP_SUFFIX.search(parts[-1]))


def touched(ws, cid, fp, edited, names):
    """Is there work in this workspace the gate has to account for?

    Not the same question as "is it verified": a subagent's passing verifier run does not
    excuse the review the parent still owes for the same tree.
    """
    if fp is None:
        # not a git repository, so an edit tool naming a file here is the only signal left;
        # None means the transcript could not be read, which is not proof of nothing
        return edited is None or bool(edited)
    if names and not work_names(names):
        return False
    # a tree that matches its own HEAD, with nothing untracked and nothing moved, owes nothing
    return gitstate.moved(cid, ws, fp) or not gitstate.clean(ws)


def verifier_gap(ws, fp, names, deadline):
    if fp is not None and gitstate.verified_digest(ws) == gitstate.digest(fp):
        return None  # this exact tree already passed, so running it again proves nothing
    steps = verifier_steps(ws)
    if not steps:
        return None
    if not trusted(ws):
        log(ws, "trust", "untrusted workspace, verifier not run")
        return "untrusted", UNTRUSTED_REASON.format(ws=ws, cmds=verifier_hint(steps),
                                                    settings=hookpaths.CLI_SETTINGS)
    found = verify(ws, names, steps, deadline)
    if found and found[1] != 0:
        return "verifier", VERIFIER_REASON.format(
            label=found[0], ws=ws, lines="\n".join(found[2][-MAX_REPORTED:]))
    if fp is not None:
        # after the run, so output the verifier itself leaves does not look like a new change
        gitstate.save_verified(ws, gitstate.fingerprint(ws))
    return None


def review_why(ws, fp, kind):
    """Why this kind of review does not cover the tree in front of us, or ''."""
    rec = gitstate.review(ws, kind)
    if fp is None:
        return "" if rec else f"no {kind} has seen this workspace"
    if not rec:
        return f"no {kind} has seen the current state"
    if rec.get("tracked") != fp[0]:
        return f"tracked files changed after the {kind} run, so it did not see this tree"
    fresh = sorted(set(fp[1]) - set(rec.get("untracked") or []))
    return f"{fresh[0]} is new since the {kind} run" if fresh else ""


def review_gap(ws, fp):
    why = review_why(ws, fp, "reviewer")
    if why:
        return "no-review", REVIEW_REASON.format(why=why, ws=ws, kind="reviewer")
    if os.path.isfile(os.path.join(ws, VISUAL_REL)):
        why = review_why(ws, fp, "visual-qa")
        if why:
            return "no-visual", VISUAL_REASON.format(why=why, ws=ws, rel=VISUAL_REL)
    return None


def workspace_gap(ws, cid, fp, names, role, deadline):
    """The first piece of missing proof for one changed workspace, or None."""
    only_docs = docs_only(names)
    if not only_docs:
        found = verifier_gap(ws, fp, names, deadline)
        if found:
            return found
    docs = docs_findings(ws, names, deadline)
    if docs:
        return "docs", DOCS_REASON.format(lines="\n".join(docs[:MAX_REPORTED]))
    junk = [rel for rel in gitstate.new_untracked(cid, ws, fp) if is_junk(rel)]
    if junk:
        return "leftovers", LEFTOVERS_REASON.format(paths=", ".join(sorted(junk)[:MAX_REPORTED]))
    # a subagent answers to the run that spawned it, and a docs change is its own check
    if role or only_docs:
        return None
    return review_gap(ws, fp)


def derived_spaces(calls, outside):
    """Repository roots holding the files an edit tool named, for stops that carry no workspace."""
    out = []
    for path in transcript.written_paths(calls, outside):
        top = gitstate.toplevel(os.path.dirname(path))
        top = os.path.abspath(top) if top else ""
        if top and os.path.isdir(top) and top not in out:
            out.append(top)
    return out


def workspaces(ev, calls, outside):
    """(workspaces to judge, payload paths dropped because they do not resolve)."""
    good, dropped = [], []
    for raw in ev.get("workspacePaths") or []:
        ws = os.path.abspath(hookpaths.real_path(raw))
        if os.path.isdir(ws):
            good.append(ws)
        else:
            dropped.append(raw)
    return (good or derived_spaces(calls, outside)), dropped


def clear_verified(spaces):
    """Drop a pending marker only where the tree in front of us is one the gate proved."""
    for ws in spaces:
        record = gitstate.verified_digest(ws)
        if record and record == gitstate.digest(gitstate.fingerprint(ws)):
            hookpaths.clear_pending(ws)


def decide(ev, deadline):
    """(result, log kind, log detail, workspaces)."""
    tpath = hookpaths.real_path(ev.get("transcriptPath") or "")
    artifact = hookpaths.real_path(ev.get("artifactDirectoryPath") or "")
    brain = transcript.brain_root(tpath)
    cid = ev.get("conversationId") or transcript.conversation_of(tpath)
    steps = transcript.load(tpath)
    calls = transcript.calls(steps)
    outside = (artifact, brain)
    spaces, dropped = workspaces(ev, calls, outside)
    if dropped:
        log(",".join(dropped), "unresolved", "workspace path dropped")
    prints = {ws: gitstate.fingerprint(ws) for ws in spaces}
    role = transcript.subagent_type(cid, brain)
    # reinforce.py records this at PreInvocation; a first Stop is the fallback
    for ws in spaces:
        gitstate.note_seen(cid, ws, prints[ws])

    if role in transcript.JUDGE_TYPES:
        # a judging subagent owes nothing: it records what it saw and gets out of the way
        kept = [ws for ws in spaces if gitstate.save_review(cid, ws, role, prints[ws])]
        return ({"decision": "stop"}, "review",
                f"{role} recorded {len(kept)}/{len(spaces)}", spaces)

    if not spaces:
        return {"decision": "stop"}, "no-workspace", "no workspace to judge", spaces

    names = {ws: gitstate.changed_names(ws) for ws in spaces}
    edited = {ws: (None if steps is None else transcript.edits(calls, ws, outside))
              for ws in spaces}
    dirty = [ws for ws in spaces if touched(ws, cid, prints[ws], edited[ws], names[ws])]
    finished = ev.get("terminationReason") in MODEL_FINISHED

    if not dirty:
        clear_verified(spaces)
        return {"decision": "stop"}, "stop", "unchanged", spaces

    if not finished:
        # the run did not choose to finish, so the gate leaves a marker rather than spend a
        # verifier run on work the user just interrupted
        reason = ev.get("terminationReason")
        for ws in dirty:
            hookpaths.write_pending(ws, [UNFINISHED_NOTE.format(reason=reason)])
        return ({"decision": "stop"}, "pending",
                f"reason={reason!r} changed={len(dirty)}", spaces)

    gap, culprit = None, dirty[0]
    for ws in dirty:
        gap = workspace_gap(ws, cid, prints[ws], names[ws], role, deadline)
        if gap:
            culprit = ws
            break

    if not gap:
        clear_verified(dirty)
        return {"decision": "stop"}, "stop", "verified", spaces

    kind, message = gap
    detail = f"{role or 'top-level'} {culprit}"
    budget = f"{int(max(0, remaining(deadline)))}s of the {GATE_BUDGET}s gate budget left."
    allowance = f"{kind}:{gitstate.digest(prints[culprit]) or 'nogit'}"
    if kind != "untrusted" and hookpaths.take_retry(cid, allowance, MAX_RETRIES):
        return {"decision": "continue", "reason": message + "\n" + budget}, kind, detail, spaces
    hookpaths.write_pending(culprit, [message])
    return ({"decision": "stop", "reason": HUMAN_NOTE.format(summary=message)},
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
