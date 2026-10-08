#!/usr/bin/env python3
"""Tests for scripts/openrouter/, with OpenRouter replaced by a fake."""
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
OPENROUTER_DIR = REPO / "scripts" / "openrouter"
sys.path.insert(0, str(OPENROUTER_DIR))
import openrouter_bridge as bridge  # noqa: E402
import settings_lease as lease  # noqa: E402

try:
    import mcp.server.fastmcp  # noqa: F401
except ModuleNotFoundError as e:  # the verifier's python may lack mcp; under this stub the tools are plain functions
    if e.name != "mcp":
        raise
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = lambda name: types.SimpleNamespace(tool=lambda: lambda fn: fn)
    sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                        "mcp.server.fastmcp": fastmcp})
import deepseek_mcp  # noqa: E402

STREAM_PATH = "/v1beta/models/m:streamGenerateContent?alt=sse"
PLAIN_PATH = "/v1beta/models/m:generateContent"
PINNED = ["fp8", "bf16", "fp16"]
GLM = bridge.GLM_MODEL
FULL_GLM = bridge.ESCALATION_MODEL


def sse(*chunks):
    return [f"data: {json.dumps(c)}\n".encode() for c in chunks] + [b"data: [DONE]\n"]


def tool_delta(index, call_id, name, args):
    return {"index": index, "id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


# A tool-call turn whose reasoning arrives in two fragments of one block.
TOOL_TURN = sse(
    {"choices": [{"delta": {"reasoning": "look ", "reasoning_details": [
        {"type": "reasoning.text", "index": 0, "text": "look "}]}}]},
    {"choices": [{"delta": {"reasoning_details": [
        {"type": "reasoning.text", "index": 0, "text": "at a"}]}}]},
    {"choices": [{"delta": {"tool_calls": [tool_delta(0, "call_x1", "read", {"p": "a"})]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
)
TWO_CALL_TURN = sse(
    {"choices": [{"delta": {"reasoning_details": [
        {"type": "reasoning.text", "index": 0, "text": "both"}]}}]},
    {"choices": [{"delta": {"tool_calls": [tool_delta(0, "call_a", "read", {"p": "a"}),
                                           tool_delta(1, "call_b", "read", {"p": "b"})]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
)
FINAL_TURN = sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]})
REASONING = [{"type": "reasoning.text", "index": 0, "text": "why"}]


def user(text):
    return {"role": "user", "parts": [{"text": text}]}


def calls(*ids):
    return {"role": "model", "parts": [
        {"functionCall": {**({"id": i} if i else {}), "name": "read", "args": {}}} for i in ids]}


def responses(*ids):
    return {"role": "user", "parts": [
        {"functionResponse": {**({"id": i} if i else {}), "name": "read", "response": {"ok": 1}}}
        for i in ids]}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        bridge.ProxyHandler.api_key = "test"
        bridge.ProxyHandler.target_model = bridge.DEFAULT_MODEL
        bridge.ProxyHandler.target_effort = ""
        bridge.ProxyHandler.reasoning_by_call = {}
        quiet = mock.patch.object(bridge.ProxyHandler, "log_message", lambda *a: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.sent = []
        self.server = HTTPServer(("127.0.0.1", 0), bridge.ProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @property
    def url(self):
        return "http://%s:%d" % self.server.server_address

    def post(self, contents, upstream, path=STREAM_PATH, **extra):
        def fake_urlopen(req, timeout=None):
            self.sent.append(json.loads(req.data))
            return io.BytesIO(b"".join(upstream) if isinstance(upstream, list) else upstream)

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
            conn.request("POST", path, json.dumps({"contents": contents, **extra}),
                         {"Content-Type": "application/json"})
            body = conn.getresponse().read().decode()
            conn.close()
        return self.sent[-1], body

    def test_tool_loop_sends_reasoning_back_and_pins_providers(self):
        first, body = self.post([user("read a")], TOOL_TURN)
        self.assertIn('"functionCall"', body)
        self.assertEqual(first["provider"], {"quantizations": PINNED, "require_parameters": True})
        self.assertEqual(first["reasoning"], {"effort": "high"})
        self.assertNotIn("temperature", first)
        self.assertNotIn("max_tokens", first)

        history = [
            user("read a"),
            {"role": "model", "parts": [
                {"text": "look at a", "thought": True},
                {"functionCall": {"id": "call_x1", "name": "read", "args": {"p": "a"}}}]},
            responses("call_x1"),
        ]
        second, _ = self.post(history, FINAL_TURN)
        assistant = second["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertNotIn("content", assistant)
        self.assertEqual(assistant["reasoning_details"],
                         [{"type": "reasoning.text", "index": 0, "text": "look at a"}])
        self.assertEqual(second["messages"][2]["tool_call_id"], "call_x1")

    def test_generation_config_is_forwarded_when_set(self):
        sent, _ = self.post([user("hi")], FINAL_TURN,
                            generationConfig={"temperature": 0.2, "maxOutputTokens": 64})
        self.assertEqual((sent["temperature"], sent["max_tokens"]), (0.2, 64))

    def test_tool_schema_is_forwarded_from_either_key(self):
        schema = {"type": "object", "properties": {"DirectoryPath": {"type": "string"}}}
        decls = [{"name": "list_dir", "parametersJsonSchema": schema}, {"name": "read", "parameters": schema}]
        sent, _ = self.post([user("hi")], FINAL_TURN, tools=[{"functionDeclarations": decls}])
        self.assertEqual([t["function"]["parameters"] for t in sent["tools"]], [schema, schema])

    def test_unknown_call_id_gets_no_reasoning(self):
        bridge.ProxyHandler.reasoning_by_call["call_x1"] = REASONING
        sent, _ = self.post([calls("call_other")], FINAL_TURN)
        self.assertNotIn("reasoning_details", sent["messages"][0])
        # another conversation (a subagent) may still need call_x1
        self.assertIn("call_x1", bridge.ProxyHandler.reasoning_by_call)

    def test_reasoning_cache_evicts_least_recently_used(self):
        cache = {}
        bridge.remember_reasoning(cache, ["call_used", "call_first"], REASONING)
        for n in range(bridge.REASONING_CACHE_SIZE - 2):
            bridge.remember_reasoning(cache, [f"call_{n}"], REASONING)
        bridge.to_openai_messages([calls("call_used")], reasoning_by_call=cache)
        bridge.remember_reasoning(cache, ["call_new"], REASONING)
        self.assertEqual(len(cache), bridge.REASONING_CACHE_SIZE)
        self.assertNotIn("call_first", cache)
        self.assertIn("call_used", cache)

    def test_non_streaming_reply_captures_reasoning(self):
        reply = {"choices": [{"message": {
            "content": None, "reasoning_details": REASONING,
            "tool_calls": [{"id": "call_n1", "function": {"name": "read", "arguments": "{}"}}]}}]}
        _, body = self.post([user("read")], json.dumps(reply).encode(), PLAIN_PATH)
        part = json.loads(body)["candidates"][0]["content"]["parts"][0]
        self.assertEqual(part["functionCall"]["id"], "call_n1")
        sent, _ = self.post([user("read"), calls("call_n1"), responses("call_n1")], FINAL_TURN)
        self.assertEqual(sent["messages"][1]["reasoning_details"], REASONING)

    def test_turn_with_two_tool_calls(self):
        _, body = self.post([user("read a and b")], TWO_CALL_TURN)
        finish = [json.loads(line[6:]) for line in body.splitlines() if "functionCall" in line][-1]
        ids = [p["functionCall"]["id"] for p in finish["candidates"][0]["content"]["parts"]]
        self.assertEqual(ids, ["call_a", "call_b"])
        # responses without ids pair with the calls in order
        sent, _ = self.post([user("read a and b"), calls("call_a", "call_b"), responses(None, None)],
                            FINAL_TURN)
        self.assertEqual(len(sent["messages"][1]["tool_calls"]), 2)
        self.assertEqual(sent["messages"][1]["reasoning_details"][0]["text"], "both")
        self.assertEqual([m["tool_call_id"] for m in sent["messages"][2:4]], ["call_a", "call_b"])

    def test_finish_then_usage_chunk_sends_one_finish_chunk(self):
        # OpenRouter repeats finish_reason on the closing usage chunk; other providers may send null there.
        for closing_reason in ("tool_calls", None):
            with self.subTest(closing_reason=closing_reason):
                _, body = self.post([user("read a")], sse(
                    {"choices": [{"delta": {"tool_calls": [tool_delta(0, "call_r1", "read", {"p": "a"})]}}]},
                    {"choices": [{"delta": {"content": ""}, "finish_reason": "tool_calls"}]},
                    {"choices": [{"delta": {"content": ""}, "finish_reason": closing_reason}],
                     "usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
                chunks = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
                finishes = [c for c in chunks if c["candidates"][0].get("finishReason")]
                self.assertEqual(len(finishes), 1)
                sent_calls = [p["functionCall"] for c in chunks for p in c["candidates"][0]["content"]["parts"]
                              if "functionCall" in p]
                self.assertEqual([(fc["id"], fc["args"]) for fc in sent_calls], [("call_r1", {"p": "a"})])
                self.assertEqual(finishes[0]["usageMetadata"]["totalTokenCount"], 10)

    def test_responses_split_over_user_turns_pair_in_order(self):
        sent, _ = self.post([calls("call_a", "call_b"), responses(None), responses(None)], FINAL_TURN)
        self.assertEqual([m["tool_call_id"] for m in sent["messages"][1:]], ["call_a", "call_b"])

    def test_calls_without_ids_get_distinct_ids_and_no_reasoning(self):
        sent, _ = self.post([calls(None, None), responses(None, None), calls(None),
                             responses(None)], FINAL_TURN)
        call_ids = [tc["id"] for m in sent["messages"] for tc in m.get("tool_calls", [])]
        reply_ids = [m["tool_call_id"] for m in sent["messages"] if m["role"] == "tool"]
        self.assertEqual(len(set(call_ids)), 3)
        self.assertEqual(reply_ids, call_ids)
        _, body = self.post([user("go")], sse(
            {"choices": [{"delta": {"reasoning_details": REASONING, "tool_calls": [
                {"index": 0, "function": {"name": "read", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}]}))
        self.assertIn('"call_', body)
        self.assertEqual(bridge.ProxyHandler.reasoning_by_call, {})

    def test_text_beside_function_response_stays_a_user_message(self):
        turn = responses("call_x1")
        turn["parts"].append({"text": "also check b"})
        sent, _ = self.post([calls("call_x1"), turn], FINAL_TURN)
        self.assertEqual([m["role"] for m in sent["messages"]], ["assistant", "tool", "user"])
        self.assertEqual(sent["messages"][2]["content"], "also check b")

    def test_glm_defaults_to_max_and_skips_unlabelled_endpoints(self):
        bridge.ProxyHandler.target_model = FULL_GLM
        sent, _ = self.post([user("review")], FINAL_TURN)
        self.assertEqual(sent["reasoning"], {"effort": "max"})
        self.assertEqual(sent["provider"]["quantizations"], PINNED)

    def test_effort_comes_only_from_the_bridge_flag(self):
        bridge.ProxyHandler.target_effort = "low"
        with mock.patch.dict("os.environ", {"OPENROUTER_EFFORT": "max"}):
            sent, _ = self.post([user("hi")], FINAL_TURN)
        self.assertEqual(sent["reasoning"], {"effort": "low"})

    def test_closed_weight_vendors_allow_unlabelled_endpoints(self):
        for model in ("anthropic/model-a", "openai/gpt-x", "google/gemini-x", "X-AI/grok-x"):
            self.assertEqual(bridge.provider_routing(model)["quantizations"], PINNED + ["unknown"])
        for model in (bridge.DEFAULT_MODEL, GLM, "meta-llama/x", "openai-community/gpt2"):
            self.assertEqual(bridge.provider_routing(model)["quantizations"], PINNED)

    def test_health_reports_model_and_effort(self):
        bridge.ProxyHandler.target_model = GLM
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        conn.request("GET", "/health")
        self.assertEqual(json.loads(conn.getresponse().read()), {"model": GLM, "effort": "max"})
        conn.close()
        with mock.patch.object(bridge.sys, "stderr", io.StringIO()):
            self.assertEqual(bridge.check_bridge(self.url, GLM), 0)
            self.assertEqual(bridge.check_bridge(self.url, bridge.DEFAULT_MODEL), 1)
            self.assertEqual(bridge.check_bridge(self.url, GLM, "high"), 1)


class MergeReasoningTest(unittest.TestCase):
    def test_signature_and_data_fragments_join(self):
        acc = {}
        bridge.merge_reasoning_details(acc, [
            {"type": "reasoning.encrypted", "index": 0, "data": "ab", "signature": None},
            {"index": 0, "data": "cd", "signature": "sig", "format": None},
        ])
        self.assertEqual(acc, {0: {"type": "reasoning.encrypted", "data": "abcd", "signature": "sig",
                                   "index": 0}})

    def test_fragment_without_index_joins_last_block(self):
        acc = {}
        bridge.merge_reasoning_details(acc, [{"index": 0, "text": "a"}, {"index": 1, "text": "b"},
                                             {"text": "c"}])
        self.assertEqual(acc[0]["text"], "a")
        self.assertEqual(acc[1]["text"], "bc")


class BridgeProcessTest(unittest.TestCase):
    def start(self, tmp, name, model, *extra):
        port_file = os.path.join(tmp, name)
        proc = subprocess.Popen([sys.executable, str(OPENROUTER_DIR / "openrouter_bridge.py"),
                                 "--port", "0", "--model", model, "--port-file", port_file, *extra],
                                env={**os.environ, "OPENROUTER_API_KEY": "test"}, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 15
        while not os.path.exists(port_file):
            self.assertIsNone(proc.poll(), "bridge exited")
            self.assertLess(time.monotonic(), deadline, "bridge did not write its port")
            time.sleep(0.05)
        return f"http://127.0.0.1:{Path(port_file).read_text()}"

    def test_parallel_bridges_bind_free_ports_and_answer_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            urls = [self.start(tmp, "a", bridge.DEFAULT_MODEL), self.start(tmp, "b", FULL_GLM, "--effort", "low")]
            self.assertNotEqual(urls[0], urls[1])
            with mock.patch.object(bridge.sys, "stderr", io.StringIO()):
                self.assertEqual(bridge.check_bridge(urls[0], bridge.DEFAULT_MODEL), 0)
                self.assertEqual(bridge.check_bridge(urls[1], FULL_GLM, "low"), 0)
                self.assertEqual(bridge.check_bridge(urls[1], FULL_GLM), 1)
                self.assertEqual(bridge.check_bridge(urls[1], bridge.DEFAULT_MODEL, "low"), 1)


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# Starts a grandchild that outlives a plain kill of its parent, then hangs.
SLOW_LAUNCHER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


class DeepseekMcpTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_subtask(self, tool, **kwargs):
        with mock.patch.object(deepseek_mcp, "run_launcher", return_value=(0, '{"response": "ok"}', "")) as run:
            self.assertIn("ok", tool("task", **kwargs))
        cmd, _cwd, env, _timeout = run.call_args.args
        return cmd, env

    def test_subtask_effort_follows_the_model_not_the_tool(self):
        cmd, env = self.run_subtask(deepseek_mcp.deepseek_subtask, model=FULL_GLM)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertEqual(env["OPENROUTER_MODEL"], FULL_GLM)
        cmd, _ = self.run_subtask(deepseek_mcp.glm_subtask, model=bridge.DEFAULT_MODEL)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        cmd, _ = self.run_subtask(deepseek_mcp.glm_subtask, effort="low")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_subtask_skips_permissions_only_on_request(self):
        cmd, _ = self.run_subtask(deepseek_mcp.deepseek_subtask)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        cmd, _ = self.run_subtask(deepseek_mcp.deepseek_subtask, allow_all_tools=True)
        self.assertIn("--dangerously-skip-permissions", cmd)

    def test_chat_resolves_effort_by_model_and_pins_providers(self):
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(json.loads(req.data))
            return io.BytesIO(b'{"choices": [{"message": {"content": null}}]}')

        with mock.patch.object(deepseek_mcp.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(deepseek_mcp.deepseek_chat("q", model=FULL_GLM), "")
            deepseek_mcp.glm_chat("q", model=bridge.DEFAULT_MODEL)
        self.assertEqual([p["reasoning"]["effort"] for p in sent], ["max", "high"])
        self.assertEqual(sent[0]["provider"], {"quantizations": PINNED, "require_parameters": True})

    def test_timeout_kills_the_launcher_tree_and_frees_the_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings, pid_file = os.path.join(tmp, "settings.json"), os.path.join(tmp, "pid")
            lease.acquire(settings, dead_pid(), alive=lambda p: True)  # a launcher that died holding it
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                deepseek_mcp.run_launcher([sys.executable, "-c", SLOW_LAUNCHER, pid_file], tmp,
                                          dict(os.environ), 3, settings)
            self.assertLess(time.monotonic() - started, 40)
            grandchild = int(Path(pid_file).read_text())
            deadline = time.monotonic() + 10
            while lease.is_alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(lease.is_alive(grandchild))
            self.assertFalse(os.path.exists(settings))


class SettingsLeaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = os.path.join(tmp.name, "settings.json")

    def read(self):
        return json.loads(Path(self.settings).read_text(encoding="utf-8"))

    def test_last_holder_restores_only_the_key(self):
        Path(self.settings).write_text('{"model": "m", "modelProvider": "old"}', encoding="utf-8")
        lease.acquire(self.settings, 1, alive=lambda p: True)
        lease.acquire(self.settings, 2, alive=lambda p: True)
        self.assertEqual(self.read()["modelProvider"], lease.PROVIDER)
        lease.release(self.settings, 1, alive=lambda p: True)
        self.assertEqual(self.read()["modelProvider"], lease.PROVIDER)
        cfg = self.read()
        cfg["model"] = "changed during the run"
        Path(self.settings).write_text(json.dumps(cfg), encoding="utf-8")
        lease.acquire(self.settings, 3, alive=lambda p: p != 2)  # holder 2 crashed
        lease.release(self.settings, 3, alive=lambda p: p != 2)
        self.assertEqual(self.read(), {"model": "changed during the run", "modelProvider": "old"})

    def test_absent_key_is_removed_and_absent_file_deleted(self):
        Path(self.settings).write_text('{"model": "m"}', encoding="utf-8")
        lease.acquire(self.settings, 1, alive=lambda p: True)
        lease.release(self.settings, 1, alive=lambda p: True)
        self.assertEqual(self.read(), {"model": "m"})
        os.remove(self.settings)
        lease.acquire(self.settings, 1, alive=lambda p: True)
        lease.release(self.settings, 1, alive=lambda p: True)
        self.assertFalse(os.path.exists(self.settings))

    def test_non_holder_release_and_recover(self):
        lease.acquire(self.settings, 1, alive=lambda p: True)
        lease.release(self.settings, 2, alive=lambda p: True)
        self.assertIn("modelProvider", self.read())
        lease.release(self.settings, alive=lambda p: False)  # recover: holder 1 is gone
        self.assertFalse(os.path.exists(self.settings))

    def test_leftover_lock_file_does_not_block(self):
        lock, _ = lease._paths(self.settings)
        Path(lock).write_text("crashed holder")
        lease.acquire(self.settings, 1, alive=lambda p: True)
        self.assertIn("modelProvider", self.read())

    def test_held_lock_times_out(self):
        lock, _ = lease._paths(self.settings)
        with mock.patch.object(lease, "LOCK_WAIT", 0.3), lease.locked(lock):
            with self.assertRaises(SystemExit):
                lease.acquire(self.settings, 1, alive=lambda p: True)

    def test_main_rejects_bad_arguments(self):
        with mock.patch.object(lease.sys, "stderr", io.StringIO()):
            for argv in (["x"], ["x", "acquire", self.settings], ["x", "acquire", self.settings, "abc"],
                         ["x", "drop", self.settings, "1"]):
                self.assertEqual(lease.main(argv), 2, argv)
        self.assertEqual(lease.main(["x", "recover", self.settings]), 0)


def installer_block(name, pattern):
    return re.search(pattern, (REPO / name).read_text(encoding="utf-8"), re.M | re.S).group(1)


SERVERS = {"mcpServers": {"a": {"args": ["~/x/y.py", "-y", 3]}, "b": {"serverUrl": "u"}}}
needs_pwsh = unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "needs pwsh on Windows")


def run_pwsh(script):
    return json.loads(subprocess.run(["pwsh", "-NoProfile", "-Command", script], capture_output=True,
                                     text=True, check=True).stdout)


class InstallerTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("jq"), "jq not installed")
    def test_install_sh_expands_home(self):
        jq_filter = installer_block("install.sh", r"^HOME_ARGS='(.*?)'$")
        out = subprocess.run(["jq", "-c", "--arg", "home", "/h", jq_filter], input=json.dumps(SERVERS),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["mcpServers"]["a"]["args"], ["/h/x/y.py", "-y", 3])

    @needs_pwsh
    def test_install_ps1_expands_home(self):
        func = installer_block("install.ps1", r"^(function Expand-HomeArgs.*?^\})")
        servers = json.loads(json.dumps(SERVERS))
        servers["mcpServers"]["a"]["args"].insert(1, "~\\z")
        out = run_pwsh(f"{func}\n$o = '{json.dumps(servers)}' | ConvertFrom-Json\n"
                       "Expand-HomeArgs $o.mcpServers 'C:\\h'\n$o | ConvertTo-Json -Depth 5 -Compress")
        self.assertEqual(out["mcpServers"]["a"]["args"], ["C:\\h\\x\\y.py", "C:\\h\\z", "-y", 3])

    @needs_pwsh
    def test_install_ps1_merges_local_servers_per_key(self):
        func = installer_block("install.ps1", r"^(function Merge-McpServers.*?^\})")
        local = {"a": {"command": "C:\\py.exe"}, "c": {"command": "n"}}
        out = run_pwsh(f"{func}\n$o = '{json.dumps(SERVERS)}' | ConvertFrom-Json\n"
                       f"Merge-McpServers $o.mcpServers ('{json.dumps(local)}' | ConvertFrom-Json)\n"
                       "$o | ConvertTo-Json -Depth 5 -Compress")
        self.assertEqual(out["mcpServers"], {"a": {"args": ["~/x/y.py", "-y", 3], "command": "C:\\py.exe"},
                                             "b": {"serverUrl": "u"}, "c": {"command": "n"}})


INSTALL_SHELLS = [s for s in ("pwsh", "powershell") if os.name == "nt" and shutil.which(s)]


def link_target(path):
    return os.path.normcase(os.readlink(path)).removeprefix("\\\\?\\")


def squash(text):
    # Windows PowerShell wraps error text at the console width, even mid-word.
    return "".join(text.split())


@unittest.skipUnless(INSTALL_SHELLS, "needs PowerShell on Windows")
class InstallPs1LinkTest(unittest.TestCase):
    """Runs a copy of install.ps1 from a stand-in repo into temp dirs, never the real home."""

    def fresh(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        for d in ("rules", "agents", "hooks", "scripts/openrouter", "skills"):
            (self.repo / d).mkdir(parents=True)
        (self.repo / "rules" / "local.md").write_text("x")
        (self.repo / "mcp_config.json").write_text('{"mcpServers": {}}')
        for name in ("GEMINI.md", "hooks.json"):
            (self.repo / name).write_text(f"repo {name}")
        self.installer = self.repo / "install.ps1"
        shutil.copy(REPO / "install.ps1", self.installer)
        self.gemini = self.root / "gemini"
        (self.gemini / "config").mkdir(parents=True)
        self.links = {"GEMINI.md": self.gemini / "GEMINI.md", "hooks.json": self.gemini / "config" / "hooks.json"}

    def install(self, shell, runs=1):
        call = (f"& '{self.installer}' -GeminiDir '{self.gemini}' -AgentsSkillsDir '{self.root / 'skills'}' "
                f"-ScriptsDir '{self.root / 'scripts'}'")
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "; ".join([call] * runs)],
                              capture_output=True, text=True)

    def assert_linked(self, name):
        link = self.links[name]
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(link_target(link), os.path.normcase(self.repo / name))

    def test_fresh_install_links_files_and_follows_repo_edits(self):
        for shell in INSTALL_SHELLS:
            with self.subTest(shell=shell):
                self.fresh()
                # Two runs in one session: the second must leave the links alone and re-run Add-Type cleanly.
                r = self.install(shell, runs=2)
                self.assertEqual(r.returncode, 0, r.stderr)
                for name in self.links:
                    self.assert_linked(name)
                    # git checkout deletes and rewrites a file, which a hard link would not follow.
                    (self.repo / name).unlink()
                    (self.repo / name).write_text("merged")
                    self.assertEqual(self.links[name].read_text(), "merged")

    def test_correct_symlink_left_alone(self):
        for shell in INSTALL_SHELLS:
            with self.subTest(shell=shell):
                self.fresh()
                self.assertEqual(self.install(shell).returncode, 0)
                before = {n: os.lstat(p).st_ino for n, p in self.links.items()}
                r = self.install(shell)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual({n: os.lstat(p).st_ino for n, p in self.links.items()}, before)

    def test_plain_file_and_hard_link_replaced(self):
        for shell in INSTALL_SHELLS:
            with self.subTest(shell=shell):
                self.fresh()
                self.links["GEMINI.md"].write_text("old copy")
                other = self.root / "other.json"
                other.write_text("other")
                os.link(other, self.links["hooks.json"])
                r = self.install(shell)
                self.assertEqual(r.returncode, 0, r.stderr)
                for name in self.links:
                    self.assert_linked(name)
                self.assertEqual(other.read_text(), "other")

    def test_link_elsewhere_refused(self):
        for shell in INSTALL_SHELLS:
            with self.subTest(shell=shell):
                self.fresh()
                other = self.root / "other.md"
                other.write_text("other")
                os.symlink(other, self.links["GEMINI.md"])
                r = self.install(shell)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("linksto", squash(r.stdout + r.stderr))
                self.assertEqual(link_target(self.links["GEMINI.md"]), os.path.normcase(other))

    def test_missing_privilege_names_developer_mode(self):
        for shell in INSTALL_SHELLS:
            with self.subTest(shell=shell):
                self.fresh()
                text = self.installer.read_text(encoding="utf-8")
                # Flag 0 drops the unprivileged-create flag, so Windows refuses as it does without Developer Mode.
                patched = text.replace("CreateSymbolicLink($dst, $src, 2)", "CreateSymbolicLink($dst, $src, 0)")
                self.assertNotEqual(patched, text, "CreateSymbolicLink call not found in install.ps1")
                self.installer.write_text(patched, encoding="utf-8")
                r = self.install(shell)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(squash("Turn on Windows Developer Mode (Settings > System > For developers) or run elevated"),
                              squash(r.stdout + r.stderr))


if __name__ == "__main__":
    unittest.main()
