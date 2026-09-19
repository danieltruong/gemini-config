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
MAX_REPORTED = 40
VISUAL_REL = ".agents/visual.md"
# agy 1.2.6 sends "NO_TOOL_CALL" here; "model_stop" is only in the docs. Every other
# reason (ERROR, USER_CANCELED, MAX_*) means the agent did not choose to finish.
MODEL_FINISHED = {"model_stop", "NO_TOOL_CALL"}
VERIFY_SCRIPTS = ("verify.cmd", "verify.ps1", "verify.sh")
PYTEST_MARKERS = ("pyproject.toml", "pytest.ini", "setup.cfg")
PYTEST_LABEL = "python -m pytest -q -x"
# pytest exits 5 when it collected nothing: that repo has no tests, it has no failure
NO_TESTS_CODE = 5
RENPY_LABEL = "renpy lint"
# one stop runs a workspace's verifier at a time
LOCKS = os.path.join(hookpaths.TMP, "locks")
# the holder touches its lock every LOCK_POLL seconds while the verifier runs, so this is "no
# heartbeat for this long", not "this run is too slow": a few missed beats, not a whole budget,
# is what a waiter pays for a lock whose gate agy killed
LOCK_MAX_AGE = 30
LOCK_POLL = 2
# waiting out another stop is only worth it when enough budget is left to then verify
LOCK_FLOOR = 60
LOCK_BUSY_NOTE = "not verified: another stop was running the verifier here"
# marker files in the workspace root -> the steps that verify it, first match wins
MARKER_VERIFIERS = (
    (("Cargo.toml",), (("cargo test -q", ["cargo", "test", "-q"]),)),
    (("go.mod",), (("go vet ./...", ["go", "vet", "./..."]),
                   ("go test ./...", ["go", "test", "./..."]))),
    (("*.sln", "*.csproj"), (("dotnet test", ["dotnet", "test"]),)),
    (PYTEST_MARKERS, ((PYTEST_LABEL, [sys.executable, "-m", "pytest", "-q", "-x"]),)),
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
    "Not checked, workspace is not trusted: {ws} changed, and it is not in trustedWorkspaces "
    "in {settings}, so the gate ran nothing there. Run {cmds} yourself and fix what it "
    "reports, or add the workspace to that list so the gate can check it."
)

# gaps the gate reports without running anything and without forcing a retry
NOT_CHECKED = {"untrusted", "no-git"}

NO_GIT_REASON = (
    "Not checked, {ws} is not a git repository, so the gate cannot tell what changed there "
    "or prove that anything reviewed it. Check the work yourself before you finish."
)

REVIEW_REASON = (
    "Blocked: {why} in {ws}. invoke_subagent with TypeName {kind} and a real Prompt on "
    "{subject}, fix its findings, then finish."
)
# per judge: the gap kind logged when it is missing, and what that judge is asked to look at
REVIEW_KINDS = {
    "reviewer": ("no-review", "`git diff HEAD`"),
    "visual-qa": ("no-visual", f"the URLs in {VISUAL_REL}, then on what fails ## Accept"),
}

DOCS_REASON = ("ai-docs-lint failed in files you changed:\n{lines}\n"
               "Fix them, then finish.")

LEFTOVERS_REASON = "delete leftovers: {paths}\nRemove them, then finish."

UNFINISHED_NOTE = "changed but not verified: this run ended as {reason}"

HUMAN_NOTE = "Stop allowed after one forced retry, still unmet: {summary}"

CEILING_NOTE = ("Stop allowed: this conversation has already been sent back {n} times, "
                "so the gate stops asking. Still unmet: {summary}")


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


def wait_for(proc, left, beat):
    """(stdout, stderr) once the child is done, calling beat while it is still running."""
    end = time.monotonic() + left
    while True:
        try:
            return proc.communicate(timeout=max(0.1, min(LOCK_POLL, end - time.monotonic())))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= end:
                proc.kill()
                proc.communicate()
                raise
            if beat:
                beat()


def execute(ws, label, argv, deadline, beat=None):
    """(label, returncode, output lines). Timeout or launch failure counts as a failure."""
    left = remaining(deadline)
    if left < MIN_STEP:
        return label, 1, [f"verifier budget exhausted: {GATE_BUDGET}s spent before {label} ran"]
    try:
        proc = subprocess.Popen(argv, cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = wait_for(proc, left, beat)
        return label, proc.returncode, (out + err).decode("utf-8", "replace").splitlines()
    except subprocess.TimeoutExpired:
        return label, 1, [f"timed out after {int(left)}s"]
    except Exception as exc:
        return label, 1, [f"could not run {label}: {exc}"]


def renpy_lint(ws, files, deadline, beat=None):
    sdk = find_sdk(ws)
    if sdk is None:
        return None
    left = remaining(deadline)
    if left < MIN_STEP:
        return RENPY_LABEL, 1, [f"verifier budget exhausted: {GATE_BUDGET}s spent before lint ran"]
    # ponytail: re-lints the whole project on every changed fingerprint (~4 s); cache per
    # fingerprint with the verified record if that drags
    try:
        proc = subprocess.Popen([os.path.join(sdk, SDK_EXE), ws, "lint", "--error-code"],
                                cwd=sdk, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = wait_for(proc, min(LINT_TIMEOUT, max(1, left)), beat)
    except Exception as exc:
        return RENPY_LABEL, 1, [f"could not run renpy lint: {exc}"]
    text = out.decode("utf-8", "replace").lstrip("﻿")
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
        lines = err.decode("utf-8", "replace").splitlines()
        return RENPY_LABEL, 1, [f"renpy lint exited {proc.returncode}"] + lines[-MAX_REPORTED:]
    return RENPY_LABEL, 1 if hits else 0, hits


def run_steps(ws, steps, deadline, beat=None):
    """The first failing step, or a pass for the last one. None when nothing was verified."""
    for label, argv in steps:
        found = execute(ws, label, argv, deadline, beat)
        if label == PYTEST_LABEL and found[1] == NO_TESTS_CODE:
            return None  # no tests here, so this repo has no verifier rather than a failure
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


def verify(ws, files, steps, deadline, beat=None):
    """Run the verifier this workspace selected. None when the repo has none."""
    if steps and steps[0][1] is None:
        return renpy_lint(ws, files, deadline, beat)
    return run_steps(ws, steps, deadline, beat)


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


def touched(ws, cid, fp, worked, edited, blind):
    """Is there work in this workspace the gate has to account for?

    Not the same question as "is it verified": a subagent's passing verifier run does not
    excuse the review the parent still owes for the same tree.

    blind means the gate had no record of this workspace before this stop and cannot read the
    transcript either, so it has nothing to date the dirt with and fails closed on it.
    """
    if fp is None:
        # not a git repository, so an edit tool naming a file here is the only signal left
        return bool(edited)
    if fp.names and not work_names(fp.names):
        return False
    if gitstate.baseline(cid, ws):
        return gitstate.moved(cid, ws, fp)
    if blind:
        return bool(work_names(fp.names))
    # only a fallback record: it is still a floor for anything that happened after it, and a
    # work tool call in the transcript is the other half of the answer
    return gitstate.moved(cid, ws, fp) or worked


def cached_pass(ws, fp):
    """Has the gate already run a passing verifier on this exact tree?"""
    rec = gitstate.verified(ws)
    return bool(rec.get("digest")) and fp is not None and rec["digest"] == gitstate.digest(
        gitstate.without(fp, rec.get("artifacts") or []))


def lock_file(ws):
    return os.path.join(LOCKS, hookpaths.ws_key(ws) + ".json")


def lock_left(ws):
    """Seconds a foreign lock can still live, or 0 when it is abandoned or unreadable."""
    rec = hookpaths.read_json_file(lock_file(ws)) or {}
    try:
        return max(0.0, LOCK_MAX_AGE - (time.time() - float(rec.get("at") or 0)))
    except (TypeError, ValueError):
        return 0.0  # a lock file nobody can read cannot be waited on


def claim_lock(ws):
    """The token this process wrote, or '' when another stop holds a live lock.

    O_EXCL is the claim itself: two stops racing here cannot both create the file.
    """
    path = lock_file(ws)
    token = f"{os.getpid()}:{time.time_ns()}"
    for _attempt in (1, 2):
        try:
            os.makedirs(LOCKS, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = hookpaths.read_json_file(path) or {}
            if lock_left(ws) > 0:
                return ""
            # rename rather than remove, so of several stops meeting one abandoned lock only the
            # one whose rename landed carries on
            dead = f"{path}.{os.getpid()}.dead"
            try:
                os.replace(path, dead)
            except OSError:
                return ""
            stolen = hookpaths.read_json_file(dead) or {}
            if stolen.get("token") != held.get("token"):
                os.replace(dead, path)  # another stop claimed it between the read and the rename
                return ""
            try:
                os.remove(dead)
            except OSError:
                pass
            continue
        except OSError:
            return ""
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "pid": os.getpid(), "token": token}, fh)
        return token
    return ""


def hold_lock(ws, deadline, done=None):
    """Claim the verifier lock, waiting out a live one. '' when it is still held.

    The wait is bounded by the lock's own age-out and has to leave budget to verify with. It
    also ends the moment done() says the running stop has already proved this tree, which is
    the only reason to wait for it at all.
    """
    while True:
        token = claim_lock(ws)
        if token:
            return token
        if done and done():
            return ""
        left = lock_left(ws)
        if left <= 0 or remaining(deadline) - left < LOCK_FLOOR:
            return ""
        time.sleep(min(LOCK_POLL, left))


def touch_lock(ws, token):
    """Say this stop is still verifying, so a long run does not lose its own lock."""
    rec = hookpaths.read_json_file(lock_file(ws)) or {}
    if rec.get("token") == token:
        hookpaths.write_json_file(lock_file(ws), dict(rec, at=time.time()))


def drop_lock(ws, token):
    """Release the lock, but only the one this process wrote."""
    if not token or (hookpaths.read_json_file(lock_file(ws)) or {}).get("token") != token:
        return
    try:
        os.remove(lock_file(ws))
    except OSError:
        pass


def verifier_gap(ws, fp, names, deadline, skipped):
    """The verifier's verdict for this workspace. Workspaces it could not run land in skipped."""
    if cached_pass(ws, fp):
        return None  # this exact tree already passed, so running it again proves nothing
    steps = verifier_steps(ws)
    if not steps:
        return None
    if not trusted(ws):
        log(ws, "trust", "untrusted workspace, verifier not run")
        return "untrusted", UNTRUSTED_REASON.format(ws=ws, cmds=verifier_hint(steps),
                                                    settings=hookpaths.CLI_SETTINGS)
    token = hold_lock(ws, deadline, lambda: cached_pass(ws, fp))
    if not token:
        if cached_pass(ws, fp):
            return None  # the stop holding the lock passed this tree while this one waited
        # another stop is verifying this workspace and there is no budget to wait it out, so
        # this stop reports no verifier gap and the marker keeps the work on the books
        log(ws, "lock", "lock busy, verifier not run")
        hookpaths.write_pending(ws, [LOCK_BUSY_NOTE])
        skipped.add(ws)
        return None
    try:
        if cached_pass(ws, fp):
            return None  # a concurrent stop just ran it, and its pass covers this tree
        return run_verifier(ws, fp, names, steps, deadline, token)
    finally:
        drop_lock(ws, token)


def run_verifier(ws, fp, names, steps, deadline, token):
    found = verify(ws, names, steps, deadline, lambda: touch_lock(ws, token))
    if found is None:
        return None  # nothing was verified here, so there is no pass to record either
    if found[1] != 0:
        return "verifier", VERIFIER_REASON.format(
            label=found[0], ws=ws, lines="\n".join(found[2][-MAX_REPORTED:]))
    after = gitstate.fingerprint(ws) if fp is not None else None
    if after is not None:
        # untracked files the run itself left are its output, not a change anyone has to
        # account for; a tracked file it rewrote is not excused, and moves the tracked hash
        made = (gitstate.artifacts(ws) | (set(after.untracked) - set(fp.untracked)))
        gitstate.save_verified(ws, after, made & set(after.untracked))
    return None


def review_why(ws, fp, kind, ignore):
    """Why this kind of review does not cover the tree in front of us, or ''."""
    rec = gitstate.review(ws, kind)
    if not rec:
        return f"no {kind} has seen the current state"
    if rec.get("tracked") != fp.tracked:
        return f"tracked files changed after the {kind} run, so it did not see this tree"
    seen_now = gitstate.hash_map(rec.get("untracked"))
    # by content, not by name: a file rewritten after the run was never reviewed either
    fresh = sorted(rel for rel, digest in fp.untracked.items()
                   if rel not in ignore and seen_now.get(rel) != digest)
    return f"{fresh[0]} is new since the {kind} run" if fresh else ""


def review_gap(ws, fp, ignore):
    kinds = ["reviewer"]
    if os.path.isfile(os.path.join(ws, VISUAL_REL)):
        kinds.append("visual-qa")
    for kind in kinds:
        why = review_why(ws, fp, kind, ignore)
        if why:
            gap, subject = REVIEW_KINDS[kind]
            return gap, REVIEW_REASON.format(why=why, ws=ws, kind=kind, subject=subject)
    return None


def run_output(ws, fp, outside):
    """Untracked paths nobody has to account for: verifier output, and the run's own scratch."""
    if fp is None:
        return set()
    ignore = {rel for rel in fp.untracked
              if any(transcript.under(os.path.join(ws, rel), root) for root in outside if root)}
    return ignore | gitstate.artifacts(ws)


def leftover_gap(ws, cid, fp, ignore):
    """Backup files and caches this conversation left behind."""
    junk = [rel for rel in gitstate.new_untracked(cid, ws, fp)
            if is_junk(rel) and rel not in ignore]
    if not junk:
        return None
    return "leftovers", LEFTOVERS_REASON.format(paths=", ".join(sorted(junk)[:MAX_REPORTED]))


def workspace_gap(ws, cid, fp, role, outside, deadline, skipped):
    """The first piece of missing proof for one changed workspace, or None."""
    names = fp.names if fp is not None else None
    only_docs = docs_only(names)
    if not only_docs:
        found = verifier_gap(ws, fp, names, deadline, skipped)
        if found:
            return found
    docs = docs_findings(ws, names, deadline)
    if docs:
        return "docs", DOCS_REASON.format(lines="\n".join(docs[:MAX_REPORTED]))
    ignore = run_output(ws, fp, outside)  # read after the verifier ran, so its output is in
    found = leftover_gap(ws, cid, fp, ignore)
    if found:
        return found
    # a subagent answers to the run that spawned it, and a docs change is its own check
    if role or only_docs:
        return None
    if fp is None:
        return "no-git", NO_GIT_REASON.format(ws=ws)
    return review_gap(ws, fp, ignore)


def repo_roots(dirs):
    """The repository root of each directory, in order, without repeats."""
    roots, out = {}, []
    for parent in dirs:
        if parent not in roots:  # many calls share a directory, and each lookup is a git call
            top = gitstate.toplevel(parent)
            roots[parent] = os.path.abspath(top) if top else ""
        top = roots[parent]
        if top and os.path.isdir(top) and top not in out:
            out.append(top)
    return out


def worth_judging(cid, ws):
    """Is there anything in this derived workspace to judge? A read-only command leaves nothing."""
    fp = gitstate.fingerprint(ws)
    return fp is not None and (bool(work_names(fp.names)) or gitstate.moved(cid, ws, fp))


def derived_spaces(calls, outside, cid):
    """Repository roots the run worked in, for a stop that carries no workspace of its own."""
    wrote = repo_roots(os.path.dirname(path)
                       for path in transcript.written_paths(calls, outside))
    # a shell write names no file, so its working directory is the only pointer it leaves. A
    # directory above a workspace an edit already named is that workspace's parent, not another.
    ran = [ws for ws in repo_roots(transcript.run_dirs(calls, outside))
           if not any(transcript.under(named, ws) for named in wrote)]
    # a read-only command must not pull an untouched repository into the judged set
    return [ws for ws in wrote + ran if worth_judging(cid, ws)]


def workspaces(ev, calls, outside, cid):
    """(workspaces to judge, payload paths dropped because they do not resolve)."""
    good, dropped = [], []
    for raw in ev.get("workspacePaths") or []:
        ws = os.path.abspath(hookpaths.real_path(raw))
        if os.path.isdir(ws):
            good.append(ws)
        else:
            dropped.append(raw)
    return (good or derived_spaces(calls, outside, cid)), dropped


def clear_markers(cid, spaces, prints):
    """Drop a pending marker where the gate proved this tree, or where the work is gone."""
    for ws in spaces:
        reverted = gitstate.seen(cid, ws) and not gitstate.moved(cid, ws, prints[ws])
        if cached_pass(ws, prints[ws]) or reverted:
            hookpaths.clear_pending(ws)


def first_gap(spaces, find):
    """(gap, workspace it belongs to) for the first workspace that has one."""
    for ws in spaces:
        gap = find(ws)
        if gap:
            return gap, ws
    return None, (spaces[0] if spaces else "")


def release_reason(kind, ledger, message):
    """What the terminal is told when the gate lets an unmet stop through."""
    if kind in NOT_CHECKED:
        return message  # nothing ran and nothing was forced, so retry wording would be a lie
    if hookpaths.forced_count(ledger) >= hookpaths.MAX_FORCED:
        return CEILING_NOTE.format(n=hookpaths.MAX_FORCED, summary=message)
    return HUMAN_NOTE.format(summary=message)


def answer(gap, culprit, cid, fp, spaces, deadline, detail, tag=""):
    """Force one more turn for this gap, or release the stop and leave a marker."""
    kind, message = gap
    budget = f"{int(max(0, remaining(deadline)))}s of the {GATE_BUDGET}s gate budget left."
    # with no conversation id the ledger is this workspace and this tree, so the gate still
    # blocks once without two such stops sharing one ceiling
    ledger = cid or f"workspace {hookpaths.ws_key(culprit)} {tag}"
    allowance = f"{kind}:{gitstate.digest(fp) or 'nogit'}"
    if kind not in NOT_CHECKED and hookpaths.take_retry(ledger, allowance):
        return {"decision": "continue", "reason": message + "\n" + budget}, kind, detail, spaces
    hookpaths.write_pending(culprit, [message])
    return ({"decision": "stop", "reason": release_reason(kind, ledger, message)},
            "release", f"{kind} {detail}", spaces)


def decide(ev, deadline):
    """(result, log kind, log detail, workspaces)."""
    tpath = hookpaths.real_path(ev.get("transcriptPath") or "")
    artifact = hookpaths.real_path(ev.get("artifactDirectoryPath") or "")
    brain = transcript.brain_root(tpath)
    cid = transcript.conversation_id(ev)
    steps = transcript.load(tpath)
    calls = transcript.calls(steps)
    outside = (artifact, brain)
    spaces, dropped = workspaces(ev, calls, outside, cid)
    if dropped:
        log(",".join(dropped), "unresolved", "workspace path dropped")
    prints = {ws: gitstate.fingerprint(ws) for ws in spaces}
    role = transcript.subagent_type(cid, brain)
    known = {ws: bool(gitstate.seen(cid, ws)) for ws in spaces}  # read before the fallback lands
    # reinforce.py takes the baseline at PreInvocation. Reaching a Stop without one means the
    # turn's work has already happened, so the record is marked as the fallback it is.
    for ws in spaces:
        gitstate.note_seen(cid, ws, prints[ws], fallback=True)

    reason = ev.get("terminationReason")
    finished = reason in MODEL_FINISHED

    if role in transcript.JUDGE_TYPES:
        # a judge owes no verifier run and no review: it records what it saw, but it still
        # has to clean up after itself. A run that was cut short judged nothing.
        if not finished:
            return ({"decision": "stop"}, "review",
                    f"{role} recorded nothing, reason={reason!r}", spaces)
        kept = [ws for ws in spaces if gitstate.save_review(cid, ws, role, prints[ws])]
        detail = f"{role} recorded {len(kept)}/{len(spaces)}"
        gap, culprit = first_gap(spaces, lambda ws: leftover_gap(
            ws, cid, prints[ws], run_output(ws, prints[ws], outside)))
        if not gap:
            return {"decision": "stop"}, "review", detail, spaces
        return answer(gap, culprit, cid, prints[culprit], spaces, deadline, detail)

    if not spaces:
        return {"decision": "stop"}, "no-workspace", "no workspace to judge", spaces

    # with only a fallback record the gate cannot date the dirt it finds, so a call that could
    # have written counts as work in every workspace of this stop. No transcript is not a call.
    worked = transcript.did_work(calls)
    edited = {ws: transcript.edits(calls, ws, outside) for ws in spaces}
    dirty = [ws for ws in spaces
             if touched(ws, cid, prints[ws], worked, edited[ws],
                        steps is None and not known[ws])]

    if not dirty:
        clear_markers(cid, spaces, prints)
        return {"decision": "stop"}, "stop", "unchanged", spaces

    if not finished:
        # the run did not choose to finish, so the gate leaves a marker rather than spend a
        # verifier run on work the user just interrupted
        for ws in dirty:
            hookpaths.write_pending(ws, [UNFINISHED_NOTE.format(reason=reason)])
        return ({"decision": "stop"}, "pending",
                f"reason={reason!r} changed={len(dirty)}", spaces)

    skipped = set()
    gap, culprit = first_gap(dirty, lambda ws: workspace_gap(
        ws, cid, prints[ws], role, outside, deadline, skipped))
    if not gap:
        # nothing is missing here, except where the verifier could not run: that stays on the books
        for ws in dirty:
            if ws not in skipped:
                hookpaths.clear_pending(ws)
        return {"decision": "stop"}, "stop", "verified", spaces
    return answer(gap, culprit, cid, prints[culprit], spaces, deadline,
                  f"{role or 'top-level'} {culprit}",
                  tag=tpath or gitstate.digest(prints[culprit]))


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
