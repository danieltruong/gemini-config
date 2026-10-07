"""Protocol translation proxy for CLI model requests.

Translates incoming REST API requests (/v1beta/models/...:streamGenerateContent)
to OpenRouter chat completions (/v1/chat/completions) and streams back SSE chunks.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_EFFORT = "high"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# List prices hide cheap low-bit endpoints; skip any provider that would drop reasoning or tools.
PROVIDER_ROUTING = {"quantizations": ["fp8", "bf16", "fp16", "unknown"], "require_parameters": True}


def merge_reasoning_details(acc, pieces):
    """Join streamed reasoning_details fragments into whole blocks, keyed by index."""
    for piece in pieces:
        block = acc.setdefault(piece.get("index", len(acc)), {})
        for key, value in piece.items():
            if key in ("text", "summary", "data") and isinstance(value, str):
                block[key] = block.get(key, "") + value
            else:
                block[key] = value


def to_openai_messages(contents, system_instruction=None, reasoning_by_call=None):
    messages = []
    if system_instruction:
        sys_text = ""
        for p in system_instruction.get("parts", []):
            if "text" in p:
                sys_text += p["text"]
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for item in contents:
        role = item.get("role", "user")
        if role == "model":
            role = "assistant"
        elif role not in ("user", "assistant", "system"):
            role = "user"

        parts = item.get("parts", [])
        text_content = ""
        tool_calls = []
        tool_responses = []

        for p in parts:
            if p.get("thought"):
                continue  # echoed reasoning is not reply text; reasoning_details carries it
            if "text" in p:
                text_content += p["text"]
            elif "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append({
                    "id": fc.get("id", f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {}))
                    }
                })
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                tool_responses.append({
                    "role": "tool",
                    "tool_call_id": fr.get("id", "call_0"),
                    "name": fr.get("name", ""),
                    "content": json.dumps(fr.get("response", {}))
                })

        if tool_responses:
            messages.extend(tool_responses)
        else:
            msg = {"role": role}
            if text_content:
                msg["content"] = text_content
            if tool_calls:
                msg["tool_calls"] = tool_calls
                # Gemini-format history has no reasoning_details; OpenRouter wants them back in tool loops.
                details = (reasoning_by_call or {}).get(tool_calls[0]["id"])
                if details:
                    msg["reasoning_details"] = details
            if "content" in msg or "tool_calls" in msg:
                messages.append(msg)

    return messages


def to_openai_tools(tools_list):
    openai_tools = []
    if not tools_list:
        return openai_tools

    for t in tools_list:
        for decl in t.get("functionDeclarations", []):
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": decl.get("name", ""),
                    "description": decl.get("description", ""),
                    "parameters": decl.get("parameters", {"type": "object", "properties": {}})
                }
            })
    return openai_tools


class ProxyHandler(BaseHTTPRequestHandler):
    target_model = DEFAULT_MODEL
    target_effort = ""
    api_key = ""
    reasoning_by_call = {}  # tool call id -> reasoning_details of the turn that made it

    def log_message(self, format, *args):
        sys.stderr.write(f"[proxy] {self.command} {self.path} - {format % args}\n")

    def do_GET(self):
        if "/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            resp = {
                "models": [
                    {"name": f"models/{self.target_model}", "displayName": self.target_model}
                ]
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception as e:
            self.send_error(400, f"Invalid JSON: {e}")
            return

        is_streaming = "streamGenerateContent" in self.path or "alt=sse" in self.path
        messages = to_openai_messages(
            req_data.get("contents", []),
            req_data.get("systemInstruction"),
            self.reasoning_by_call
        )
        tools = to_openai_tools(req_data.get("tools", []))

        payload = {
            "model": self.target_model,
            "messages": messages,
            "stream": is_streaming,
            "provider": PROVIDER_ROUTING
        }
        if tools:
            payload["tools"] = tools

        effort = (
            self.headers.get("x-openrouter-effort")
            or self.headers.get("x-reasoning-effort")
            or self.target_effort
            or os.environ.get("OPENROUTER_EFFORT")
            or DEFAULT_EFFORT
        )
        payload["reasoning"] = {"effort": effort}

        gen_cfg = req_data.get("generationConfig", {})
        if "temperature" in gen_cfg:
            payload["temperature"] = gen_cfg["temperature"]
        if "maxOutputTokens" in gen_cfg:
            payload["max_tokens"] = gen_cfg["maxOutputTokens"]

        req = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost",
                "X-Title": "Local Model Bridge"
            }
        )

        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            sys.stderr.write(f"[proxy] Upstream HTTP {e.code}: {err_body}\n")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(err_body.encode("utf-8"))
            return
        except Exception as e:
            sys.stderr.write(f"[proxy] Connection error: {e}\n")
            self.send_error(502, f"Upstream error: {e}")
            return

        if is_streaming:
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            tool_calls_acc = {}
            reasoning_acc = {}
            prompt_tokens = 0
            completion_tokens = 0
            had_finish = False

            for line in upstream:
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str.startswith("data: "):
                    continue
                data_part = line_str[6:].strip()
                if data_part == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_part)
                except Exception:
                    continue

                if "usage" in chunk and chunk["usage"]:
                    prompt_tokens = chunk["usage"].get("prompt_tokens", prompt_tokens)
                    completion_tokens = chunk["usage"].get("completion_tokens", completion_tokens)

                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                finish_reason = choices[0].get("finish_reason")
                merge_reasoning_details(reasoning_acc, delta.get("reasoning_details") or [])

                reasoning = delta.get("reasoning")
                if reasoning:
                    out_chunk = {
                        "candidates": [{
                            "content": {
                                "parts": [{"text": reasoning, "thought": True}],
                                "role": "model"
                            },
                            "index": 0
                        }]
                    }
                    self.wfile.write(f"data: {json.dumps(out_chunk)}\n\n".encode("utf-8"))
                    self.wfile.flush()

                text = delta.get("content")
                if text:
                    out_chunk = {
                        "candidates": [{
                            "content": {
                                "parts": [{"text": text}],
                                "role": "model"
                            },
                            "index": 0
                        }]
                    }
                    self.wfile.write(f"data: {json.dumps(out_chunk)}\n\n".encode("utf-8"))
                    self.wfile.flush()

                tcs = delta.get("tool_calls", [])
                for tc in tcs:
                    idx = tc.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc.get("id", f"call_{idx}"),
                            "name": tc.get("function", {}).get("name", ""),
                            "args_str": tc.get("function", {}).get("arguments", "")
                        }
                    else:
                        if "name" in tc.get("function", {}):
                            tool_calls_acc[idx]["name"] += tc["function"]["name"]
                        if "arguments" in tc.get("function", {}):
                            tool_calls_acc[idx]["args_str"] += tc["function"]["arguments"]

                if finish_reason:
                    had_finish = True
                    details = [reasoning_acc[i] for i in sorted(reasoning_acc)]
                    parts = []
                    for idx, tc_info in sorted(tool_calls_acc.items()):
                        if details:
                            self.reasoning_by_call[tc_info["id"]] = details
                        try:
                            parsed_args = json.loads(tc_info["args_str"])
                        except Exception:
                            parsed_args = {"raw": tc_info["args_str"]}
                        parts.append({
                            "functionCall": {
                                "id": tc_info["id"],
                                "name": tc_info["name"],
                                "args": parsed_args
                            }
                        })

                    if not parts:
                        parts = [{"text": ""}]

                    finish_chunk = {
                        "candidates": [{
                            "content": {"parts": parts, "role": "model"},
                            "finishReason": "STOP" if finish_reason in ("stop", "tool_calls") else "MAX_TOKENS",
                            "index": 0
                        }],
                        "usageMetadata": {
                            "promptTokenCount": prompt_tokens,
                            "candidatesTokenCount": completion_tokens,
                            "totalTokenCount": prompt_tokens + completion_tokens
                        }
                    }
                    self.wfile.write(f"data: {json.dumps(finish_chunk)}\n\n".encode("utf-8"))
                    self.wfile.flush()

            if not had_finish:
                fallback_chunk = {
                    "candidates": [{
                        "content": {"parts": [{"text": ""}], "role": "model"},
                        "finishReason": "STOP",
                        "index": 0
                    }],
                    "usageMetadata": {
                        "promptTokenCount": prompt_tokens,
                        "candidatesTokenCount": completion_tokens,
                        "totalTokenCount": prompt_tokens + completion_tokens
                    }
                }
                self.wfile.write(f"data: {json.dumps(fallback_chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()

            self.close_connection = True
        else:
            raw_resp = upstream.read()
            try:
                data = json.loads(raw_resp.decode("utf-8"))
                choice = data.get("choices", [{}])[0]
                msg = choice.get("message", {})
                parts = []
                if "content" in msg and msg["content"]:
                    parts.append({"text": msg["content"]})
                for tc in msg.get("tool_calls") or []:
                    if msg.get("reasoning_details"):
                        self.reasoning_by_call[tc.get("id", "")] = msg["reasoning_details"]
                    try:
                        args = json.loads(tc.get("function", {}).get("arguments", "{}"))
                    except Exception:
                        args = {}
                    parts.append({
                        "functionCall": {
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", ""),
                            "args": args
                        }
                    })

                if not parts:
                    parts = [{"text": ""}]

                out_resp = {
                    "candidates": [{
                        "content": {"parts": parts, "role": "model"},
                        "finishReason": "STOP",
                        "index": 0
                    }],
                    "usageMetadata": {
                        "promptTokenCount": data.get("usage", {}).get("prompt_tokens", 0),
                        "candidatesTokenCount": data.get("usage", {}).get("completion_tokens", 0),
                        "totalTokenCount": data.get("usage", {}).get("total_tokens", 0)
                    }
                }
                out_bytes = json.dumps(out_resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(out_bytes)
                self.close_connection = True
            except Exception as e:
                self.send_error(500, f"Error formatting response: {e}")


def main():
    parser = argparse.ArgumentParser(description="CLI to OpenRouter model proxy bridge")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGY_PROXY_PORT", 8045)))
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--model", type=str, default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--effort", type=str, default=os.environ.get("OPENROUTER_EFFORT", ""))
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.stderr.write("Error: OPENROUTER_API_KEY environment variable is not set.\n")
        sys.exit(1)

    ProxyHandler.target_model = args.model
    ProxyHandler.target_effort = args.effort
    ProxyHandler.api_key = api_key

    server = HTTPServer((args.host, args.port), ProxyHandler)
    sys.stderr.write(f"Proxy bridge listening on http://{args.host}:{args.port}\n")
    sys.stderr.write(f"Target OpenRouter model: {args.model}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nProxy stopped.\n")


if __name__ == "__main__":
    main()
