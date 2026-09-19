#!/usr/bin/env python3
"""Unit tests for Antigravity hooks."""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent


def hooks_module(name):
    """Import a hook module for the tests that call into it directly."""
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(HOOKS_DIR))
HEADER = ("Created At: 2026-09-18T00:00:00-07:00\n"
          "Completed At: 2026-09-18T00:00:01-07:00\n\n")
RESULT = HEADER + "The command exited with code {code}.\n"
VERIFIER_LINE = "python -m pytest -q"


def one_call(event, root):
    """(tool call, result step) for one shorthand transcript event."""
    kind = event[0]
    if kind == "edit":
        call = {"name": "write_to_file",
                "args": {"TargetFile": os.path.join(root, event[1].replace("/", os.sep))}}
        return call, {"content": RESULT.format(code=0)}
    if kind == "verifier":
        return one_call(("run", VERIFIER_LINE, 0 if event[1] else 1), root)
    if kind == "run":
        call = {"name": "run_command",
                "args": {"CommandLine": event[1], "Cwd": event[3] if len(event) > 3 else root}}
        return call, {"content": RESULT.format(code=event[2])}
    if kind == "background":
        # a backgrounded run answers before it finishes, so it never reports an exit code
        call = {"name": "run_command", "args": {"CommandLine": VERIFIER_LINE, "Cwd": root}}
        return call, {"content": HEADER + "Command is running in the background.\n"}
    if kind == "spawn":
        call = {"name": "invoke_subagent",
                "args": {"Subagents": [{"TypeName": event[1], "Prompt": event[2],
                                        "Role": event[3] if len(event) > 3 else "Worker"}]}}
        return call, {"content": RESULT.format(code=0)}
    if kind == "spawn_error":
        call, _result = one_call(("spawn", event[1], "Review `git diff HEAD`"), root)
        return call, {"content": HEADER + "subagent failed to start\n", "status": "ERROR"}
    raise AssertionError(f"unknown transcript event {kind}")


def write_transcript(path, events, root):
    """Write a transcript of shorthand events, the way agy records steps and results.

    ("noresult", event) writes the call with no result step after it, and
    ("multi", event, event) puts both calls in one step.
    """
    steps = []

    def add(step):
        step["step_index"] = len(steps) * 2  # agy leaves gaps in step_index
        steps.append(step)

    for event in events:
        if event[0] == "multi":
            made = [one_call(inner, root) for inner in event[1:]]
            add({"type": "PLANNER_RESPONSE", "tool_calls": [c for c, _r in made]})
            for _call, result in made:
                add({"type": "GENERIC", **result})
            continue
        if event[0] == "noresult":
            call, _result = one_call(event[1], root)
            add({"type": "PLANNER_RESPONSE", "tool_calls": [call]})
            continue
        if event[0] == "interrupted":
            # a hook message lands between the call and its result in real transcripts
            call, result = one_call(event[1], root)
            add({"type": "PLANNER_RESPONSE", "tool_calls": [call]})
            add({"type": "EPHEMERAL_MESSAGE", "content": "injected reminder"})
            add({"type": "GENERIC", **result})
            continue
        call, result = one_call(event, root)
        add({"type": "PLANNER_RESPONSE", "tool_calls": [call]})
        add({"type": "GENERIC", **result})
    Path(path).write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    return str(path)


class TestHooks(unittest.TestCase):
    def test_reinforce_hook(self):
        script = HOOKS_DIR / "reinforce.py"
        payload = json.dumps({"conversationId": "test-cid"})
        proc = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout)
        self.assertIn("injectSteps", res)
        self.assertTrue(len(res["injectSteps"]) > 0)
        self.assertIn("CAVEMAN", res["injectSteps"][0]["ephemeralMessage"])

    def reinforce(self, num):
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "reinforce.py")],
            input=json.dumps({"invocationNum": num}), text=True, capture_output=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)["injectSteps"]

    def test_banner_is_skipped_between_milestones(self):
        for num in (2, 5, 9, 11):
            self.assertEqual(self.reinforce(num), [], f"invocationNum {num}")

    def test_banner_returns_every_tenth_invocation(self):
        for num in (10, 20):
            self.assertIn("CAVEMAN", self.reinforce(num)[0]["ephemeralMessage"])

    def test_deny_circuit_breaker(self):
        script = HOOKS_DIR / "deny_circuit_breaker.py"
        payload = json.dumps({
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "ls"}
            },
            "conversationId": "test-session"
        })
        proc = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout)
        self.assertEqual(res.get("decision"), "allow")


def gate(script, tool, args, extra=None):
    """Decision a PreToolUse hook returns for one tool call."""
    payload = {"toolCall": {"name": tool, "args": args}}
    payload.update(extra or {})
    proc = subprocess.run([sys.executable, str(HOOKS_DIR / script)],
                          input=json.dumps(payload), text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def command(script, line):
    return gate(script, "run_command", {"CommandLine": line})


class TestCommitGate(unittest.TestCase):
    def deny_reason(self, line):
        res = command("commit_gate.py", line)
        self.assertEqual(res.get("decision"), "deny", line)
        return res.get("reason", "")

    def assertAllowed(self, line):
        self.assertEqual(command("commit_gate.py", line).get("decision"), "allow", line)

    def test_conventional_subject_allowed(self):
        self.assertAllowed('git commit -m "feat(core): add new feature"')

    def test_non_conventional_subject_denied(self):
        self.assertIn("not Conventional Commits", self.deny_reason('git commit -m "Fixed the bug"'))

    def test_long_subject_denied(self):
        self.assertIn("limit 50", self.deny_reason('git commit -m "feat: ' + "a" * 50 + '"'))

    def test_no_verify_denied_on_commit_push_and_merge(self):
        for line in ('git commit --no-verify -m "fix: x"',
                     "git push --no-verify origin feat-x",
                     "git merge --no-verify feat-x"):
            self.assertIn("--no-verify", self.deny_reason(line))

    def test_short_no_verify_denied_on_commit(self):
        self.assertIn("--no-verify", self.deny_reason('git commit -n -m "fix: x"'))

    def test_dry_run_push_allowed(self):
        """-n means --dry-run for push, so it is not a skipped gate."""
        self.assertAllowed("git push -n origin main")

    def test_force_push_to_main_denied(self):
        self.assertIn("Force push", self.deny_reason("git push --force origin main"))

    def test_force_push_without_a_branch_denied(self):
        self.assertIn("Force push", self.deny_reason("git push -f"))

    def test_force_push_to_own_branch_allowed(self):
        self.assertAllowed("git push --force-with-lease origin feat-discipline")

    def test_unrelated_command_allowed(self):
        self.assertAllowed("git status --short")


class TestClockWaitGate(unittest.TestCase):
    def decision(self, line):
        return command("clock_wait_gate.py", line).get("decision")

    def test_bare_sleep_denied(self):
        self.assertEqual(self.decision("sleep 30"), "deny")

    def test_windows_timeout_denied(self):
        self.assertEqual(self.decision("timeout /t 10"), "deny")

    def test_powershell_sleep_denied(self):
        self.assertEqual(self.decision("powershell -c Start-Sleep -Seconds 5"), "deny")

    def test_ping_delay_denied(self):
        self.assertEqual(self.decision("ping -n 5 127.0.0.1 > nul"), "deny")

    def test_polling_loop_denied(self):
        self.assertEqual(self.decision("until curl -sf localhost:8080; do sleep 5; done"), "deny")

    def test_bounded_polling_loop_still_denied(self):
        """A timeout wrapper caps a wait; it does not make polling the right tool."""
        self.assertEqual(
            self.decision("timeout 300 bash -c 'until curl -sf localhost; do sleep 5; done'"),
            "deny")

    def test_sleep_inside_a_timeout_wrapper_allowed(self):
        self.assertEqual(self.decision("timeout 60 bash -c 'sleep 5; ./check.sh'"), "allow")

    def test_npm_script_named_sleep_allowed(self):
        self.assertEqual(self.decision("npm run sleep-test"), "allow")

    def test_deny_reason_names_the_replacement(self):
        self.assertIn("command_status", command("clock_wait_gate.py", "sleep 5").get("reason", ""))

    def test_other_tools_are_not_judged(self):
        res = gate("clock_wait_gate.py", "write_to_file",
                   {"TargetFile": "/proj/a.py", "CodeContent": "sleep 5"})
        self.assertEqual(res.get("decision"), "allow")


class TestNoAiMentions(unittest.TestCase):
    def write(self, path, content):
        return gate("no_ai_mentions.py", "write_to_file",
                    {"TargetFile": path, "CodeContent": content})

    def test_attribution_in_source_denied(self):
        res = self.write("/proj/src/app.py", "# written by an AI agent\nx = 1\n")
        self.assertEqual(res.get("decision"), "deny")

    def test_tool_name_in_source_denied(self):
        self.assertEqual(self.write("/proj/src/app.py", "# ask Claude\n").get("decision"), "deny")

    def test_readme_may_name_the_tools(self):
        self.assertEqual(self.write("/proj/README.md", "Runs under Antigravity and Gemini.")
                         .get("decision"), "allow")

    def test_agents_directory_is_exempt(self):
        self.assertEqual(self.write("/proj/.agents/visual.md", "Gemini opens each page.")
                         .get("decision"), "allow")

    def test_sdk_glue_may_name_the_vendor(self):
        self.assertEqual(self.write("/proj/sdk/gemini_client.py", "import gemini\n")
                         .get("decision"), "allow")

    def test_ordinary_source_allowed(self):
        self.assertEqual(self.write("/proj/src/app.py", "def add(a, b):\n    return a + b\n")
                         .get("decision"), "allow")

    def test_commit_message_attribution_denied(self):
        res = command("no_ai_mentions.py", 'git commit -m "feat: add gate" -m "Generated with x"')
        self.assertEqual(res.get("decision"), "deny")

    def test_co_authored_bot_trailer_denied(self):
        res = command("no_ai_mentions.py",
                      'git commit -m "feat: add gate\n\nCo-authored-by: helper bot <b@x.dev>"')
        self.assertEqual(res.get("decision"), "deny")

    def test_command_without_a_message_is_not_judged(self):
        self.assertEqual(command("no_ai_mentions.py", "ls ~/.gemini/config").get("decision"),
                         "allow")

    def test_private_key_block_denied(self):
        res = self.write("/proj/src/keys.py", "KEY = '''-----BEGIN PRIVATE KEY-----'''\n")
        self.assertIn("credential", res.get("reason", ""))

    def test_credential_check_applies_to_exempt_paths_too(self):
        res = self.write("/proj/README.md", "-----BEGIN RSA PRIVATE KEY-----\n")
        self.assertIn("credential", res.get("reason", ""))


class TestWriteGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, path, content=""):
        return gate("write_gate.py", "write_to_file",
                    {"TargetFile": path, "CodeContent": content})

    def existing(self, name, content="{}\n"):
        path = Path(self.tmp) / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_backup_suffix_denied(self):
        for name in ("config.bak", "app.py.orig", "notes.old", "run.tmp"):
            self.assertEqual(self.write(f"/proj/{name}").get("decision"), "deny", name)

    def test_copy_names_denied(self):
        for name in ("app-v2.py", "app_backup.py", "report - copy.md"):
            self.assertEqual(self.write(f"/proj/{name}").get("decision"), "deny", name)

    def test_ordinary_file_allowed(self):
        self.assertEqual(self.write("/proj/src/app.py", "x = 1\n").get("decision"), "allow")

    def test_existing_lint_config_denied(self):
        res = self.write(self.existing("tsconfig.json"))
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("config protected", res.get("reason", ""))

    def test_new_lint_config_allowed(self):
        self.assertEqual(self.write(str(Path(self.tmp) / ".eslintrc.json")).get("decision"),
                         "allow")

    def test_ruff_section_in_pyproject_denied(self):
        path = self.existing("pyproject.toml", "[project]\nname = 'x'\n")
        res = self.write(path, "[tool.ruff]\nline-length = 200\n")
        self.assertEqual(res.get("decision"), "deny")

    def test_pyproject_without_tool_sections_allowed(self):
        path = self.existing("pyproject.toml", "[project]\nname = 'x'\n")
        self.assertEqual(self.write(path, "[project]\nversion = '2'\n").get("decision"), "allow")

    def test_read_tools_are_not_judged(self):
        res = gate("write_gate.py", "view_file", {"AbsolutePath": "/proj/config.bak"})
        self.assertEqual(res.get("decision"), "allow")


class GateCase(unittest.TestCase):
    """Drives the hooks over a real git repository. Nothing here mocks git."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.new_repo()
        self.trusted = [self.repo]

    def sh(self, root, *args):
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def new_repo(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        self.sh(root, "init", "-q")
        self.sh(root, "config", "user.email", "gate@example.invalid")
        self.sh(root, "config", "user.name", "gate")
        self.write("app.py", "x = 1\n", root=root)
        self.write(".gitignore", "scratch/\n", root=root)
        self.commit(root)
        return root

    def commit(self, root=None, message="change"):
        root = root or self.repo
        self.sh(root, "add", "-A")
        self.sh(root, "commit", "-q", "-m", message)

    def write(self, rel, text, root=None):
        path = Path(root or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def env(self):
        settings = Path(self.tmp) / "cli-settings.json"
        settings.write_text(json.dumps({"trustedWorkspaces": self.trusted}), encoding="utf-8")
        return dict(os.environ, GEMINI_HOOK_TMP=self.tmp, GEMINI_CLI_SETTINGS=str(settings))

    def hook(self, script, payload):
        proc = subprocess.run([sys.executable, str(HOOKS_DIR / script)],
                              input=json.dumps(payload), text=True, capture_output=True,
                              env=self.env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def cid(self):
        """One conversation per test, so each gets its own retry allowance and baseline."""
        return self.id().rsplit(".", 1)[-1].replace("_", "-")

    def payload(self, **kw):
        base = {"terminationReason": "NO_TOOL_CALL", "fullyIdle": True,
                "conversationId": self.cid(), "workspacePaths": [self.repo]}
        base.update(kw)
        return base

    def turn(self, cid=None, spaces=None):
        """A PreInvocation: where a conversation's baseline fingerprint is taken."""
        return self.hook("reinforce.py", {"invocationNum": 1, "conversationId": cid or self.cid(),
                                          "workspacePaths": spaces or [self.repo]})

    def stop(self, **kw):
        return self.hook("stop_gate.py", self.payload(**kw))

    def gap(self, **kw):
        """The reason a refused stop carries."""
        res = self.stop(**kw)
        self.assertEqual(res.get("decision"), "continue", res)
        return res["reason"]

    def log(self):
        path = Path(self.tmp) / "stop_gate.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def markers(self):
        return list((Path(self.tmp) / "pending").glob("*.json"))

    def verifier(self, code=0, root=None, writes=""):
        """A real verify script, committed so the script itself is not a pending change.

        writes names a file in the workspace the run rewrites every time, the way a coverage
        report or a generated file does.
        """
        root = Path(root or self.repo)
        counter = str(Path(self.tmp) / "ran.txt")
        if os.name == "nt":
            body = f'@echo ran>>"{counter}"\r\n'
            if writes:
                body += f'@echo output>"{writes}"\r\n'
            self.write(".agents/verify.cmd", body + f"@exit /b {code}\r\n", root=root)
            label = ".agents/verify.cmd"
        else:
            body = f'echo ran >> "{counter}"\n'
            if writes:
                body += f'echo output > "{writes}"\n'
            self.write(".agents/verify.sh", body + f"exit {code}\n", root=root)
            label = ".agents/verify.sh"
        self.commit(str(root), "verifier")
        return label

    def runs(self):
        """How many times the verify script has run."""
        path = Path(self.tmp) / "ran.txt"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

    def store_review(self, ws, kind="reviewer"):
        """A reviewed record for a workspace, written by the gate's own record code."""
        code = ("import sys, time, gitstate, hookpaths;"
                "hookpaths.write_json_file("
                "gitstate.record_path(gitstate.REVIEWED, sys.argv[1], sys.argv[2]),"
                "{'at': time.time(), 'workspace': sys.argv[1], 'tracked': 'x', 'untracked': []})")
        proc = subprocess.run([sys.executable, "-c", code, ws, kind], cwd=str(HOOKS_DIR),
                              env=self.env(), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def subagent(self, kind, cid=None, spaces=None, before=None, turn=True):
        """A subagent of this type: its brain record, its PreInvocation, its own Stop.

        turn=False is a subagent whose PreInvocation never fired, so it has no baseline.
        """
        cid = cid or f"{kind}-cid"
        logs = Path(self.tmp) / "brain" / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "transcript.jsonl").write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT"}) + "\n", encoding="utf-8")
        records = Path(self.tmp) / "brain" / "parent-cid" / ".system_generated" / "subagents"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{cid}.json").write_text(json.dumps(
            {"conversationId": cid, "subagentDescriptor": {"typeName": kind},
             "state": "SUBAGENT_STATE_ALIVE"}), encoding="utf-8")
        if turn:
            self.turn(cid=cid, spaces=spaces)
        if before:
            before()
        return self.hook("stop_gate.py", self.payload(
            conversationId=cid, transcriptPath=str(logs / "transcript.jsonl"),
            workspacePaths=spaces or [self.repo]))

    def review(self, **kw):
        res = self.subagent("reviewer", **kw)
        self.assertEqual(res.get("decision"), "stop", res)
        return res

    def transcript(self, *events, root=None):
        return write_transcript(Path(self.tmp) / "t.jsonl", events, root or self.repo)


class TestChangeDetection(GateCase):
    """Every gaming vector two review rounds found, judged by the git fingerprint instead."""

    NO_REVIEW = "no reviewer has seen the current state"

    def test_a_shell_redirect_is_a_change(self):
        """echo x>app.py: no edit tool named the file, and the transcript says nothing."""
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_copying_a_file_is_a_change(self):
        self.verifier()
        self.turn()
        shutil.copy(Path(self.repo) / "app.py", Path(self.repo) / "app2.py")
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_deleting_a_file_is_a_change(self):
        self.verifier()
        self.turn()
        os.remove(Path(self.repo) / "app.py")
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_resetting_the_branch_is_a_change(self):
        """A clean tree at a different commit: the old gate saw nothing to check."""
        self.verifier()
        self.write("app.py", "x = 2\n")
        self.commit()
        self.turn()
        self.sh(self.repo, "reset", "--hard", "-q", "HEAD~1")
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_a_write_from_outside_the_workspace_is_a_change(self):
        """The command ran somewhere else; the file it wrote is still in this tree."""
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        self.verifier()
        self.turn()
        self.write("app.py", "x = 3\n")
        path = self.transcript(("run", "python -c \"open('app.py','w')\"", 0, other), root=other)
        self.assertIn(self.NO_REVIEW, self.gap(transcriptPath=path))

    def test_a_claimed_pass_does_not_beat_the_real_verifier(self):
        """pytest || true in the transcript, against a verifier that really fails."""
        label = self.verifier(code=1)
        self.turn()
        self.write("app.py", "x = 2\n")
        path = self.transcript(("run", "python -m pytest -q || true", 0))
        self.assertIn(f"Verifier failed ({label})", self.gap(transcriptPath=path))

    def test_listing_the_verifier_is_not_running_it(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        path = self.transcript(("run", "ls .agents/verify.sh", 0))
        self.assertIn(self.NO_REVIEW, self.gap(transcriptPath=path))
        self.assertEqual(self.runs(), 1, "the gate ran the verifier itself")

    def test_a_pass_in_one_workspace_does_not_cover_another(self):
        other = self.new_repo()
        self.trusted = [self.repo, other]
        self.verifier()
        self.verifier(root=other)
        self.turn(spaces=[self.repo, other])
        self.write("lib.py", "y = 1\n", root=other)
        reason = self.gap(workspacePaths=[self.repo, other])
        self.assertIn(other, reason)
        self.assertNotIn(self.repo, reason)

    def test_ignoring_a_new_file_does_not_hide_it(self):
        """Adding it to .gitignore rewrites a tracked file, so the fingerprint moves anyway."""
        self.verifier()
        self.turn()
        self.review()
        self.write("sneaky.py", "y = 2\n")
        with open(Path(self.repo) / ".gitignore", "a", encoding="utf-8") as fh:
            fh.write("sneaky.py\n")
        self.assertIn("tracked files changed after the reviewer run", self.gap())

    def test_an_edit_after_the_review_is_stale(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        self.write("app.py", "x = 3\n")
        self.assertIn("tracked files changed after the reviewer run", self.gap())

    def test_a_new_file_after_the_review_is_stale(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        self.write("extra.py", "y = 1\n")
        self.assertIn("extra.py is new since the reviewer run", self.gap())

    def test_dirt_from_before_the_conversation_is_not_this_conversations_work(self):
        """263 changed paths were waiting in one workspace: a question there costs one fingerprint."""
        self.verifier()
        self.write("app.py", "x = 2\n")
        self.write("half-done.py", "y = 1\n")
        self.turn()
        path = self.transcript(("run", "git status --short", 0))
        res = self.stop(transcriptPath=path)
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0, "an unmoved fingerprint owes no verifier run")
        self.assertIn("unchanged", self.log())

    def test_committing_the_work_is_still_work(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.commit()
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_stashing_the_work_is_still_work(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.sh(self.repo, "stash", "-q")
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_a_stop_without_a_baseline_reads_an_edit_call_as_work(self):
        """No PreInvocation fired, so the gate cannot date the dirt and fails closed."""
        self.verifier()
        self.write("app.py", "x = 2\n")
        path = self.transcript(("edit", "app.py"))
        payload = self.payload(transcriptPath=path)
        payload.pop("conversationId")
        self.assertIn(self.NO_REVIEW, self.hook("stop_gate.py", payload)["reason"])

    def test_a_stop_without_a_baseline_and_without_a_tool_call_is_released(self):
        self.verifier()
        self.write("app.py", "x = 2\n")
        path = self.transcript(("spawn", "researcher", "find the API docs"))
        payload = self.payload(transcriptPath=path)
        payload.pop("conversationId")
        res = self.hook("stop_gate.py", payload)
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)

    def test_a_pre_invocation_without_a_conversation_id_still_writes_a_baseline(self):
        self.verifier()
        logs = Path(self.tmp) / "brain" / "cid-from-path" / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        tpath = write_transcript(logs / "transcript.jsonl", [("run", "git log -1", 0)], self.repo)
        self.hook("reinforce.py", {"invocationNum": 1, "transcriptPath": tpath,
                                  "workspacePaths": [self.repo]})
        records = list((Path(self.tmp) / "seen").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertFalse(json.loads(records[0].read_text(encoding="utf-8"))["fallback"])
        # that baseline is what lets the stop see an untouched tree instead of failing closed
        payload = self.payload(transcriptPath=tpath)
        payload.pop("conversationId")
        res = self.hook("stop_gate.py", payload)
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)

    def test_a_reviewer_without_a_baseline_records_nothing(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        res = self.subagent("reviewer", turn=False,
                            before=lambda: self.write("app.py", "x = 3\n"))
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertIn("reviewer recorded 0/1", self.log())
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_a_file_the_reviewer_created_is_new_since_the_review(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review(before=lambda: self.write("new_feature.py", "def f():\n    return 1\n"))
        self.assertIn("new_feature.py is new since the reviewer run", self.gap())

    def test_a_judge_that_leaves_a_backup_file_is_told(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        res = self.subagent("reviewer", before=lambda: self.write("notes.bak", "old\n"))
        self.assertEqual(res.get("decision"), "continue", res)
        self.assertIn("delete leftovers: notes.bak", res["reason"])

    def test_output_the_verifier_writes_every_run_is_not_a_change(self):
        self.verifier(writes="coverage.txt")
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        self.assertEqual(self.stop().get("decision"), "stop")
        self.assertEqual(self.runs(), 1)
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 1, "verifier output must not read as a changed tree")

    def test_a_verifier_that_rewrites_a_tracked_file_is_not_excused(self):
        self.verifier(writes="app.py")
        self.turn()
        self.write("lib.py", "y = 1\n")
        self.review()
        self.assertEqual(self.stop().get("decision"), "stop", "the tree it reviewed passed")
        self.assertIn("tracked files changed after the reviewer run", self.gap())

    def test_forced_continues_stop_at_the_ceiling(self):
        self.verifier(code=1)
        self.turn()
        decisions = []
        for n in range(2, 7):
            self.write("app.py", f"x = {n}\n")  # a moved fingerprint earns a fresh allowance
            res = self.stop()
            decisions.append(res.get("decision"))
        self.assertEqual(decisions, ["continue", "continue", "continue", "stop", "stop"])
        self.assertIn("already been sent back 3 times", res["reason"])
        self.assertEqual(len(self.markers()), 1)

    def test_a_stop_without_a_conversation_id_still_blocks_once(self):
        self.verifier()
        self.write("app.py", "x = 2\n")
        payload = self.payload(conversationId="")
        self.assertEqual(self.hook("stop_gate.py", payload).get("decision"), "continue")
        self.assertEqual(self.hook("stop_gate.py", payload).get("decision"), "stop")

    def test_a_stored_review_gives_no_credit_without_git(self):
        plain = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, plain, True)
        self.trusted = [plain]
        self.store_review(plain)
        path = self.transcript(("edit", "app.py"), root=plain)
        reason = self.gap(workspacePaths=[plain], transcriptPath=path)
        self.assertIn("cannot be proved without git", reason)

    def test_a_reviewer_that_edits_a_tracked_file_is_not_a_review(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review(before=lambda: self.write("app.py", "x = 3\n"))
        self.assertIn("reviewer recorded 0/1", self.log())
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_another_subagent_type_is_never_a_review(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        res = self.subagent("coder")
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertIn(self.NO_REVIEW, self.gap())

    def test_a_subagent_owes_the_verifier_but_no_review(self):
        label = self.verifier(code=1)
        self.turn()
        res = self.subagent("coder", before=lambda: self.write("app.py", "x = 2\n"))
        self.assertEqual(res.get("decision"), "continue", res)
        self.assertIn(f"Verifier failed ({label})", res["reason"])

    def test_empty_workspace_paths_come_from_the_files_edited(self):
        """183 of 430 logged stops carried no workspacePaths."""
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        path = self.transcript(("edit", "app.py"))
        reason = self.gap(workspacePaths=[], transcriptPath=path)
        self.assertIn(os.path.basename(self.repo), reason)

    def test_the_transcript_path_names_the_conversation(self):
        """No conversationId in the payload, so the retry allowance comes from brain/<cid>/."""
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        payload = self.payload()
        payload.pop("conversationId")
        spent = []
        for cid in ("cid-a", "cid-a", "cid-b"):
            logs = Path(self.tmp) / "brain" / cid / ".system_generated" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            write_transcript(logs / "transcript.jsonl", [("edit", "app.py")], self.repo)
            payload["transcriptPath"] = str(logs / "transcript.jsonl")
            spent.append(self.hook("stop_gate.py", dict(payload)).get("decision"))
        self.assertEqual(spent, ["continue", "stop", "continue"])

    def test_a_garbage_retry_file_counts_as_spent(self):
        self.verifier()
        self.turn(cid="garbagecid")
        self.write("app.py", "x = 2\n")
        path = Path(self.tmp) / "retries" / os.path.basename(
            hooks_module("hookpaths").retry_path("garbagecid"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        res = self.stop(conversationId="garbagecid")
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertIn("release", self.log())

    def test_a_pre_existing_empty_directory_is_not_a_leftover(self):
        self.verifier()
        (Path(self.repo) / "half-done").mkdir()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)

    def test_a_new_backup_file_is_a_leftover(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.write("notes.bak", "old\n")
        self.review()
        self.assertIn("delete leftovers: notes.bak", self.gap())

    def test_an_untracked_file_from_before_the_run_is_not_a_leftover(self):
        self.verifier()
        self.write("stale.bak", "old\n")
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        self.assertEqual(self.stop().get("decision"), "stop")

    def test_an_untrusted_workspace_runs_no_code(self):
        label = self.verifier()
        self.trusted = []
        self.turn()
        self.write("app.py", "x = 2\n")
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)
        self.assertIn("not checked, workspace is not trusted", res["reason"].lower())
        self.assertIn("not in trustedWorkspaces", res["reason"])
        self.assertIn(label, res["reason"])
        self.assertNotIn("forced retry", res["reason"], "nothing was retried, so say so")

    def test_the_same_fingerprint_is_not_verified_twice(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        self.assertEqual(self.stop().get("decision"), "stop")
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.stop().get("decision"), "stop")
        self.assertEqual(self.runs(), 1, "a verified fingerprint is cached")

    def test_source_under_an_agents_directory_is_not_docs_only(self):
        self.verifier()
        self.turn()
        self.write("src/agents/x.py", "def f():\n    return 1\n")
        self.assertIn(self.NO_REVIEW, self.gap())
        self.assertEqual(self.runs(), 1, "source owes a verifier run")

    def test_verified_and_reviewed_work_may_stop(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertIn("verified", self.log())

    def test_a_file_the_reviewer_created_in_its_artifact_directory_is_not_a_change(self):
        """A judge's own scratch lives in its artifact directory, which nobody has to account for."""
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        artifact = Path(self.repo) / "brain" / "reviewer-cid"
        self.review(before=lambda: self.write("brain/reviewer-cid/notes.md", "# notes\n"))
        res = self.stop(artifactDirectoryPath=str(artifact))
        self.assertEqual(res.get("decision"), "stop", res)

    def test_a_reviewer_scratch_file_in_an_ignored_directory_still_passes(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review(before=lambda: self.write("scratch/diff.txt", "diff --git\n"))
        self.assertEqual(self.stop().get("decision"), "stop")

    def test_a_visual_spec_also_needs_a_visual_qa_run(self):
        self.verifier()
        self.write(".agents/visual.md", "## Pages\n\nhttp://localhost:5173/\n\n"
                                        "## Accept\n\n- Nav is visible\n")
        self.commit()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.review()
        reason = self.gap()
        self.assertIn("TypeName visual-qa", reason)
        res = self.subagent("visual-qa")
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.stop().get("decision"), "stop")

    def test_a_bad_instruction_doc_blocks_without_a_verifier_run(self):
        self.verifier()
        self.turn()
        self.write("GEMINI.md", "# Rules\n\n- See `~/.gemini/nope-does-not-exist` for details.\n")
        reason = self.gap()
        self.assertIn("ai-docs-lint failed in files you changed:", reason)
        self.assertIn("dead-path: ~/.gemini/nope-does-not-exist", reason)
        self.assertEqual(self.runs(), 0, "an instruction doc owes no verifier run")

    def test_a_clean_instruction_doc_needs_no_review(self):
        self.verifier()
        self.turn()
        self.write("GEMINI.md", "# Rules\n\n- Keep it short.\n")
        self.assertEqual(self.stop().get("decision"), "stop")

    def test_the_gates_own_answer_file_is_not_a_change(self):
        self.verifier()
        self.turn()
        self.write(".agents/DECISIONS.md", "2026-09-18 chose X because Y\n")
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)

    def test_an_unchanged_workspace_stops_at_once(self):
        self.verifier()
        self.turn()
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)
        self.assertIn("unchanged", self.log())

    def test_not_fully_idle_still_runs_the_verifier(self):
        label = self.verifier(code=1)
        self.turn()
        self.write("app.py", "x = 2\n")
        self.assertIn(f"Verifier failed ({label})", self.gap(fullyIdle=False))

    def test_user_canceled_leaves_a_marker_and_runs_nothing(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        res = self.stop(terminationReason="USER_CANCELED")
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.runs(), 0)
        self.assertEqual(len(self.markers()), 1)
        self.assertIn("pending", self.log())

    def test_a_repeated_gap_is_released_with_a_message_for_the_human(self):
        self.verifier(code=1)
        self.turn()
        self.write("app.py", "x = 2\n")
        self.assertEqual(self.stop().get("decision"), "continue")
        res = self.stop()
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("Stop allowed after one forced retry", res["reason"])
        self.assertIn("release", self.log())

    def test_a_moved_fingerprint_earns_a_new_retry(self):
        self.verifier(code=1)
        self.turn()
        self.write("app.py", "x = 2\n")
        self.assertEqual(self.stop().get("decision"), "continue")
        self.assertEqual(self.stop().get("decision"), "stop")
        self.write("app.py", "x = 3\n")
        self.assertEqual(self.stop().get("decision"), "continue")

    def test_an_unresolvable_workspace_path_is_dropped_and_logged(self):
        res = self.stop(workspacePaths=["/no/such/place"])
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("unresolved", self.log())

    @unittest.skipUnless(os.name == "nt", "MSYS paths only reach a Windows Python")
    def test_an_msys_workspace_path_is_normalized(self):
        drive, rest = os.path.splitdrive(os.path.abspath(self.repo))
        msys = "/" + drive[0].lower() + rest.replace("\\", "/")
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.assertIn(self.NO_REVIEW, self.gap(workspacePaths=[msys]))

    def test_bad_input_stops(self):
        proc = subprocess.run([sys.executable, str(HOOKS_DIR / "stop_gate.py")],
                              input="not json", text=True, capture_output=True, env=self.env())
        self.assertEqual(json.loads(proc.stdout).get("decision"), "stop")


class TestPendingMarker(GateCase):
    """A stop that leaves a gap writes a marker the next invocation announces once."""

    def notes(self, cid):
        res = self.hook("reinforce.py", {"invocationNum": 5, "conversationId": cid,
                                         "workspacePaths": [self.repo]})
        return [s["ephemeralMessage"] for s in res["injectSteps"]]

    def leave_a_gap(self):
        self.verifier()
        self.turn()
        self.write("app.py", "x = 2\n")
        self.stop(terminationReason="USER_CANCELED")
        self.assertEqual(len(self.markers()), 1)

    def test_a_marker_is_said_once_per_conversation(self):
        self.leave_a_gap()
        said = self.notes("cid-1")
        self.assertEqual(len(said), 1)
        self.assertIn("still unverified", said[0])
        self.assertIn("changed but not verified", said[0])
        self.assertEqual(self.notes("cid-1"), [], "same conversation must not be told twice")
        self.assertEqual(len(self.notes("cid-2")), 1, "a new conversation has not heard it")
        self.assertEqual(len(self.markers()), 1, "only the gate clears a marker")

    def test_only_a_verified_stop_clears_the_marker(self):
        self.leave_a_gap()
        self.review()
        self.assertEqual(self.stop().get("decision"), "stop")
        self.assertEqual(self.markers(), [])

    def test_a_marker_older_than_a_day_is_dropped_unread(self):
        self.leave_a_gap()
        path = self.markers()[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["at"] = time.time() - 90000
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.notes("cid-1"), [])
        self.assertEqual(self.markers(), [])


class TestVerifierSelection(unittest.TestCase):
    """Which verifier a workspace picks. execute() is swapped for a recorder, so nothing runs."""

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import stop_gate

        self.gate = stop_gate
        self.ran = []
        real = stop_gate.execute
        self.addCleanup(setattr, stop_gate, "execute", real)
        stop_gate.execute = lambda ws, label, argv, deadline: (self.ran.append(label) or (label, 0, []))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        real_which = shutil.which
        self.addCleanup(setattr, shutil, "which", real_which)
        shutil.which = lambda name, *a, **k: real_which(name, *a, **k) or ("/bin/bash" if name == "bash" else None)
        self.trust(self.tmp)

    def trust(self, *roots):
        import hookpaths

        settings = Path(self.tmp) / "cli-settings.json"
        settings.write_text(json.dumps({"trustedWorkspaces": list(roots)}), encoding="utf-8")
        real = hookpaths.CLI_SETTINGS
        self.addCleanup(setattr, hookpaths, "CLI_SETTINGS", real)
        hookpaths.CLI_SETTINGS = str(settings)

    def verify(self):
        steps = self.gate.verifier_steps(self.tmp)
        return self.gate.verify(self.tmp, set(), steps,
                                time.monotonic() + self.gate.GATE_BUDGET)

    def write(self, rel, text="x"):
        path = Path(self.tmp) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_verify_script_wins_over_npm(self):
        self.write(".agents/verify.sh")
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        label, _, _ = self.verify()
        self.assertEqual(label, ".agents/verify.sh")
        self.assertEqual(self.ran, [".agents/verify.sh"])

    def test_cmd_preferred_over_ps1_and_sh(self):
        for name in self.gate.VERIFY_SCRIPTS:
            self.write(f".agents/{name}")
        label, _, _ = self.verify()
        self.assertEqual(label, ".agents/verify.cmd")

    def test_npm_runs_lint_then_test(self):
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        self.verify()
        self.assertEqual(self.ran, ["npm run lint", "npm test"])

    def test_npm_stops_at_first_failure(self):
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        self.gate.execute = lambda ws, label, argv, deadline: (
            self.ran.append(label) or (label, 1, ["boom"])
        )
        label, rc, _ = self.verify()
        self.assertEqual((label, rc), ("npm run lint", 1))
        self.assertEqual(self.ran, ["npm run lint"])

    def test_npm_without_matching_scripts_falls_through(self):
        self.write("package.json", json.dumps({"scripts": {"build": "x"}}))
        self.assertIsNone(self.verify())

    def test_pytest_markers_detected(self):
        for marker in self.gate.PYTEST_MARKERS:
            with self.subTest(marker=marker):
                self.fresh()
                self.write(marker)
                label, _, _ = self.verify()
                self.assertEqual(label, "python -m pytest -q -x")

    def test_npm_wins_over_pytest(self):
        self.write("package.json", json.dumps({"scripts": {"test": "x"}}))
        self.write("pyproject.toml")
        self.verify()
        self.assertEqual(self.ran, ["npm test"])

    def fresh(self):
        shutil.rmtree(self.tmp, True)
        os.makedirs(self.tmp, exist_ok=True)
        self.ran.clear()

    def test_cargo_toml_runs_cargo_test(self):
        self.write("Cargo.toml")
        self.assertEqual(self.verify()[0], "cargo test -q")
        self.assertEqual(self.ran, ["cargo test -q"])

    def test_go_mod_runs_vet_then_test(self):
        self.write("go.mod")
        self.assertEqual(self.verify()[0], "go test ./...")
        self.assertEqual(self.ran, ["go vet ./...", "go test ./..."])

    def test_dotnet_project_files_run_dotnet_test(self):
        for marker in ("App.sln", "App.csproj"):
            with self.subTest(marker=marker):
                self.fresh()
                self.write(marker)
                self.assertEqual(self.verify()[0], "dotnet test")
                self.assertEqual(self.ran, ["dotnet test"])

    def test_cargo_wins_over_pytest(self):
        self.write("Cargo.toml")
        self.write("pyproject.toml")
        self.verify()
        self.assertEqual(self.ran, ["cargo test -q"])

    def test_no_verifier(self):
        self.assertIsNone(self.verify())
        self.assertEqual(self.ran, [])


class TestTranscript(unittest.TestCase):
    """Transcript parsing, driven by a line captured from a real agy run."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transcript_sample.jsonl"
    PROJECT = "C:\\proj"

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import transcript

        self.mod = transcript
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.steps = [json.loads(ln) for ln
                      in self.FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def written(self, steps, project=None):
        return self.mod.edits(self.mod.calls(steps), project or self.PROJECT)

    def rename_tool(self, name):
        steps = json.loads(json.dumps(self.steps))
        for step in steps:
            for call in step.get("tool_calls") or []:
                call["name"] = name
        return steps

    def test_real_capture_yields_written_file(self):
        self.assertEqual(self.written(self.steps), {"note.rpy"})

    def test_arg_values_are_doubly_json_encoded(self):
        raw = self.steps[1]["tool_calls"][0]["args"]["TargetFile"]
        self.assertEqual(raw, json.dumps("C:\\proj\\note.rpy"))
        self.assertEqual(self.mod.unwrap(raw), "C:\\proj\\note.rpy")

    def test_plain_arg_values_are_read_too(self):
        """agy 1.2.6 sends the path as a plain string, the older capture encodes it twice."""
        steps = json.loads(json.dumps(self.steps))
        steps[1]["tool_calls"][0]["args"]["TargetFile"] = "C:\\proj\\note.rpy"
        self.assertEqual(self.written(steps), {"note.rpy"})

    def test_path_outside_project_ignored(self):
        self.assertEqual(self.written(self.steps, "C:\\elsewhere"), set())

    def test_artifact_writes_are_not_workspace_edits(self):
        artifacts = os.path.join(self.PROJECT, "brain")
        steps = json.loads(json.dumps(self.steps))
        steps[1]["tool_calls"][0]["args"]["TargetFile"] = json.dumps(
            os.path.join(artifacts, "note.rpy"))
        self.assertEqual(self.mod.edits(self.mod.calls(steps), self.PROJECT, [artifacts]), set())

    def test_paths_match_case_insensitively(self):
        """agy reports C:\\Users\\... while the transcript may say c:\\users\\..."""
        expected = {"note.rpy"} if os.name == "nt" else set()
        self.assertEqual(self.written(self.steps, self.PROJECT.swapcase()), expected)

    def test_read_only_tool_ignored(self):
        self.assertEqual(self.written(self.rename_tool("view_file")), set())

    def test_every_write_tool_counted(self):
        for name in self.mod.EDIT_TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(self.written(self.rename_tool(name)), {"note.rpy"})

    def test_the_answer_file_is_not_an_edit(self):
        steps = json.loads(json.dumps(self.steps))
        steps[1]["tool_calls"][0]["args"]["TargetFile"] = json.dumps(
            os.path.join(self.PROJECT, ".agents", "DECISIONS.md"))
        self.assertEqual(self.written(steps), set())

    def test_written_paths_keep_the_order_they_were_written_in(self):
        path = write_transcript(Path(self.tmp) / "t.jsonl",
                                [("edit", "b.py"), ("edit", "a.py"), ("edit", "b.py")], self.tmp)
        calls = self.mod.calls(self.mod.load(path))
        self.assertEqual([os.path.basename(p) for p in self.mod.written_paths(calls)],
                         ["b.py", "a.py"])

    def test_missing_transcript_reads_as_nothing(self):
        self.assertIsNone(self.mod.load(str(Path(self.tmp) / "nope.jsonl")))

    def test_unparseable_transcript_reads_as_nothing(self):
        path = Path(self.tmp) / "junk.jsonl"
        path.write_text("not json\nalso not json\n", encoding="utf-8")
        self.assertIsNone(self.mod.load(str(path)))

    def test_the_untruncated_transcript_wins(self):
        logs = Path(self.tmp) / "logs"
        logs.mkdir()
        (logs / "transcript.jsonl").write_text("", encoding="utf-8")
        write_transcript(logs / "transcript_full.jsonl", [("edit", "app.py")], self.tmp)
        self.assertEqual(len(self.mod.load(str(logs / "transcript.jsonl"))), 2)

    def subagent_record(self, cid, type_name):
        records = Path(self.tmp) / "parent" / ".system_generated" / "subagents"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{cid}.json").write_text(json.dumps(
            {"subagentDescriptor": {"typeName": type_name}}), encoding="utf-8")

    def test_subagent_type_comes_from_the_parents_record(self):
        self.subagent_record("child-cid", "reviewer")
        self.assertEqual(self.mod.subagent_type("child-cid", self.tmp), "reviewer")

    def test_a_record_without_a_type_still_marks_a_subagent(self):
        records = Path(self.tmp) / "parent" / ".system_generated" / "subagents"
        records.mkdir(parents=True, exist_ok=True)
        (records / "child-cid.json").write_text("{}", encoding="utf-8")
        self.assertTrue(self.mod.subagent_type("child-cid", self.tmp))

    def test_conversation_without_a_record_is_top_level(self):
        self.subagent_record("child-cid", "reviewer")
        self.assertEqual(self.mod.subagent_type("other-cid", self.tmp), "")

    def test_brain_root_and_conversation_id_come_from_the_transcript_path(self):
        path = "/data/brain/cid/.system_generated/logs/transcript.jsonl"
        self.assertEqual(self.mod.brain_root(path).replace("\\", "/"), "/data/brain")
        self.assertEqual(self.mod.conversation_of(path), "cid")
        self.assertEqual(self.mod.conversation_of("/tmp/t.jsonl"), "")


class TestFingerprint(unittest.TestCase):
    """The fingerprint itself, against a real repository."""

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import gitstate

        self.mod = gitstate
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        for args in (("init", "-q"), ("config", "user.email", "g@example.invalid"),
                     ("config", "user.name", "g")):
            subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)
        self.write("app.py", "x = 1\n")
        self.write(".gitignore", "build/\n")
        self.commit()

    def write(self, rel, text):
        path = Path(self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self):
        subprocess.run(["git", "add", "-A"], cwd=self.repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=self.repo,
                       capture_output=True, check=True)

    def digest(self):
        return self.mod.digest(self.mod.fingerprint(self.repo))

    def test_an_untouched_tree_keeps_its_fingerprint(self):
        self.assertEqual(self.digest(), self.digest())

    def test_editing_a_tracked_file_moves_it(self):
        before = self.digest()
        self.write("app.py", "x = 2\n")
        self.assertNotEqual(before, self.digest())

    def test_committing_moves_it(self):
        before = self.digest()
        self.write("app.py", "x = 2\n")
        self.commit()
        self.assertNotEqual(before, self.digest())

    def test_a_new_untracked_file_moves_it(self):
        before = self.digest()
        self.write("extra.py", "y = 1\n")
        self.assertNotEqual(before, self.digest())

    def test_an_ignored_file_does_not_move_it(self):
        before = self.digest()
        self.write("build/out.js", "// generated\n")
        self.assertEqual(before, self.digest())

    def test_a_file_over_the_size_cap_is_hashed_by_size_and_mtime(self):
        path = Path(self.repo) / "big.bin"
        path.write_bytes(b"\0" * (self.mod.SIZE_CAP + 1))
        self.assertTrue(self.mod.file_hash(str(path)).startswith("stat:"))

    def test_a_big_file_rewritten_with_its_mtime_restored_moves_it(self):
        """Size and mtime can both be put back by hand, so the file's two ends are read too."""
        path = Path(self.repo) / "big.bin"
        path.write_bytes(b"a" * 3_000_000)
        before = self.digest()
        stat = os.stat(path)
        path.write_bytes(b"b" + b"a" * 2_999_999)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(os.stat(path).st_size, stat.st_size)
        self.assertEqual(os.stat(path).st_mtime_ns, stat.st_mtime_ns)
        self.assertNotEqual(before, self.digest())

    def test_a_file_inside_a_nested_untracked_repository_moves_it(self):
        nested = Path(self.repo) / "vendor"
        nested.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=nested, capture_output=True, check=True)
        self.write("vendor/lib.py", "y = 1\n")
        before = self.digest()
        self.write("vendor/lib.py", "y = 2\n")
        self.assertNotEqual(before, self.digest())

    def test_a_workspace_inside_a_bigger_repository_ignores_the_rest_of_it(self):
        self.write("sub/app.py", "x = 1\n")
        self.commit()
        sub = os.path.join(self.repo, "sub")
        before = self.mod.digest(self.mod.fingerprint(sub))
        self.write("app.py", "x = 99\n")
        self.write("other.py", "y = 1\n")
        self.assertEqual(before, self.mod.digest(self.mod.fingerprint(sub)))
        self.write("sub/app.py", "x = 2\n")
        self.assertNotEqual(before, self.mod.digest(self.mod.fingerprint(sub)))

    def test_a_directory_that_is_not_a_repository_has_no_fingerprint(self):
        plain = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, plain, True)
        self.assertIsNone(self.mod.fingerprint(plain))

    def test_a_fingerprint_names_both_kinds_of_change(self):
        self.write("app.py", "x = 2\n")
        self.write("extra.py", "y = 1\n")
        self.assertEqual(self.mod.fingerprint(self.repo).names, {"app.py", "extra.py"})

    def test_toplevel_finds_the_repository_root(self):
        self.write("pkg/mod.py", "z = 1\n")
        found = self.mod.toplevel(os.path.join(self.repo, "pkg"))
        self.assertEqual(os.path.normcase(os.path.abspath(found)),
                         os.path.normcase(os.path.realpath(self.repo)))


class TestGateInternals(unittest.TestCase):
    """execute, script_argv, instruction docs and the retry ledger, called directly."""

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import hookpaths
        import stop_gate

        self.gate = stop_gate
        self.paths = hookpaths
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_spent_budget_reports_a_finding_instead_of_running(self):
        label, rc, lines = self.gate.execute(
            self.tmp, "npm test", ["npm", "test"], time.monotonic() - 1
        )
        self.assertEqual((label, rc), ("npm test", 1))
        self.assertIn("verifier budget exhausted", lines[0])

    def test_the_gate_budget_leaves_room_under_the_hook_timeout(self):
        hooks = json.loads((HOOKS_DIR.parent / "hooks.json").read_text(encoding="utf-8"))
        self.assertLess(self.gate.GATE_BUDGET, hooks["stop-gate"]["Stop"][0]["timeout"] - 60)

    def test_cmd_script_runs_through_call(self):
        argv = self.gate.script_argv(os.path.join(".agents", "verify.cmd"))
        self.assertEqual(argv[:3], ["cmd", "/c", "call"])

    def test_sh_script_argv_needs_bash(self):
        real_which = shutil.which
        self.addCleanup(setattr, shutil, "which", real_which)
        shutil.which = lambda name, *a, **k: None if name == "bash" else real_which(name, *a, **k)
        self.assertIsNone(self.gate.script_argv(os.path.join(".agents", "verify.sh")))

    def test_sh_script_without_bash_is_not_a_verifier(self):
        agents = Path(self.tmp) / ".agents"
        agents.mkdir()
        (agents / "verify.sh").write_text("exit 1\n", encoding="utf-8")
        real_which = shutil.which
        self.addCleanup(setattr, shutil, "which", real_which)
        shutil.which = lambda name, *a, **k: None if name == "bash" else real_which(name, *a, **k)
        self.assertEqual(self.gate.verifier_steps(self.tmp), [])

    def test_instruction_docs_are_markdown_in_an_instruction_place(self):
        for rel in ("GEMINI.md", "AGENTS.md", "skills/caveman/SKILL.md", "agents/coder.md",
                    "rules/yagni.md", ".agents/rules/house.md"):
            self.assertTrue(self.paths.is_instruction_doc(rel), rel)
        for rel in ("src/agents/x.py", "src/agents/x.md", "agents/tool.py", "notes.md",
                    "docs/skills/plan.md", "app.py"):
            self.assertFalse(self.paths.is_instruction_doc(rel), rel)

    def test_docs_only_ignores_the_answer_file(self):
        self.assertTrue(self.gate.docs_only({"GEMINI.md", ".agents/DECISIONS.md"}))
        self.assertFalse(self.gate.docs_only({"GEMINI.md", "app.py"}))
        self.assertFalse(self.gate.docs_only(set()), "a moved fingerprint with no names is work")
        self.assertFalse(self.gate.docs_only(None))

    def test_leftovers_are_backups_and_caches(self):
        for rel in ("notes.bak", "a/b.orig", "old-plan.old", "scratch.tmp",
                    "pkg/__pycache__/app.pyc"):
            self.assertTrue(self.gate.is_junk(rel), rel)
        self.assertFalse(self.gate.is_junk("src/app.py"))

    def retry(self, kind, cid="conv"):
        real = self.paths.RETRIES
        self.addCleanup(setattr, self.paths, "RETRIES", real)
        self.paths.RETRIES = os.path.join(self.tmp, "retries")
        return self.paths.take_retry(cid, kind, 1)

    def test_one_retry_per_kind_and_fingerprint(self):
        self.assertTrue(self.retry("no-review:aaa"))
        self.assertFalse(self.retry("no-review:aaa"))
        self.assertTrue(self.retry("no-review:bbb"), "a moved fingerprint is a new allowance")
        self.assertTrue(self.retry("verifier:aaa"), "a different gap kind has its own allowance")

    def test_a_garbage_ledger_counts_as_spent(self):
        self.assertTrue(self.retry("verifier:aaa"))
        Path(self.paths.retry_path("conv")).write_text("{broken", encoding="utf-8")
        self.assertFalse(self.retry("no-review:aaa"))

    def test_the_conversation_ceiling_outranks_a_fresh_fingerprint(self):
        for kind in ("verifier:a", "verifier:b", "verifier:c"):
            self.assertTrue(self.retry(kind), kind)
        self.assertFalse(self.retry("verifier:d"), "the total is capped per conversation")
        self.assertEqual(self.paths.forced_count("conv"), self.paths.MAX_FORCED)

    def test_a_conversation_without_an_id_gets_no_retry(self):
        self.assertFalse(self.retry("verifier:aaa", cid=""))
