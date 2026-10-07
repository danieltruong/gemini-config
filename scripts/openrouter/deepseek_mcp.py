"""MCP server exposing subtask execution and direct model queries via OpenRouter."""

import json
import os
import signal
import subprocess
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP

import settings_lease
from openrouter_bridge import (DEFAULT_MODEL, GLM_MODEL, OPENROUTER_URL, UPSTREAM_TIMEOUT,
                               provider_routing, resolve_effort)

mcp = FastMCP("deepseek")

SUBTASK_TIMEOUT = 900  # a GLM subtask at max effort can run for many minutes


def _chat_completion(prompt: str, system_prompt: str, model: str, title: str, effort: str = "") -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return "Error: OPENROUTER_API_KEY environment variable is not set."

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "reasoning": {"effort": resolve_effort(model, effort)},
        "provider": provider_routing(model)
    }

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost",
            "X-Title": title
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            return msg.get("content") or ""
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return f"HTTP error {e.code}: {err}"
    except Exception as e:
        return f"Execution error: {e}"


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)


def run_launcher(cmd, cwd, env, timeout_seconds, settings=settings_lease.DEFAULT_SETTINGS):
    """Run a launcher; on timeout kill its whole tree (bridge and agy too) and drop its settings lease."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **group)
    try:
        out, err = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate(timeout=30)
        settings_lease.release(settings, proc.pid)  # the launcher died before its own cleanup
        raise


def _subtask_execution(prompt: str, cwd: str, model: str, timeout_seconds: int, effort: str = "",
                       allow_all_tools: bool = False) -> str:
    work_dir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        launcher = os.path.join(script_dir, "agy-deepseek.ps1")
        cmd = ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher]
    else:
        launcher = os.path.join(script_dir, "agy-deepseek.sh")
        cmd = ["bash", launcher]

    if not os.path.isfile(launcher):
        return f"Error: launcher script not found at {launcher}"

    target_effort = resolve_effort(model, effort)
    cmd += ["--agent", "harness-worker", "--effort", target_effort, "-p", prompt,
            "--output-format", "json"]
    if allow_all_tools:
        cmd.append("--dangerously-skip-permissions")

    env = os.environ.copy()
    env["OPENROUTER_MODEL"] = model

    try:
        code, out, err = run_launcher(cmd, work_dir, env, timeout_seconds)
        out = out.strip()
        if not out:
            if err:
                return f"Subtask error: {err.strip()}"
            return f"Subtask exited with code {code} and no output."

        try:
            parsed = json.loads(out)
            resp = parsed.get("response", "")
            status = parsed.get("status", "UNKNOWN")
            duration = parsed.get("duration_seconds", 0)
            return f"[{status} in {duration:.1f}s]\n{resp}"
        except json.JSONDecodeError:
            return out
    except subprocess.TimeoutExpired:
        return f"Error: subtask timed out after {timeout_seconds} seconds."
    except Exception as e:
        return f"Error launching subtask: {e}"


@mcp.tool()
def deepseek_chat(prompt: str, system_prompt: str = "", model: str = DEFAULT_MODEL, effort: str = "") -> str:
    """Direct query to DeepSeek Flash via OpenRouter for code generation, analysis, or creative writing.

    Args:
        prompt: The prompt or instructions for the model.
        system_prompt: Optional system instructions.
        model: OpenRouter model slug (defaults to deepseek/deepseek-v4.1-flash).
        effort: Reasoning effort low, high or max; empty picks the model default (GLM max, others high).
    """
    return _chat_completion(prompt, system_prompt, model, "Local DeepSeek MCP", effort)


@mcp.tool()
def glm_chat(prompt: str, system_prompt: str = "", model: str = GLM_MODEL, effort: str = "") -> str:
    """Direct query to GLM 5.3 Flash via OpenRouter for adversarial review, critique, or planning.

    Args:
        prompt: The prompt or instructions for the model.
        system_prompt: Optional system instructions.
        model: OpenRouter model slug (defaults to z-ai/glm-5.3-flash; z-ai/glm-5.3 to escalate).
        effort: Reasoning effort low, high or max; empty picks the model default (GLM max, others high).
    """
    return _chat_completion(prompt, system_prompt, model, "Local GLM MCP", effort)


@mcp.tool()
def deepseek_subtask(
    prompt: str,
    cwd: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = SUBTASK_TIMEOUT,
    effort: str = "",
    allow_all_tools: bool = False
) -> str:
    """Run an autonomous subtask using DeepSeek Flash via OpenRouter with file and shell access.

    Args:
        prompt: Task instructions for the agent to execute.
        cwd: Working directory for the subtask (defaults to current directory).
        model: OpenRouter model slug (defaults to deepseek/deepseek-v4.1-flash).
        timeout_seconds: Max seconds to wait for task completion.
        effort: Reasoning effort low, high or max; empty picks the model default (GLM max, others high).
        allow_all_tools: Approve every tool call without asking (agy --dangerously-skip-permissions).
    """
    return _subtask_execution(prompt, cwd, model, timeout_seconds, effort, allow_all_tools)


@mcp.tool()
def glm_subtask(
    prompt: str,
    cwd: str = "",
    model: str = GLM_MODEL,
    timeout_seconds: int = SUBTASK_TIMEOUT,
    effort: str = "",
    allow_all_tools: bool = False
) -> str:
    """Run an autonomous subtask using GLM 5.3 Flash via OpenRouter for critique, audit, or planning.

    Args:
        prompt: Task instructions for the agent to execute.
        cwd: Working directory for the subtask (defaults to current directory).
        model: OpenRouter model slug (defaults to z-ai/glm-5.3-flash; z-ai/glm-5.3 to escalate).
        timeout_seconds: Max seconds to wait for task completion.
        effort: Reasoning effort low, high or max; empty picks the model default (GLM max, others high).
        allow_all_tools: Approve every tool call without asking (agy --dangerously-skip-permissions).
    """
    return _subtask_execution(prompt, cwd, model, timeout_seconds, effort, allow_all_tools)


if __name__ == "__main__":
    mcp.run()
