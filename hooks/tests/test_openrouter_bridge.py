#!/usr/bin/env python3
"""Tests for scripts/openrouter/openrouter_bridge.py, with OpenRouter replaced by a fake."""
import http.client
import json
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "openrouter"))
import openrouter_bridge as bridge  # noqa: E402

GEMINI_PATH = "/v1beta/models/m:streamGenerateContent?alt=sse"


def sse(*chunks):
    return [f"data: {json.dumps(c)}\n".encode() for c in chunks] + [b"data: [DONE]\n"]


# A tool-call turn whose reasoning arrives in two fragments of one block.
TOOL_TURN = sse(
    {"choices": [{"delta": {"reasoning": "look ", "reasoning_details": [
        {"type": "reasoning.text", "index": 0, "text": "look "}]}}]},
    {"choices": [{"delta": {"reasoning_details": [
        {"type": "reasoning.text", "index": 0, "text": "at a"}]}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_x1", "function": {"name": "read", "arguments": "{\"p\": \"a\"}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
)
FINAL_TURN = sse({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]})


class BridgeTest(unittest.TestCase):
    def setUp(self):
        bridge.ProxyHandler.api_key = "test"
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

    def post(self, contents, upstream):
        def fake_urlopen(req, timeout=None):
            self.sent.append(json.loads(req.data))
            return iter(upstream)

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.dict("os.environ", {"OPENROUTER_EFFORT": ""}):
            conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
            conn.request("POST", GEMINI_PATH, json.dumps({"contents": contents}),
                         {"Content-Type": "application/json"})
            body = conn.getresponse().read().decode()
            conn.close()
        return self.sent[-1], body

    def test_tool_loop_sends_reasoning_back_and_pins_providers(self):
        user = {"role": "user", "parts": [{"text": "read a"}]}
        first, body = self.post([user], TOOL_TURN)
        self.assertIn('"functionCall"', body)
        self.assertEqual(first["provider"], bridge.PROVIDER_ROUTING)
        self.assertEqual(first["reasoning"], {"effort": "high"})

        history = [
            user,
            {"role": "model", "parts": [
                {"text": "look at a", "thought": True},
                {"functionCall": {"id": "call_x1", "name": "read", "args": {"p": "a"}}}]},
            {"role": "user", "parts": [
                {"functionResponse": {"id": "call_x1", "name": "read", "response": {"ok": 1}}}]},
        ]
        second, _ = self.post(history, FINAL_TURN)
        assistant = second["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertNotIn("content", assistant)
        self.assertEqual(assistant["reasoning_details"],
                         [{"type": "reasoning.text", "index": 0, "text": "look at a"}])
        self.assertEqual(second["messages"][2]["tool_call_id"], "call_x1")

    def test_unknown_call_id_gets_no_reasoning(self):
        history = [{"role": "model", "parts": [
            {"functionCall": {"id": "call_other", "name": "read", "args": {}}}]}]
        sent, _ = self.post(history, FINAL_TURN)
        self.assertNotIn("reasoning_details", sent["messages"][0])


if __name__ == "__main__":
    unittest.main()
