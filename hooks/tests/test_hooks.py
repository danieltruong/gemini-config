#!/usr/bin/env python3
"""Unit tests for Antigravity hooks."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

    def test_commit_gate_valid(self):
        script = HOOKS_DIR / "commit_gate.py"
        payload = json.dumps({
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": 'git commit -m "feat(core): add new feature"'}
            }
        })
        proc = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout)
        self.assertEqual(res.get("decision"), "allow")

    def test_commit_gate_invalid_non_cc(self):
        script = HOOKS_DIR / "commit_gate.py"
        payload = json.dumps({
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": 'git commit -m "Fixed the bug"'}
            }
        })
        proc = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("not Conventional Commits", res.get("reason", ""))

    def test_commit_gate_invalid_too_long(self):
        script = HOOKS_DIR / "commit_gate.py"
        long_subject = "feat: " + "a" * 50
        payload = json.dumps({
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": f'git commit -m "{long_subject}"'}
            }
        })
        proc = subprocess.run([sys.executable, str(script)], input=payload, text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        res = json.loads(proc.stdout)
        self.assertEqual(res.get("decision"), "deny")
        self.assertIn("limit 50", res.get("reason", ""))

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


class TestStopGate(unittest.TestCase):
    """Drives the hook with sitecustomize stubs for renpy.exe, git, and the SDK probe."""

    SCRIPT = HOOKS_DIR / "stop_gate.py"
    LINT_OUT = (
        "Ren'Py lint report\n\n"
        "game/changed.rpy:4 'a' is not an image.\n\n"
        "game/untouched.rpy:9 'b' is not an image.\n"
    )

    def run_hook(self, payload, stub=None):
        env = dict(os.environ)
        env["GEMINI_HOOK_TMP"] = self.tmp
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
            "os.path.isdir = lambda p: p.replace('\\\\', '/').endswith('/game') or _isdir(p)\n"
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

    def test_non_renpy_workspace_stops(self):
        res = self.run_hook(self.payload(workspacePaths=[self.tmp]))
        self.assertEqual(res.get("decision"), "stop")

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

    def test_errors_only_in_unchanged_files_stop(self):
        res = self.run_hook(self.payload(), stub=self.stub(["game/other.rpy"]))
        self.assertEqual(res.get("decision"), "stop")

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

    def test_review_pass_fires_on_first_execution(self):
        res = self.run_hook(self.payload(
            executionNum=0, workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing("src/app.py"),
        ))
        self.assertEqual(res.get("decision"), "continue")
        self.assertIn("run the `reviewer` subagent on `git diff HEAD`", res["reason"])
        self.assertIn("Do not fix nits.", res["reason"])

    def test_review_pass_does_not_repeat(self):
        res = self.run_hook(self.payload(
            executionNum=1, workspacePaths=[self.tmp],
            transcriptPath=self.transcript_writing("src/app.py"),
        ))
        self.assertEqual(res.get("decision"), "stop")

    def test_review_skipped_when_only_docs_touched(self):
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

    def test_execution_num_4_stops(self):
        res = self.run_hook(self.payload(executionNum=4), stub=self.stub(["game/changed.rpy"]))
        self.assertEqual(res.get("decision"), "stop")

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

    def write(self, rel, text="x"):
        path = Path(self.tmp) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_verify_script_wins_over_npm(self):
        self.write(".agents/verify.sh")
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        label, _, _ = self.gate.verify(self.tmp, set())
        self.assertEqual(label, ".agents/verify.sh")
        self.assertEqual(self.ran, [".agents/verify.sh"])

    def test_cmd_preferred_over_ps1_and_sh(self):
        for name in self.gate.VERIFY_SCRIPTS:
            self.write(f".agents/{name}")
        label, _, _ = self.gate.verify(self.tmp, set())
        self.assertEqual(label, ".agents/verify.cmd")

    def test_npm_runs_lint_then_test(self):
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        self.gate.verify(self.tmp, set())
        self.assertEqual(self.ran, ["npm run lint", "npm test"])

    def test_npm_stops_at_first_failure(self):
        self.write("package.json", json.dumps({"scripts": {"lint": "x", "test": "x"}}))
        self.gate.execute = lambda ws, label, argv, deadline: (
            self.ran.append(label) or (label, 1, ["boom"])
        )
        label, rc, _ = self.gate.verify(self.tmp, set())
        self.assertEqual((label, rc), ("npm run lint", 1))
        self.assertEqual(self.ran, ["npm run lint"])

    def test_npm_without_matching_scripts_falls_through(self):
        self.write("package.json", json.dumps({"scripts": {"build": "x"}}))
        self.assertIsNone(self.gate.verify(self.tmp, set()))

    def test_pytest_markers_detected(self):
        for marker in self.gate.PYTEST_MARKERS:
            with self.subTest(marker=marker):
                shutil.rmtree(self.tmp, True)
                os.makedirs(self.tmp, exist_ok=True)
                self.ran.clear()
                self.write(marker)
                label, _, _ = self.gate.verify(self.tmp, set())
                self.assertEqual(label, "python -m pytest -q -x")

    def test_npm_wins_over_pytest(self):
        self.write("package.json", json.dumps({"scripts": {"test": "x"}}))
        self.write("pyproject.toml")
        self.gate.verify(self.tmp, set())
        self.assertEqual(self.ran, ["npm test"])

    def test_no_verifier(self):
        self.assertIsNone(self.gate.verify(self.tmp, set()))
        self.assertEqual(self.ran, [])


class TestPendingFindings(unittest.TestCase):
    """ai_docs_lint_hook parks findings; reinforce injects them once and clears them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, GEMINI_HOOK_TMP=self.tmp)
        self.pending = Path(self.tmp) / "pending_findings.txt"

    def run_hook(self, script, payload):
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / script)], input=json.dumps(payload),
            text=True, capture_output=True, env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc.stderr

    def lint_doc(self, text):
        path = Path(self.tmp) / "GEMINI.md"
        path.write_text(text, encoding="utf-8")
        return self.run_hook(
            "ai_docs_lint_hook.py",
            {"toolCall": {"name": "write_to_file", "args": {"TargetFile": str(path)}}},
        )

    def test_failing_lint_parks_findings(self):
        out, err = self.lint_doc("x" * 20000)
        self.assertEqual(out, {})
        self.assertIn("ai-docs-lint failed", self.pending.read_text(encoding="utf-8"))
        self.assertIn("ai-docs-lint failed", err)

    def test_clean_doc_parks_nothing(self):
        self.lint_doc("# Rules\n\n- Keep it short.\n")
        self.assertFalse(self.pending.exists())

    def test_reinforce_injects_and_clears_findings(self):
        self.lint_doc("x" * 20000)
        res, _ = self.run_hook("reinforce.py", {"invocationNum": 1})
        self.assertEqual(len(res["injectSteps"]), 2)
        self.assertIn("CAVEMAN", res["injectSteps"][0]["ephemeralMessage"])
        self.assertIn("ai-docs-lint failed", res["injectSteps"][1]["ephemeralMessage"])
        self.assertFalse(self.pending.exists())

    def test_reinforce_injects_once_only(self):
        self.lint_doc("x" * 20000)
        self.run_hook("reinforce.py", {"invocationNum": 1})
        res, _ = self.run_hook("reinforce.py", {"invocationNum": 2})
        self.assertEqual(len(res["injectSteps"]), 1)

    def test_reinforce_without_findings_is_unchanged(self):
        res, _ = self.run_hook("reinforce.py", {"invocationNum": 0})
        self.assertEqual(len(res["injectSteps"]), 1)


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


class TestAiDocsLintHook(unittest.TestCase):
    SCRIPT = HOOKS_DIR / "ai_docs_lint_hook.py"

    def run_hook(self, name, path):
        payload = json.dumps({"toolCall": {"name": name, "args": {"TargetFile": str(path)}}})
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)], input=payload, text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc.stderr

    def test_non_instruction_file_is_noop(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "notes.md"
        path.write_text("# notes\n", encoding="utf-8")
        out, err = self.run_hook("write_to_file", path)
        self.assertEqual(out, {})
        self.assertEqual(err, "")

    def test_missing_file_is_noop(self):
        out, err = self.run_hook("write_to_file", Path(tempfile.gettempdir()) / "nope" / "GEMINI.md")
        self.assertEqual(out, {})
        self.assertEqual(err, "")

    def test_clean_instruction_doc_is_silent(self):
        out, err = self.run_hook("replace_file_content", HOOKS_DIR.parent / "AGENTS.md")
        self.assertEqual(out, {})
        self.assertEqual(err, "")

    def test_bad_instruction_doc_reports_findings(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "GEMINI.md"
        path.write_text("x" * 20000, encoding="utf-8")
        out, err = self.run_hook("write_to_file", path)
        self.assertEqual(out, {})
        self.assertIn("ai-docs-lint failed", err)


if __name__ == "__main__":
    unittest.main()
