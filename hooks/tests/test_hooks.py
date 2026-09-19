#!/usr/bin/env python3
"""Unit tests for Antigravity hooks."""
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
    if kind == "reviewer":
        return one_call(("spawn", "reviewer", "Review `git diff HEAD`"), root)
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
    ("multi", event, event) puts both calls in one step, which is where positional
    call-to-result pairing has to hold.
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


class TestStopGate(unittest.TestCase):
    """Drives the hook with sitecustomize stubs for renpy.exe, git, and the SDK probe."""

    SCRIPT = HOOKS_DIR / "stop_gate.py"
    LINT_OUT = (
        "Ren'Py lint report\n\n"
        "game/changed.rpy:4 'a' is not an image.\n\n"
        "game/untouched.rpy:9 'b' is not an image.\n"
    )

    def run_hook(self, payload, stub=None, trusted=True):
        env = dict(os.environ)
        env["GEMINI_HOOK_TMP"] = self.tmp
        settings = Path(self.tmp) / "cli-settings.json"
        roots = [self.tmp, "/proj"] if trusted else []
        settings.write_text(json.dumps({"trustedWorkspaces": roots}), encoding="utf-8")
        env["GEMINI_CLI_SETTINGS"] = str(settings)
        if stub:
            (Path(self.tmp) / "sitecustomize.py").write_text(stub, encoding="utf-8")
            env["PYTHONPATH"] = self.tmp
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)], input=json.dumps(payload),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def stub(self, changed):
        """sitecustomize that replaces subprocess.run for renpy.exe and git, and os.path.isdir."""
        return (
            "import os, subprocess\n"
            f"CHANGED = {changed!r}\n"
            f"LINT = {self.LINT_OUT!r}\n"
            "_run = subprocess.run\n"
            "class R:\n"
            "    def __init__(self, out, rc=0):\n"
            "        self.stdout, self.returncode = out, rc\n"
            "def run(cmd, *a, **k):\n"
            "    if 'lint' in cmd:\n"
            "        return R(LINT.encode(), 1)\n"
            "    if cmd[0] == 'git':\n"
            "        if 'rev-parse' in cmd:\n"
            "            return R(b'' if CHANGED is None else b'/proj\\n')\n"
            "        if 'diff' in cmd:\n"
            "            return R(('\\n'.join(CHANGED or []) + '\\n').encode())\n"
            "        return R(b'')\n"
            "    return _run(cmd, *a, **k)\n"
            "subprocess.run = run\n"
            "_isdir = os.path.isdir\n"
            "os.path.isdir = lambda p: p.replace('\\\\', '/').rstrip('/').endswith(('/game', '/proj')) or _isdir(p)\n"
            "import shutil\n"
            "_which = shutil.which\n"
            "shutil.which = lambda n, *a, **k: None if n == 'cygpath' else _which(n, *a, **k)\n"
            "os.environ['RENPY_SDK_PATH'] = '/sdk'\n"
            "_isfile = os.path.isfile\n"
            "os.path.isfile = lambda p: 'renpy.' in os.path.basename(p) or _isfile(p)\n"
        )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def payload(self, **kw):
        """A stop payload. No executionNum: agy omits it, and the gate counts retries itself."""
        base = {
            "terminationReason": "model_stop", "fullyIdle": True,
            "workspacePaths": ["/proj"],
        }
        base.update(kw)
        return base

    def transcript_of(self, *events, root=None, path=None):
        """Transcript path for ("edit", rel), ("verifier", passed) and ("reviewer",) events."""
        return write_transcript(path or Path(self.tmp) / "t.jsonl", events, root or self.tmp)

    def transcript_writing(self, *rels):
        """A transcript in which this session wrote each given workspace-relative path."""
        return self.transcript_of(*[("edit", rel) for rel in rels])

    def verified(self, *rels, root=None):
        """The same, then a passing verifier run and a reviewer subagent, so evidence is complete."""
        return self.transcript_of(*[("edit", rel) for rel in rels],
                                  ("verifier", True), ("reviewer",), root=root)

    def pytest_marker(self):
        """Gives the workspace a verifier, so the gate can name the command the agent owes."""
        (Path(self.tmp) / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    def test_non_renpy_workspace_stops(self):
        res = self.run_hook(self.payload(workspacePaths=[self.tmp]))
        self.assertEqual(res.get("decision"), "stop")

    def leftover(self, name, directory=False):
        """A stray file or directory the session left in the workspace."""
        path = Path(self.tmp) / name
        path.mkdir() if directory else path.write_text("stale\n", encoding="utf-8")
        # one conversation per call, so each gets its own forced-retry budget
        return self.run_hook(self.payload(workspacePaths=[self.tmp], conversationId=name))

    def test_backup_file_blocks_the_stop(self):
        res = self.leftover("notes.bak")
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("delete leftovers: notes.bak", res.get("reason", ""))

    def test_every_backup_suffix_is_a_leftover(self):
        for name in ("app.py.orig", "old-plan.old", "scratch.tmp"):
            self.assertIn(name, self.leftover(name).get("reason", ""), name)

    def test_empty_directory_is_a_leftover(self):
        self.assertIn("scratch/", self.leftover("scratch", directory=True).get("reason", ""))

    def test_unignored_pycache_is_a_leftover(self):
        """Not empty, so only the __pycache__ rule can catch it."""
        cache = Path(self.tmp) / "__pycache__"
        cache.mkdir()
        (cache / "app.cpython-313.pyc").write_bytes(b"\x00")
        res = self.run_hook(self.payload(workspacePaths=[self.tmp]))
        self.assertIn("__pycache__", res.get("reason", ""))

    def test_shipped_no_tool_call_reason_gates(self):
        """agy sends NO_TOOL_CALL, not the documented model_stop."""
        res = self.run_hook(
            self.payload(terminationReason="NO_TOOL_CALL"), stub=self.stub(["game/changed.rpy"])
        )
        self.assertEqual(res.get("decision"), "continue")

    def test_user_canceled_stops(self):
        res = self.run_hook(
            self.payload(terminationReason="USER_CANCELED"), stub=self.stub(["game/changed.rpy"])
        )
        self.assertEqual(res.get("decision"), "stop")

    def test_max_steps_exceeded_stops(self):
        res = self.run_hook(self.payload(terminationReason="max_steps_exceeded"))
        self.assertEqual(res.get("decision"), "stop")

    def test_not_fully_idle_stops_when_nothing_was_edited(self):
        res = self.run_hook(self.payload(fullyIdle=False))
        self.assertEqual(res.get("decision"), "stop")

    def test_not_fully_idle_still_blocks_unverified_edits(self):
        """The transcript check runs no commands, so background tasks cannot excuse it."""
        self.pytest_marker()
        res = self.run_hook(self.payload(
            fullyIdle=False, workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing("app.py"),
        ))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("no passing verifier run after your last change (step 0)", res["reason"])

    def test_git_filtered_errors_continue(self):
        res = self.run_hook(self.payload(), stub=self.stub(["game/changed.rpy"]))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("game/changed.rpy:4", res["reason"])
        self.assertNotIn("game/untouched.rpy", res["reason"])
        self.assertIn("Fix, re-run renpy lint, then finish.", res["reason"])

    def test_errors_only_in_unchanged_files_are_not_reported(self):
        res = self.run_hook(self.payload(), stub=self.stub(["game/other.rpy"]))
        self.assertNotIn("Verifier failed", res.get("reason", ""))

    def test_non_git_project_counts_all(self):
        res = self.run_hook(self.payload(), stub=self.stub(None))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("game/untouched.rpy:9", res["reason"])

    def test_transcript_filter_overrides_git(self):
        """git calls changed.rpy dirty, but this session only wrote untouched.rpy."""
        project = os.path.abspath("/proj")
        path = self.transcript_of(("edit", "game/untouched.rpy"),
                                  ("run", f"renpy.exe {project} lint --error-code", 0, project),
                                  ("reviewer",), root=project)
        res = self.run_hook(
            self.payload(transcriptPath=path), stub=self.stub(["game/changed.rpy"])
        )
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("game/untouched.rpy:9", res["reason"])
        self.assertNotIn("game/changed.rpy", res["reason"])

    def test_bad_instruction_doc_gates_at_stop(self):
        """PostToolUse never fires in 1.1.27, so the Stop hook has to catch this."""
        (Path(self.tmp) / "GEMINI.md").write_text(
            "# Rules\n\n- See `~/.gemini/nope-does-not-exist` for details.\n", encoding="utf-8"
        )
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_writing("GEMINI.md")
        ))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("ai-docs-lint failed in files you changed:", res["reason"])
        self.assertIn("dead-path: ~/.gemini/nope-does-not-exist", res["reason"])

    def test_clean_instruction_doc_stops(self):
        """An instruction doc is not code, so it owes no reviewer."""
        (Path(self.tmp) / "GEMINI.md").write_text("# Rules\n\n- Keep it short.\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_writing("GEMINI.md")
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_non_instruction_file_is_not_docs_linted(self):
        (Path(self.tmp) / "notes.md").write_text("`~/.gemini/nope-does-not-exist`\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.verified("notes.md")
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_docs_findings_release_after_the_cap(self):
        (Path(self.tmp) / "GEMINI.md").write_text(
            "# Rules\n\n- See `~/.gemini/nope-does-not-exist` for details.\n", encoding="utf-8"
        )
        payload = self.payload(workspacePaths=[self.tmp],
                               transcriptPath=self.transcript_writing("GEMINI.md"))
        self.assertEqual(self.run_hook(payload).get("decision"), "continue")
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("still unmet: docs", res.get("reason", ""))

    def test_real_verify_script_failure_continues(self):
        """End to end through subprocess: a .agents verifier that exits 1."""
        agents = Path(self.tmp) / ".agents"
        agents.mkdir()
        if os.name == "nt":
            (agents / "verify.cmd").write_text("@echo FAIL\r\n@exit /b 1\r\n", encoding="utf-8")
            label = ".agents/verify.cmd"
        else:
            (agents / "verify.sh").write_text("echo FAIL\nexit 1\n", encoding="utf-8")
            label = ".agents/verify.sh"
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_of(
                ("edit", "src/app.py"), ("run", label, 0), ("reviewer",))
        ))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn(f"Verifier failed ({label}):", res["reason"])
        self.assertIn("FAIL", res["reason"])
        self.assertIn(f"Fix, re-run {label}, then finish.", res["reason"])

    def code_payload(self, **kw):
        """A stop after this session wrote one source file into the workspace."""
        (Path(self.tmp) / "app.py").write_text("x = 1\n", encoding="utf-8")
        kw.setdefault("workspacePaths", [self.tmp])
        if "transcriptPath" not in kw:  # setdefault would rewrite a transcript the caller built
            kw["transcriptPath"] = self.transcript_writing("app.py")
        return self.payload(**kw)

    def blocks(self, *events, **kw):
        """Stop over these events in a workspace with a verifier; returns the continue reason."""
        self.pytest_marker()
        res = self.run_hook(self.code_payload(transcriptPath=self.transcript_of(*events), **kw))
        self.assertEqual(res.get("decision"), "continue", res)
        return res["reason"]

    def verify_script(self, code=0):
        """Give the workspace a trusted verify script; returns the command that runs it."""
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        if os.name == "nt":
            (agents / "verify.cmd").write_text(f"@exit /b {code}\r\n", encoding="utf-8")
            return ".agents/verify.cmd"
        (agents / "verify.sh").write_text(f"exit {code}\n", encoding="utf-8")
        return ".agents/verify.sh"

    def test_hand_written_audit_report_is_not_evidence(self):
        """The old gate read .agents/audit.json, which the agent could simply write."""
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        (agents / "audit.json").write_text(json.dumps({"clean": True, "round": 1}),
                                           encoding="utf-8")
        self.assertIn("no passing verifier run", self.blocks(("edit", "app.py")))

    def test_verifier_before_the_last_change_does_not_count(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("edit", "app.py"))
        self.assertIn("no passing verifier run after your last change (step 8)", reason)

    def test_failed_verifier_does_not_count(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", False), ("reviewer",))
        self.assertIn("`python -m pytest -q -x`", reason)

    def test_echoing_the_verifier_is_not_running_it(self):
        reason = self.blocks(("edit", "app.py"), ("run", "echo pytest", 0), ("reviewer",))
        self.assertIn("no passing verifier run after your last change", reason)

    def test_collecting_tests_is_not_running_them(self):
        reason = self.blocks(("edit", "app.py"),
                             ("run", "python -m pytest --collect-only", 0), ("reviewer",))
        self.assertIn("no passing verifier run after your last change", reason)

    def test_verifier_run_in_another_project_does_not_count(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        reason = self.blocks(("edit", "app.py"), ("run", VERIFIER_LINE, 0, other),
                             ("reviewer",))
        self.assertIn("no passing verifier run after your last change", reason)

    def test_backgrounded_verifier_proves_nothing(self):
        """No "exited with code" line, so the run never reported a verdict."""
        reason = self.blocks(("edit", "app.py"), ("background",), ("reviewer",))
        self.assertIn("no passing verifier run after your last change", reason)

    def test_shell_write_without_an_edit_tool_is_still_a_change(self):
        reason = self.blocks(("run", "echo x > app.py", 0))
        self.assertIn("no passing verifier run after your last change (step 0)", reason)

    def test_verified_and_reviewed_work_stops(self):
        res = self.run_hook(self.code_payload(transcriptPath=self.verified("app.py")))
        self.assertEqual(res.get("decision"), "stop")

    def test_fix_only_edits_after_the_review_pass(self):
        """Acting on the reviewer's findings must not owe another review."""
        script = self.verify_script()
        res = self.run_hook(self.code_payload(transcriptPath=self.transcript_of(
            ("edit", "app.py"), ("run", script, 0), ("reviewer",),
            ("edit", "app.py"), ("run", script, 0))))
        self.assertEqual(res.get("decision"), "stop", res)

    def test_new_file_after_the_review_needs_a_new_review(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("reviewer",),
                            ("edit", "extra.py"), ("verifier", True))
        self.assertIn("extra.py was first changed after the review at step 8", reason)

    def test_delegated_change_after_the_review_needs_a_new_review(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("reviewer",),
                            ("spawn", "coder", "Finish the feature"), ("verifier", True))
        self.assertIn("a delegated or shell change at step 12 came after the review at step 8",
                      reason)

    def test_missing_reviewer_blocks(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True))
        self.assertIn("no reviewer spawned after your first change (step 0)", reason)
        self.assertIn("TypeName reviewer", reason)

    def test_another_agent_asked_to_review_is_not_the_reviewer(self):
        reason = self.blocks(("edit", "app.py"),
                            ("spawn", "researcher", "Review `git diff HEAD`", "Code Reviewer"),
                            ("verifier", True))
        self.assertIn("no reviewer spawned after your first change", reason)

    def test_reviewer_without_a_prompt_is_not_a_review(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("spawn", "reviewer", ""))
        self.assertIn("no reviewer spawned after your first change", reason)

    def test_reviewer_whose_result_errored_is_not_a_review(self):
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("spawn_error", "reviewer"))
        self.assertIn("no reviewer spawned after your first change", reason)

    def test_fully_delegated_run_is_still_verified(self):
        """Nothing named a file, so the verifier runs against what git says changed."""
        script = self.verify_script(code=1)
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_of(
                ("spawn", "coder", "Implement it"), ("run", script, 0), ("reviewer",))))
        self.assertEqual(res.get("decision"), "continue", res)
        self.assertIn(f"Verifier failed ({script})", res["reason"])

    def test_fully_delegated_run_is_still_swept_for_leftovers(self):
        script = self.verify_script()
        (Path(self.tmp) / "app.py.bak").write_text("old\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_of(
                ("spawn", "coder", "Implement it"), ("run", script, 0), ("reviewer",))))
        self.assertEqual(res.get("decision"), "continue", res)
        self.assertIn("delete leftovers: app.py.bak", res["reason"])

    def test_a_diff_dumped_to_an_ignored_path_is_not_a_change(self):
        """End to end, through a real `git check-ignore` in a real repository."""
        script = self.verify_script()
        subprocess.run(["git", "init", "-q"], cwd=self.tmp, capture_output=True)
        (Path(self.tmp) / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        (Path(self.tmp) / "scratch").mkdir()
        (Path(self.tmp) / "scratch" / "diff.txt").write_text("diff --git\n", encoding="utf-8")
        res = self.run_hook(self.code_payload(transcriptPath=self.transcript_of(
            ("edit", "app.py"), ("run", script, 0), ("reviewer",),
            ("run", "git diff HEAD > scratch/diff.txt", 0))))
        self.assertEqual(res.get("decision"), "stop", res)

    def test_unreadable_transcript_blocks_once(self):
        truncated = Path(self.tmp) / "truncated.jsonl"
        truncated.write_text('{"step_index": 0, "type": "PLANNER_RESP', encoding="utf-8")
        payload = self.payload(workspacePaths=[self.tmp], transcriptPath=str(truncated))
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("could not read this conversation's transcript", res["reason"])
        self.assertEqual(self.run_hook(payload).get("decision"), "stop")
        self.assertIn("no-transcript",
                     (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_second_stop_is_released_with_a_message_for_the_human(self):
        self.pytest_marker()
        payload = self.code_payload(
            transcriptPath=self.transcript_of(("edit", "app.py"), ("verifier", True)))
        self.assertEqual(self.run_hook(payload).get("decision"), "continue")
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("Stop allowed after one forced retry", res["reason"])
        self.assertIn("no reviewer spawned", res["reason"])
        self.assertIn("release", (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_user_canceled_with_a_gap_leaves_a_marker(self):
        self.pytest_marker()
        res = self.run_hook(self.code_payload(terminationReason="USER_CANCELED"))
        self.assertEqual(res.get("decision"), "stop")
        self.assertEqual(len(list((Path(self.tmp) / "pending").glob("*.json"))), 1)
        self.assertIn("pending", (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_both_workspaces_in_one_payload_are_judged(self):
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        (Path(other) / "pyproject.toml").write_text("[project]\nname = 'y'\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp, other],
            transcriptPath=self.transcript_of(("edit", "lib.py"), root=other)))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn(other, res["reason"])
        self.assertIn("python -m pytest -q -x", res["reason"])

    @unittest.skipUnless(os.name == "nt", "MSYS paths only reach a Windows Python")
    def test_msys_transcript_path_is_normalized(self):
        self.pytest_marker()
        path = os.path.abspath(self.transcript_writing("app.py"))
        drive, rest = os.path.splitdrive(path)
        msys = "/" + drive[0].lower() + rest.replace("\\", "/")
        res = self.run_hook(self.code_payload(transcriptPath=msys))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("no passing verifier run", res["reason"])

    def subagent_transcript(self, cid, type_name="coder"):
        """A brain tree in which cid is another conversation's subagent."""
        logs = Path(self.tmp) / "brain" / cid / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        records = Path(self.tmp) / "brain" / "parent-cid" / ".system_generated" / "subagents"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{cid}.json").write_text(json.dumps({
            "conversationId": cid, "subagentDescriptor": {"typeName": type_name},
            "state": "SUBAGENT_STATE_ALIVE", "spawnStepIndex": 12}), encoding="utf-8")
        return logs / "transcript.jsonl"

    def test_subagent_owes_no_reviewer_where_a_top_level_run_does(self):
        """Same evidence, same workspace: only the parent of a subagent owes the review."""
        cid = "sub-cid"
        script = self.verify_script()
        path = self.transcript_of(("edit", "app.py"), ("run", script, 0),
                                  path=self.subagent_transcript(cid))
        payload = self.code_payload(conversationId=cid, transcriptPath=path)
        self.assertEqual(self.run_hook(payload).get("decision"), "stop")
        res = self.run_hook(dict(payload, conversationId="top-cid"))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("no reviewer spawned", res["reason"])

    def test_subagent_still_owes_the_verifier(self):
        cid = "sub-cid"
        self.pytest_marker()
        path = self.transcript_of(("edit", "app.py"), path=self.subagent_transcript(cid))
        res = self.run_hook(self.code_payload(conversationId=cid, transcriptPath=path))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("no passing verifier run", res["reason"])

    def visual_spec(self):
        (Path(self.tmp) / ".agents").mkdir(exist_ok=True)
        (Path(self.tmp) / ".agents" / "visual.md").write_text(
            "## Pages\n\nhttp://localhost:5173/\n\n## Accept\n\n- Nav is visible\n", encoding="utf-8"
        )

    def test_visual_spec_requires_a_visual_qa_subagent(self):
        self.visual_spec()
        reason = self.blocks(("edit", "app.py"), ("verifier", True), ("reviewer",))
        self.assertIn("no visual-qa subagent after your last change (step 0)", reason)
        self.assertIn("TypeName visual-qa", reason)

    def test_visual_qa_spawn_satisfies_the_visual_spec(self):
        self.visual_spec()
        script = self.verify_script()
        res = self.run_hook(self.code_payload(transcriptPath=self.transcript_of(
            ("edit", "app.py"), ("run", script, 0), ("reviewer",),
            ("spawn", "visual-qa", "Check the pages in .agents/visual.md"))))
        self.assertEqual(res.get("decision"), "stop", res)

    def test_without_a_visual_spec_no_visual_qa_is_owed(self):
        script = self.verify_script()
        res = self.run_hook(self.code_payload(transcriptPath=self.transcript_of(
            ("edit", "app.py"), ("run", script, 0), ("reviewer",))))
        self.assertEqual(res.get("decision"), "stop", res)

    def test_the_gates_own_answer_file_is_not_a_change(self):
        """Otherwise answering the gate would itself keep the gate open."""
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing(".agents/DECISIONS.md"),
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_untouched_workspace_is_skipped(self):
        """A session that wrote nothing here must not trigger the verifier."""
        agents = Path(self.tmp) / ".agents"
        agents.mkdir()
        (agents / "verify.sh").write_text("exit 1\n", encoding="utf-8")
        empty = Path(self.tmp) / "empty.jsonl"
        empty.write_text(json.dumps({"step_index": 0, "type": "USER_INPUT"}) + "\n", encoding="utf-8")
        res = self.run_hook(self.payload(workspacePaths=[self.tmp], transcriptPath=str(empty)))
        self.assertEqual(res.get("decision"), "stop")

    def test_repeated_verifier_failure_is_released(self):
        stub = self.stub(["game/changed.rpy"])
        self.assertEqual(self.run_hook(self.payload(), stub=stub).get("decision"), "continue")
        res = self.run_hook(self.payload(), stub=stub)
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("still unmet: verifier", res.get("reason", ""))

    def test_msys_workspace_path_is_normalized(self):
        """agy started from Git Bash sends /c/Users/...; Windows Python cannot open that."""
        drive, rest = os.path.splitdrive(os.path.abspath(self.tmp))
        msys = "/" + drive[0].lower() + rest.replace("\\", "/")
        res = self.run_hook(self.payload(workspacePaths=[msys]))
        self.assertEqual(res.get("decision"), "stop")
        logged = (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8")
        self.assertIn(os.path.abspath(self.tmp).replace("\\", "/"), logged)
        self.assertNotIn("unresolved", logged)

    def test_unresolvable_workspace_is_dropped_and_logged(self):
        res = self.run_hook(self.payload(workspacePaths=["/no/such/place"]))
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("unresolved", (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_untrusted_workspace_skips_the_verify_script(self):
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        (agents / "verify.sh").write_text("exit 1\n", encoding="utf-8")
        res = self.run_hook(self.code_payload(transcriptPath=self.verified("app.py")),
                            trusted=False)
        self.assertNotIn("Verifier failed", res.get("reason", ""))
        self.assertIn("untrusted workspace, verifier skipped",
                      (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_continue_reason_reports_the_remaining_budget(self):
        res = self.run_hook(self.payload(), stub=self.stub(["game/changed.rpy"]))
        self.assertIn(f"s of the {800}s gate budget left.", res["reason"])

    def test_bad_input_stops(self):
        env = dict(os.environ, GEMINI_HOOK_TMP=self.tmp)
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)], input="not json", text=True, capture_output=True, env=env
        )
        self.assertEqual(json.loads(proc.stdout).get("decision"), "stop")


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
        self.steps = [json.loads(ln) for ln in self.FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def written(self, steps, project=None):
        return set(self.mod.edits(self.mod.calls(steps), project or self.PROJECT))

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
        self.assertEqual(self.mod.edits(self.mod.calls(steps), self.PROJECT, [artifacts]), {})

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

    def test_session_with_no_writes_gates_nothing(self):
        self.assertEqual(self.written(self.steps[:1]), set())

    def test_missing_transcript_falls_back_to_git(self):
        self.assertIsNone(self.mod.load(str(Path(self.tmp) / "nope.jsonl")))

    def test_unparseable_transcript_falls_back_to_git(self):
        path = Path(self.tmp) / "junk.jsonl"
        path.write_text("not json\nalso not json\n", encoding="utf-8")
        self.assertIsNone(self.mod.load(str(path)))

    def test_the_untruncated_transcript_wins(self):
        logs = Path(self.tmp) / "logs"
        logs.mkdir()
        (logs / "transcript.jsonl").write_text("", encoding="utf-8")
        write_transcript(logs / "transcript_full.jsonl", [("edit", "app.py")], self.tmp)
        self.assertEqual(len(self.mod.load(str(logs / "transcript.jsonl"))), 2)

    def runs(self, *events):
        path = write_transcript(Path(self.tmp) / "t.jsonl", list(events), self.tmp)
        calls = self.mod.calls(self.mod.load(path))
        return calls, self.mod.verifier_runs(calls, ["python -m pytest"], self.tmp)

    def test_verifier_run_reports_its_exit_code(self):
        _calls, runs = self.runs(("verifier", True), ("verifier", False))
        self.assertEqual(runs, [(0, True), (4, False)])

    def test_a_call_with_no_result_step_counts_as_unproven(self):
        """step_index has holes, so calls pair with results by position, not by index."""
        _calls, runs = self.runs(("noresult", ("verifier", True)), ("verifier", True))
        self.assertEqual(runs, [(0, False), (2, True)])

    def test_an_injected_message_does_not_become_a_result(self):
        _calls, runs = self.runs(("interrupted", ("verifier", True)))
        self.assertEqual(runs, [(0, True)])

    def test_two_calls_in_one_step_take_their_results_in_order(self):
        _calls, runs = self.runs(("multi", ("verifier", True), ("verifier", False)))
        self.assertEqual(runs, [(0, True), (0, False)])

    def test_reviewer_spawn_is_found_by_type_name(self):
        calls, _runs = self.runs(("edit", "a.py"), ("reviewer",))
        self.assertEqual(self.mod.spawns(calls, "reviewer"), [4])
        self.assertEqual(self.mod.spawns(calls, "visual-qa"), [])

    def test_edits_keep_the_first_and_last_step_per_path(self):
        calls, _runs = self.runs(("edit", "a.py"), ("edit", "b.py"), ("edit", "a.py"))
        self.assertEqual(self.mod.edits(calls, self.tmp),
                         {"a.py": (0, 8), "b.py": (4, 4)})

    def opaque(self, line, artifact="", ignored=None):
        """Step indexes this command line counts as a change, judged by its target."""
        calls = [(0, "run_command", {"CommandLine": line, "Cwd": self.tmp}, None)]
        return self.mod.opaque_changes(calls, [self.tmp], artifact, ignored)

    def test_a_write_outside_every_workspace_is_not_a_change(self):
        outside = os.path.join(tempfile.gettempdir(), "elsewhere", "diff.txt")
        self.assertEqual(self.opaque(f'git diff HEAD > "{outside}"'), [])

    def test_a_write_into_the_artifact_directory_is_not_a_change(self):
        artifact = os.path.join(self.tmp, "brain", "cid")
        self.assertEqual(self.opaque("git diff HEAD > brain/cid/diff.txt", artifact), [])

    def test_a_write_to_an_ignored_path_is_not_a_change(self):
        ignored = os.path.abspath(os.path.join(self.tmp, "scratch", "diff.txt"))
        self.assertEqual(
            self.opaque("git diff HEAD > scratch/diff.txt", ignored=lambda p: p == ignored), [])

    def test_a_target_the_line_does_not_spell_out_stays_a_change(self):
        self.assertEqual(self.opaque('git diff HEAD > "$out"'), [0])
        self.assertEqual(self.opaque("git diff HEAD | tee $(mktemp)"), [0])

    def test_a_write_to_a_workspace_file_stays_a_change(self):
        for line in ("git diff HEAD > diff.txt", "git diff HEAD | tee notes.txt",
                     "git diff HEAD | Out-File -Encoding utf8 loop_diff.txt",
                     "Copy-Item game/a.rpy game/b.rpy", "sed -i 's/a/b/' game/a.rpy"):
            with self.subTest(line=line):
                self.assertEqual(self.opaque(line), [0], line)

    def test_restoring_an_old_revision_over_a_source_file_is_a_change(self):
        """The command only reads, but the redirect rewrites the file it names."""
        self.assertEqual(self.opaque("git show HEAD~3:game/x.rpy > game/x.rpy"), [0])

    def test_a_discarded_redirect_is_not_a_change(self):
        self.assertEqual(self.opaque("git diff HEAD > /dev/null"), [])

    def subagent_record(self, cid, type_name):
        records = Path(self.tmp) / "parent" / ".system_generated" / "subagents"
        records.mkdir(parents=True, exist_ok=True)
        (records / f"{cid}.json").write_text(json.dumps(
            {"subagentDescriptor": {"typeName": type_name}}), encoding="utf-8")

    def test_subagent_is_recognised_by_its_parents_record(self):
        self.subagent_record("child-cid", "reviewer")
        self.assertTrue(self.mod.is_subagent("child-cid", self.tmp))

    def test_conversation_without_a_record_is_top_level(self):
        self.subagent_record("child-cid", "reviewer")
        self.assertFalse(self.mod.is_subagent("other-cid", self.tmp))

    def test_brain_root_is_four_levels_above_the_transcript(self):
        path = "/data/brain/cid/.system_generated/logs/transcript.jsonl"
        self.assertEqual(self.mod.brain_root(path).replace("\\", "/"), "/data/brain")


class TestPendingVerification(unittest.TestCase):
    """A stop that leaves a gap writes a marker the next invocation announces once."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, GEMINI_HOOK_TMP=self.tmp)
        (Path(self.tmp) / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    def hook(self, script, payload):
        proc = subprocess.run([sys.executable, str(HOOKS_DIR / script)],
                              input=json.dumps(payload), text=True, capture_output=True,
                              env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def notes(self, cid="cid-1"):
        res = self.hook("reinforce.py", {"invocationNum": 5, "conversationId": cid,
                                         "workspacePaths": [self.tmp]})
        return [s["ephemeralMessage"] for s in res["injectSteps"]]

    def marker(self):
        return list((Path(self.tmp) / "pending").glob("*.json"))

    def stop(self, events, reason="ERROR", cid="gone-cid"):
        path = write_transcript(Path(self.tmp) / "t.jsonl", events, self.tmp)
        return self.hook("stop_gate.py", {
            "terminationReason": reason, "fullyIdle": True, "conversationId": cid,
            "workspacePaths": [self.tmp], "transcriptPath": path,
        })

    def test_gap_marks_and_reinforce_says_it_once_per_conversation(self):
        self.assertEqual(self.stop([("edit", "app.py")]).get("decision"), "stop")
        self.assertEqual(len(self.marker()), 1)
        data = json.loads(self.marker()[0].read_text(encoding="utf-8"))
        self.assertTrue(any("no passing verifier run" in n for n in data["notes"]), data)

        said = self.notes()
        self.assertEqual(len(said), 1)
        self.assertIn("still unverified", said[0])
        self.assertIn("no passing verifier run", said[0])
        self.assertEqual(self.notes(), [], "same conversation must not be told twice")
        self.assertEqual(len(self.notes("cid-2")), 1, "a new conversation has not heard it")
        self.assertEqual(len(self.marker()), 1, "only the gate clears a marker")

    def test_only_a_clean_stop_clears_the_marker(self):
        self.stop([("edit", "app.py")])
        self.assertEqual(len(self.marker()), 1)
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        script = "verify.cmd" if os.name == "nt" else "verify.sh"
        (agents / script).write_text("@exit /b 0\r\n" if os.name == "nt" else "exit 0\n",
                                     encoding="utf-8")
        settings = Path(self.tmp) / "cli-settings.json"
        settings.write_text(json.dumps({"trustedWorkspaces": [self.tmp]}), encoding="utf-8")
        self.env["GEMINI_CLI_SETTINGS"] = str(settings)
        res = self.stop([("edit", "app.py"), ("run", f".agents/{script}", 0), ("reviewer",)],
                        reason="NO_TOOL_CALL")
        self.assertEqual(res.get("decision"), "stop", res)
        self.assertEqual(self.marker(), [])

    def test_a_marker_older_than_a_day_is_dropped_unread(self):
        self.stop([("edit", "app.py")])
        path = self.marker()[0]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["at"] = time.time() - 90000
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.marker(), [])

    def test_a_stop_with_no_gap_marks_nothing(self):
        self.stop([("verifier", True)])
        self.assertEqual(self.marker(), [])


class TestGateInternals(unittest.TestCase):
    """execute, git and script_argv, called directly."""

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import stop_gate

        self.gate = stop_gate
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_spent_budget_reports_a_finding_instead_of_running(self):
        label, rc, lines = self.gate.execute(
            self.tmp, "npm test", ["npm", "test"], time.monotonic() - 1
        )
        self.assertEqual((label, rc), ("npm test", 1))
        self.assertIn("verifier budget exhausted", lines[0])

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
        import hookpaths

        settings = Path(self.tmp) / "s.json"
        settings.write_text(json.dumps({"trustedWorkspaces": [self.tmp]}), encoding="utf-8")
        real = hookpaths.CLI_SETTINGS
        self.addCleanup(setattr, hookpaths, "CLI_SETTINGS", real)
        hookpaths.CLI_SETTINGS = str(settings)

        real_which = shutil.which
        self.addCleanup(setattr, shutil, "which", real_which)
        shutil.which = lambda name, *a, **k: None if name == "bash" else real_which(name, *a, **k)
        self.assertEqual(self.gate.verifier_steps(self.tmp), [])

    def test_git_returns_none_when_it_fails(self):
        """A directory with no repo, so rev-parse exits non-zero and the filter must give up."""
        self.assertIsNone(self.gate.git(self.tmp, "rev-parse", "--show-toplevel"))
        self.assertIsNone(self.gate.changed_files(self.tmp))
