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
        base = {
            "executionNum": 1, "terminationReason": "model_stop", "fullyIdle": True,
            "workspacePaths": ["/proj"],
        }
        base.update(kw)
        return base

    def write_audit(self, **fields):
        """A fresh audit report, which is what lets a code-touching session reach stop."""
        data = {"clean": True, "findings": 0, "round": 1}
        data.update(fields)
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        path = agents / "audit.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_non_renpy_workspace_stops(self):
        self.write_audit()
        res = self.run_hook(self.payload(workspacePaths=[self.tmp]))
        self.assertEqual(res.get("decision"), "stop")

    def leftover(self, name, directory=False):
        """A stray file or directory in the workspace, with a clean audit already filed."""
        self.write_audit()
        path = Path(self.tmp) / name
        path.mkdir() if directory else path.write_text("stale\n", encoding="utf-8")
        return self.run_hook(self.payload(workspacePaths=[self.tmp]))

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
        self.write_audit()
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

    def test_not_fully_idle_stops(self):
        res = self.run_hook(self.payload(fullyIdle=False))
        self.assertEqual(res.get("decision"), "stop")

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
        path = Path(self.tmp) / "t.jsonl"
        path.write_text(json.dumps({
            "tool_calls": [{"name": "write_to_file",
                            "args": {"TargetFile": json.dumps(os.path.abspath("/proj") + "\\game\\untouched.rpy")}}]
        }) + "\n", encoding="utf-8")
        res = self.run_hook(
            self.payload(transcriptPath=str(path)), stub=self.stub(["game/changed.rpy"])
        )
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("game/untouched.rpy:9", res["reason"])
        self.assertNotIn("game/changed.rpy", res["reason"])

    def transcript_writing(self, *relpaths):
        """A transcript in which this session wrote each given project-relative path."""
        path = Path(self.tmp) / "t.jsonl"
        path.write_text("\n".join(json.dumps({
            "tool_calls": [{"name": "write_to_file", "args": {
                "TargetFile": json.dumps(os.path.join(self.tmp, rel.replace("/", os.sep)))}}]
        }) for rel in relpaths) + "\n", encoding="utf-8")
        return str(path)

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
        (Path(self.tmp) / "GEMINI.md").write_text("# Rules\n\n- Keep it short.\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_writing("GEMINI.md")
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_non_instruction_file_is_not_docs_linted(self):
        (Path(self.tmp) / "notes.md").write_text("`~/.gemini/nope-does-not-exist`\n", encoding="utf-8")
        self.write_audit()
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=self.transcript_writing("notes.md")
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_docs_findings_respect_execution_cap(self):
        (Path(self.tmp) / "GEMINI.md").write_text(
            "# Rules\n\n- See `~/.gemini/nope-does-not-exist` for details.\n", encoding="utf-8"
        )
        res = self.run_hook(self.payload(
            executionNum=4, workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing("GEMINI.md"),
        ))
        self.assertEqual(res.get("decision"), "stop")

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
            workspacePaths=[self.tmp], transcriptPath=self.transcript_writing("src/app.py")
        ))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn(f"Verifier failed ({label}):", res["reason"])
        self.assertIn("FAIL", res["reason"])
        self.assertIn(f"Fix, re-run {label}, then finish.", res["reason"])

    def code_payload(self, **kw):
        """A stop after this session wrote one source file into the workspace."""
        (Path(self.tmp) / "app.py").write_text("x = 1\n", encoding="utf-8")
        kw.setdefault("workspacePaths", [self.tmp])
        kw.setdefault("transcriptPath", self.transcript_writing("app.py"))
        return self.payload(**kw)

    def test_missing_audit_report_continues(self):
        res = self.run_hook(self.code_payload(executionNum=0))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Audit round 1:", res["reason"])
        self.assertIn("re-run the verifier", res["reason"])
        self.assertIn(
            "Delegate: reviewer for findings, linter for lint fixes, tester for missing tests, "
            "visual-qa for .agents/visual.md.",
            res["reason"],
        )

    def test_stale_audit_report_continues(self):
        """A report older than the newest file this session wrote judged different code."""
        payload = self.code_payload()
        self.write_audit(round=2)
        later = time.time() + 60
        os.utime(Path(self.tmp) / "app.py", (later, later))
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Audit round 3:", res["reason"])

    def test_unclean_audit_report_continues(self):
        payload = self.code_payload()
        self.write_audit(clean=False, findings=2, round=1)
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Audit round 2:", res["reason"])

    def test_fresh_clean_audit_stops_and_report_is_deleted(self):
        payload = self.code_payload()
        report = self.write_audit()
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "stop")
        self.assertFalse(report.exists())

    def test_audit_cap_counts_rounds_not_executions(self):
        """Verifier retries burn executionNum, so the cap has to read the report's round."""
        payload = self.code_payload(executionNum=9)
        self.write_audit(clean=False, round=4)
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("Audit round 5:", res["reason"])

        self.write_audit(clean=False, round=5)
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("audit cap", (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_visual_spec_adds_visual_audit(self):
        (Path(self.tmp) / ".agents").mkdir(exist_ok=True)
        (Path(self.tmp) / ".agents" / "visual.md").write_text(
            "## Pages\n\nhttp://localhost:5173/\n\n## Accept\n\n- Nav is visible\n", encoding="utf-8"
        )
        payload = self.code_payload()
        self.write_audit(visual={"pages": 1, "failed": 1})
        res = self.run_hook(payload)
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("capture_browser_screenshot", res["reason"])

    def test_writing_the_audit_report_is_not_a_code_change(self):
        """Otherwise answering the gate would itself keep the gate open."""
        self.write_audit(clean=False)
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing(".agents/audit.json"),
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_no_visual_spec_means_no_visual_text(self):
        res = self.run_hook(self.code_payload())
        self.assertEqual(res.get("decision"), "continue")
        self.assertNotIn("capture_browser_screenshot", res["reason"])

    def test_audit_skipped_when_only_docs_touched(self):
        (Path(self.tmp) / "GEMINI.md").write_text("# Rules\n\n- Keep it short.\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            executionNum=0, workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing("GEMINI.md"),
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_untouched_workspace_is_skipped(self):
        """A session that wrote nothing here must not trigger the verifier."""
        agents = Path(self.tmp) / ".agents"
        agents.mkdir()
        (agents / "verify.sh").write_text("exit 1\n", encoding="utf-8")
        empty = Path(self.tmp) / "empty.jsonl"
        empty.write_text(json.dumps({"step_index": 0, "type": "USER_INPUT"}) + "\n", encoding="utf-8")
        res = self.run_hook(self.payload(
            executionNum=0, workspacePaths=[self.tmp], transcriptPath=str(empty)
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_execution_num_4_drops_the_verifier(self):
        res = self.run_hook(self.payload(executionNum=4), stub=self.stub(["game/changed.rpy"]))
        self.assertNotIn("Verifier failed", res.get("reason", ""))

    def test_msys_workspace_path_is_normalized(self):
        """agy started from Git Bash sends /c/Users/...; Windows Python cannot open that."""
        drive, rest = os.path.splitdrive(os.path.abspath(self.tmp))
        msys = "/" + drive[0].lower() + rest.replace("\\", "/")
        self.write_audit()
        res = self.run_hook(self.payload(workspacePaths=[msys]))
        self.assertEqual(res.get("decision"), "stop")
        logged = (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8")
        self.assertIn(os.path.abspath(self.tmp).replace("\\", "/"), logged)
        self.assertNotIn("unresolved", logged)

    def test_unresolvable_workspace_is_dropped_and_logged(self):
        res = self.run_hook(self.payload(workspacePaths=["/no/such/place"]))
        self.assertEqual(res.get("decision"), "stop")
        self.assertIn("unresolved", (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_transcript_paths_match_case_insensitively(self):
        """agy reports C:\\Users\\... while the transcript may say c:\\users\\..."""
        (Path(self.tmp) / "app.py").write_text("x = 1\n", encoding="utf-8")
        transcript = Path(self.tmp) / "t.jsonl"
        transcript.write_text(json.dumps({"tool_calls": [{"name": "write_to_file", "args": {
            "TargetFile": json.dumps(str(Path(self.tmp) / "app.py").swapcase())}}]}) + "\n",
            encoding="utf-8")
        self.write_audit()
        res = self.run_hook(self.payload(
            workspacePaths=[self.tmp], transcriptPath=str(transcript)
        ))
        expected = "stop" if os.name == "nt" else "continue"
        self.assertEqual(res.get("decision"), expected)

    def test_decisions_file_does_not_stale_the_audit(self):
        """GEMINI.md writes DECISIONS.md after audit.json, so it must not count as an edit."""
        payload = self.code_payload()
        self.write_audit()
        decisions = Path(self.tmp) / ".agents" / "DECISIONS.md"
        later = time.time() + 60
        decisions.write_text("- 2026-09-07 chose X because Y\n", encoding="utf-8")
        os.utime(decisions, (later, later))
        transcript = self.transcript_writing("app.py", ".agents/DECISIONS.md")
        res = self.run_hook(self.payload(workspacePaths=[self.tmp], transcriptPath=transcript))
        self.assertEqual(res.get("decision"), "stop")

    def test_untrusted_workspace_skips_the_verify_script(self):
        agents = Path(self.tmp) / ".agents"
        agents.mkdir(exist_ok=True)
        (agents / "verify.sh").write_text("exit 1\n", encoding="utf-8")
        res = self.run_hook(self.code_payload(), trusted=False)
        self.assertNotIn("Verifier failed", res.get("reason", ""))
        self.assertIn("untrusted workspace, verifier skipped",
                      (Path(self.tmp) / "stop_gate.log").read_text(encoding="utf-8"))

    def test_continue_reason_reports_the_remaining_budget(self):
        res = self.run_hook(self.payload(), stub=self.stub(["game/changed.rpy"]))
        self.assertIn(f"s of the {800}s gate budget left.", res["reason"])

    def test_exception_keeps_the_audit_report(self):
        report = self.write_audit()
        env = dict(os.environ, GEMINI_HOOK_TMP=self.tmp)
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)], input="not json",
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(json.loads(proc.stdout).get("decision"), "stop")
        self.assertTrue(report.exists())

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
        self.trust(self.tmp)

    def trust(self, *roots):
        import hookpaths

        settings = Path(self.tmp) / "cli-settings.json"
        settings.write_text(json.dumps({"trustedWorkspaces": list(roots)}), encoding="utf-8")
        real = hookpaths.CLI_SETTINGS
        self.addCleanup(setattr, hookpaths, "CLI_SETTINGS", real)
        hookpaths.CLI_SETTINGS = str(settings)

    def verify(self):
        return self.gate.verify(self.tmp, set(), time.monotonic() + self.gate.GATE_BUDGET)

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


class TestTouchedFiles(unittest.TestCase):
    """Transcript parsing, driven by a line captured from a real agy run."""

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transcript_sample.jsonl"
    PROJECT = "C:\\proj"

    def setUp(self):
        sys.path.insert(0, str(HOOKS_DIR))
        self.addCleanup(sys.path.remove, str(HOOKS_DIR))
        import stop_gate

        self.gate = stop_gate
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.steps = [json.loads(ln) for ln in self.FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def transcript(self, steps):
        path = Path(self.tmp) / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
        return str(path)

    def rename_tool(self, name):
        steps = json.loads(json.dumps(self.steps))
        for step in steps:
            for call in step.get("tool_calls") or []:
                call["name"] = name
        return steps

    def test_real_capture_yields_written_file(self):
        got = self.gate.touched_files(self.transcript(self.steps), self.PROJECT)
        self.assertEqual(got, {"note.rpy"})

    def test_arg_values_are_doubly_json_encoded(self):
        raw = self.steps[1]["tool_calls"][0]["args"]["TargetFile"]
        self.assertEqual(raw, json.dumps("C:\\proj\\note.rpy"))
        self.assertEqual(self.gate.unwrap(raw), "C:\\proj\\note.rpy")

    def test_path_outside_project_ignored(self):
        got = self.gate.touched_files(self.transcript(self.steps), "C:\\elsewhere")
        self.assertEqual(got, set())

    def test_read_only_tool_ignored(self):
        got = self.gate.touched_files(self.transcript(self.rename_tool("view_file")), self.PROJECT)
        self.assertEqual(got, set())

    def test_every_write_tool_counted(self):
        for name in self.gate.WRITE_TOOLS:
            with self.subTest(tool=name):
                got = self.gate.touched_files(self.transcript(self.rename_tool(name)), self.PROJECT)
                self.assertEqual(got, {"note.rpy"})

    def test_session_with_no_writes_gates_nothing(self):
        got = self.gate.touched_files(self.transcript(self.steps[:1]), self.PROJECT)
        self.assertEqual(got, set())

    def test_missing_transcript_falls_back_to_git(self):
        self.assertIsNone(self.gate.touched_files(str(Path(self.tmp) / "nope.jsonl"), self.PROJECT))

    def test_unparseable_transcript_falls_back_to_git(self):
        path = Path(self.tmp) / "junk.jsonl"
        path.write_text("not json\nalso not json\n", encoding="utf-8")
        self.assertIsNone(self.gate.touched_files(str(path), self.PROJECT))


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
        self.assertIsNone(self.gate.verify(self.tmp, set(), time.monotonic() + 600))

    def test_git_returns_none_when_it_fails(self):
        """A directory with no repo, so rev-parse exits non-zero and the filter must give up."""
        self.assertIsNone(self.gate.git(self.tmp, "rev-parse", "--show-toplevel"))
        self.assertIsNone(self.gate.changed_files(self.tmp))
