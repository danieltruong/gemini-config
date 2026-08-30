#!/usr/bin/env python3
"""Unit tests for Antigravity hooks."""
import json
import subprocess
import sys
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

if __name__ == "__main__":
    unittest.main()
