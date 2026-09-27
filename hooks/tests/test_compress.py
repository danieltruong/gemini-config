#!/usr/bin/env python3
"""Tests for scripts/compress-docs.py and the vendored engine in scripts/compress/.

The end-to-end cases put a stub `agy` first on PATH, so no model is called. The
stub drops one filler phrase from the prose; the engine's own validation runs for real.
"""
import glob
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WRAPPER = SCRIPTS / "compress-docs.py"
sys.path.insert(0, str(SCRIPTS))
from compress.compress import is_sensitive_path  # noqa: E402

FILLER = "It is worth noting that "

FIXTURE = f"""# Notes

{FILLER}the deploy reads settings from one place. {FILLER}the cache is cleared on start.

## Commands

```bash
curl -s https://example.org/api/v1/items?limit=10 | head -n 5
echo "It is worth noting that this line is code"
```

{FILLER}the docs live at https://example.org/docs/guide#setup and the source at https://github.com/example/repo.
"""

# STUB_AGY_DROP drops a URL on the compress call so validation fails; STUB_AGY_FIX
# picks the fix-call reply: "same" (still broken), "good", "preamble", or "error".
STUB_AGY = r"""import json, os, sys
FILLER = %r
DROPPED = " and the source at https://github.com/example/repo"

def between(text, tag):
    return text.split("<%%s>\n" %% tag, 1)[1].split("\n</%%s>" %% tag, 1)[0]

def shorten(doc):
    out, fenced = [], False
    for line in doc.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        out.append(line if fenced or line.startswith("```") else line.replace(FILLER, ""))
    return "\n".join(out)

prompt = json.loads(sys.stdin.readline())["message"]["content"]
fix = os.environ.get("STUB_AGY_FIX", "good")
if os.environ.get("STUB_AGY_FAIL") or ("<errors>" in prompt and fix == "error"):
    result = {"status": "ERROR", "response": "", "error": "stub failure"}
elif "<errors>" in prompt:
    reply = {"same": between(prompt, "shortened"), "good": shorten(between(prompt, "original")),
             "preamble": "Here is the fixed file:\n\n" + shorten(between(prompt, "original"))}[fix]
    result = {"status": "SUCCESS", "response": reply}
else:
    reply = shorten(between(prompt, "document"))
    if os.environ.get("STUB_AGY_DROP"):
        reply = reply.replace(DROPPED, "")
    result = {"status": "SUCCESS", "response": reply}
print(json.dumps({"event": "init", "init": {}}))
print(json.dumps({"event": "result", "result": result}))
""" % FILLER


def fences(text):
    return text.split("```")[1::2]


class TestCompress(unittest.TestCase):
    def test_sensitive_names_are_refused(self):
        for name in ("feedback_quality_tokens_speed_priority.md", "design-tokens.md", "notes.md"):
            self.assertFalse(is_sensitive_path(Path("/tmp/memory") / name), name)
        for name in ("secrets.md", "api_token.txt", "token.md", "auth-token.md", ".env",
                     ".env.local", "credentials", "credentials_prod.md", "apitoken.txt"):
            self.assertTrue(is_sensitive_path(Path("/tmp/memory") / name), name)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="compress-test-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bin_dir, self.data, work = root / "bin", root / "data", root / "fixture"
        for d in (bin_dir, self.data, work):
            d.mkdir()
        (bin_dir / "agy_stub.py").write_text(STUB_AGY, encoding="utf-8")
        if os.name == "nt":
            (bin_dir / "agy.cmd").write_text(f'@"{sys.executable}" "%~dp0agy_stub.py" %*\r\n')
        else:
            stub = bin_dir / "agy"
            stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/agy_stub.py" "$@"\n')
            stub.chmod(0o755)
        self.target = work / "notes.md"
        self.target.write_text(FIXTURE, encoding="utf-8", newline="\n")
        self.env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "COMPRESS_BACKUP_DIR")}
        self.env.update(PATH=str(bin_dir) + os.pathsep + self.env["PATH"],
                        LOCALAPPDATA=str(self.data), XDG_DATA_HOME=str(self.data))

    def run_wrapper(self, **env):
        return subprocess.run([sys.executable, str(WRAPPER), str(self.target)], env={**self.env, **env},
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    def backups(self):
        return glob.glob(str(self.data / "compress-runs" / "*" / "fixture" / "notes.original.md"))

    def test_prose_shrinks_and_code_urls_and_backup_survive(self):
        p = self.run_wrapper()
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        out = self.target.read_text(encoding="utf-8")
        self.assertEqual(out.count(FILLER), 1, "prose not shortened, or code touched:\n" + out)
        self.assertEqual(fences(out), fences(FIXTURE))
        for url in ("https://example.org/api/v1/items?limit=10", "https://example.org/docs/guide#setup",
                    "https://github.com/example/repo"):
            self.assertIn(url, out)
        backups = self.backups()
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(Path(backups[0]).read_text(encoding="utf-8"), FIXTURE)

    def test_failed_model_call_leaves_the_file_alone(self):
        p = self.run_wrapper(STUB_AGY_FAIL="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("stub failure", p.stdout + p.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), FIXTURE)
        self.assertEqual(self.backups(), [])

    def test_fix_call_repairs_a_dropped_url(self):
        p = self.run_wrapper(STUB_AGY_DROP="1", STUB_AGY_FIX="good")
        self.assertEqual(p.returncode, 0, p.stdout[-800:] + p.stderr[-800:])
        self.assertIn("Fixing with Gemini", p.stdout)
        out = self.target.read_text(encoding="utf-8")
        self.assertIn("https://github.com/example/repo", out)
        self.assertEqual(out.count(FILLER), 1, out)
        self.assertEqual(len(self.backups()), 1)

    def assert_restored(self, p):
        self.assertIn("original restored", p.stdout)
        self.assertEqual(self.target.read_text(encoding="utf-8"), FIXTURE)
        self.assertEqual(self.backups(), [])

    def test_validation_failing_twice_restores_the_original(self):
        p = self.run_wrapper(STUB_AGY_DROP="1", STUB_AGY_FIX="same")
        self.assertEqual(p.returncode, 2, p.stdout[-800:] + p.stderr[-800:])
        self.assert_restored(p)

    def test_fix_with_a_preamble_is_rejected(self):
        p = self.run_wrapper(STUB_AGY_DROP="1", STUB_AGY_FIX="preamble")
        self.assertEqual(p.returncode, 2, p.stdout[-800:] + p.stderr[-800:])
        self.assertIn("Possible preamble leak", p.stdout)
        self.assert_restored(p)

    def test_error_in_fix_call_restores_the_original(self):
        p = self.run_wrapper(STUB_AGY_DROP="1", STUB_AGY_FIX="error")
        self.assertEqual(p.returncode, 1, p.stdout[-800:] + p.stderr[-800:])
        self.assertIn("stub failure", p.stdout + p.stderr)
        self.assert_restored(p)

    def test_full_stop_after_a_url_or_path_is_not_part_of_it(self):
        from compress.validate import extract_paths, extract_urls
        self.assertEqual(extract_urls("See https://example.org/docs/deploy."), {"https://example.org/docs/deploy"})
        self.assertEqual(extract_paths("Edit ./config/deploy.yaml."), {"./config/deploy.yaml"})

    def test_sdk_call_pins_thinking_level_high(self):
        import types as pytypes
        from unittest import mock
        from compress import compress as engine
        sent = {}

        class Models:
            def generate_content(self, **kw):
                sent.update(kw)
                return pytypes.SimpleNamespace(text="ok")

        genai = pytypes.ModuleType("google.genai")
        genai.Client = lambda api_key: pytypes.SimpleNamespace(models=Models())
        genai.types = pytypes.SimpleNamespace(GenerateContentConfig=dict, ThinkingConfig=dict)
        google = pytypes.ModuleType("google")
        google.genai = genai
        with mock.patch.dict(sys.modules, {"google": google, "google.genai": genai,
                                           "google.genai.types": genai.types}), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
            self.assertEqual(engine.call_model("prompt"), "ok")
        self.assertEqual(sent["config"], {"thinking_config": {"thinking_level": "high"}})
        self.assertEqual(sent["model"], os.environ.get("COMPRESS_MODEL") or engine.SDK_DEFAULT_MODEL)

    def test_all_arguments_checked_before_any_rewrite(self):
        p = subprocess.run([sys.executable, str(WRAPPER), str(self.target), str(self.target.with_name("missing.md"))],
                           env=self.env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 1)
        self.assertIn("missing.md", p.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), FIXTURE)


if __name__ == "__main__":
    unittest.main()
