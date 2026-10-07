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
except ImportError:  # the verifier's python may lack mcp; under this stub the tools are plain functions
    fastmcp = types.ModuleType("mcp.server.fastmcp")
    fastmcp.FastMCP = lambda name: types.SimpleNamespace(tool=lambda: lambda fn: fn)
    sys.modules.update({"mcp": types.ModuleType("mcp"), "mcp.server": types.ModuleType("mcp.server"),
                        "mcp.server.fastmcp": fastmcp})
import deepseek_mcp  # noqa: E402

STREAM_PATH = "/v1beta/models/m:streamGenerateContent?alt=sse"
PLAIN_PATH = "/v1beta/models/m:generateContent"
PINNED = ["fp8", "bf16", "fp16"]
NO_EFFORT_ENV = {"OPENROUTER_EFFORT": ""}


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
        for patcher in (mock.patch.object(bridge.ProxyHandler, "log_message", lambda *a: None),
                        mock.patch.dict("os.environ", NO_EFFORT_ENV)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        self.server = HTTPServer(("127.0.0.1", 0), bridge.ProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @property
    def url(self):
        return "http://%s:%d" % self.server.server_address

    def post(self, contents, upstream, path=STREAM_PATH):
        def fake_urlopen(req, timeout=None):
            self.sent.append(json.loads(req.data))
            return io.BytesIO(b"".join(upstream) if isinstance(upstream, list) else upstream)

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
            conn.request("POST", path, json.dumps({"contents": contents}),
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

    def test_unknown_call_id_gets_no_reasoning_and_stale_ids_are_pruned(self):
        bridge.ProxyHandler.reasoning_by_call["call_x1"] = REASONING
        sent, _ = self.post([calls("call_other")], FINAL_TURN)
        self.assertNotIn("reasoning_details", sent["messages"][0])
        self.assertEqual(bridge.ProxyHandler.reasoning_by_call, {})

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
        bridge.ProxyHandler.target_model = "z-ai/glm-5.3"
        sent, _ = self.post([user("review")], FINAL_TURN)
        self.assertEqual(sent["reasoning"], {"effort": "max"})
        self.assertEqual(sent["provider"]["quantizations"], PINNED)

    def test_closed_weight_vendors_allow_unlabelled_endpoints(self):
        for model in ("anthropic/model-a", "openai/gpt-x", "google/gemini-x", "X-AI/grok-x"):
            self.assertEqual(bridge.provider_routing(model)["quantizations"], PINNED + ["unknown"])
        for model in ("deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "meta-llama/x",
                      "openai-community/gpt2"):
            self.assertEqual(bridge.provider_routing(model)["quantizations"], PINNED)

    def test_health_reports_model_and_effort(self):
        bridge.ProxyHandler.target_model = "z-ai/glm-5.3-flash"
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        conn.request("GET", "/health")
        self.assertEqual(json.loads(conn.getresponse().read()),
                         {"model": "z-ai/glm-5.3-flash", "effort": "max"})
        conn.close()
        with mock.patch.object(bridge.sys, "stderr", io.StringIO()):
            self.assertEqual(bridge.check_bridge(self.url, "z-ai/glm-5.3-flash"), 0)
            self.assertEqual(bridge.check_bridge(self.url, bridge.DEFAULT_MODEL), 1)
            self.assertEqual(bridge.check_bridge(self.url, "z-ai/glm-5.3-flash", "high"), 1)


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
    def start(self, tmp, name, model):
        port_file = os.path.join(tmp, name)
        env = {**os.environ, "OPENROUTER_API_KEY": "test", "OPENROUTER_EFFORT": ""}
        proc = subprocess.Popen([sys.executable, str(OPENROUTER_DIR / "openrouter_bridge.py"),
                                 "--port", "0", "--model", model, "--port-file", port_file],
                                env=env, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 15
        while not os.path.exists(port_file):
            self.assertIsNone(proc.poll(), "bridge exited")
            self.assertLess(time.monotonic(), deadline, "bridge did not write its port")
            time.sleep(0.05)
        return int(Path(port_file).read_text())

    def test_parallel_bridges_bind_free_ports_and_answer_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            ports = [self.start(tmp, "a", bridge.DEFAULT_MODEL), self.start(tmp, "b", "z-ai/glm-5.3")]
            self.assertNotEqual(ports[0], ports[1])
            with mock.patch.dict("os.environ", NO_EFFORT_ENV), \
                    mock.patch.object(bridge.sys, "stderr", io.StringIO()):
                self.assertEqual(bridge.check_bridge(f"http://127.0.0.1:{ports[0]}", bridge.DEFAULT_MODEL), 0)
                self.assertEqual(bridge.check_bridge(f"http://127.0.0.1:{ports[1]}", "z-ai/glm-5.3"), 0)
                self.assertEqual(bridge.check_bridge(f"http://127.0.0.1:{ports[1]}", bridge.DEFAULT_MODEL), 1)


class DeepseekMcpTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {**NO_EFFORT_ENV, "OPENROUTER_API_KEY": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_subtask(self, tool, **kwargs):
        done = subprocess.CompletedProcess([], 0, stdout='{"response": "ok"}', stderr="")
        with mock.patch.object(deepseek_mcp.subprocess, "run", return_value=done) as run:
            tool("task", **kwargs)
        cmd, env = run.call_args.args[0], run.call_args.kwargs["env"]
        return cmd, env

    def test_subtask_effort_follows_the_model_not_the_tool(self):
        cmd, env = self.run_subtask(deepseek_mcp.deepseek_subtask, model="z-ai/glm-5.3")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertEqual((env["OPENROUTER_MODEL"], env["OPENROUTER_EFFORT"]), ("z-ai/glm-5.3", "max"))
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
            self.assertEqual(deepseek_mcp.deepseek_chat("q", model="z-ai/glm-5.3"), "")
            deepseek_mcp.glm_chat("q", model=bridge.DEFAULT_MODEL)
        self.assertEqual([p["reasoning"]["effort"] for p in sent], ["max", "high"])
        self.assertEqual(sent[0]["provider"], {"quantizations": PINNED, "require_parameters": True})


class SettingsLeaseTest(unittest.TestCase):
    def test_last_holder_restores_and_dead_holders_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "settings.json")
            original = '{\n  "model": "m"\n}'
            Path(settings).write_bytes(original.encode())
            lease.acquire(settings, 1, alive=lambda p: True)
            lease.acquire(settings, 2, alive=lambda p: True)
            self.assertEqual(json.loads(Path(settings).read_text())["modelProvider"], lease.PROVIDER)
            lease.release(settings, 1, alive=lambda p: True)
            self.assertIn("modelProvider", Path(settings).read_text())
            lease.acquire(settings, 3, alive=lambda p: p != 2)  # holder 2 crashed
            lease.release(settings, 3, alive=lambda p: p != 2)
            self.assertEqual(Path(settings).read_bytes(), original.encode())


def installer_line(name, pattern):
    return re.search(pattern, (REPO / name).read_text(encoding="utf-8"), re.M | re.S).group(1)


SERVERS = {"mcpServers": {"a": {"args": ["~/x/y.py", "-y", 3]}, "b": {"serverUrl": "u"}}}


class InstallerHomeArgsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("jq"), "jq not installed")
    def test_install_sh_expands_home(self):
        jq_filter = installer_line("install.sh", r"^HOME_ARGS='(.*?)'$")
        out = subprocess.run(["jq", "-c", "--arg", "home", "/h", jq_filter], input=json.dumps(SERVERS),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out)["mcpServers"]["a"]["args"], ["/h/x/y.py", "-y", 3])

    @unittest.skipUnless(os.name == "nt" and shutil.which("pwsh"), "needs pwsh on Windows")
    def test_install_ps1_expands_home(self):
        func = installer_line("install.ps1", r"^(function Expand-HomeArgs.*?^\})")
        servers = json.loads(json.dumps(SERVERS))
        servers["mcpServers"]["a"]["args"].insert(1, "~\\z")
        script = (f"{func}\n$o = '{json.dumps(servers)}' | ConvertFrom-Json\n"
                  "Expand-HomeArgs $o.mcpServers 'C:\\h'\n$o | ConvertTo-Json -Depth 5 -Compress")
        out = subprocess.run(["pwsh", "-NoProfile", "-Command", script], capture_output=True, text=True,
                             check=True).stdout
        self.assertEqual(json.loads(out)["mcpServers"]["a"]["args"],
                         ["C:\\h\\x\\y.py", "C:\\h\\z", "-y", 3])


if __name__ == "__main__":
    unittest.main()
