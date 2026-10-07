"""MCP server exposing subtask execution and direct model queries via OpenRouter."""

import json
import os
import subprocess
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP

from openrouter_bridge import DEFAULT_MODEL, OPENROUTER_URL, default_effort, provider_routing

mcp = FastMCP("deepseek")

GLM_MODEL = "z-ai/glm-5.3-flash"


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
        "reasoning": {"effort": effort or default_effort(model)},
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
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choice = data.get("choices", [{}])[0]
            msg = choice.get("message", {})
            return msg.get("content", "")
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        return f"HTTP error {e.code}: {err}"
    except Exception as e:
        return f"Execution error: {e}"


def _subtask_execution(prompt: str, cwd: str, model: str, timeout_seconds: int, effort: str = "") -> str:
    work_dir = cwd if cwd and os.path.isdir(cwd) else os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    launcher = os.path.join(script_dir, "agy-deepseek.ps1")

    if not os.path.isfile(launcher):
        return f"Error: launcher script not found at {launcher}"

    target_effort = effort or default_effort(model)

    cmd = [
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", launcher,
        "--agent", "harness-worker",
        "--effort", target_effort,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "json"
    ]

    env = os.environ.copy()
    env["OPENROUTER_MODEL"] = model
    env["OPENROUTER_EFFORT"] = target_effort

    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds
        )
        out = proc.stdout.strip()
        if not out:
            if proc.stderr:
                return f"Subtask error: {proc.stderr.strip()}"
            return f"Subtask exited with code {proc.returncode} and no output."

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
def deepseek_chat(prompt: str, system_prompt: str = "", model: str = DEFAULT_MODEL, effort: str = default_effort(DEFAULT_MODEL)) -> str:
    """Direct query to DeepSeek Flash via OpenRouter for code generation, analysis, or creative writing.

    Args:
        prompt: The prompt or instructions for the model.
        system_prompt: Optional system instructions.
        model: OpenRouter model slug (defaults to deepseek/deepseek-v4.1-flash).
        effort: Reasoning effort: low, high or max (defaults to high; max only to escalate).
    """
    return _chat_completion(prompt, system_prompt, model, "Local DeepSeek MCP", effort)


@mcp.tool()
def glm_chat(prompt: str, system_prompt: str = "", model: str = GLM_MODEL, effort: str = default_effort(GLM_MODEL)) -> str:
    """Direct query to GLM 5.3 Flash via OpenRouter for adversarial review, critique, or planning.

    Args:
        prompt: The prompt or instructions for the model.
        system_prompt: Optional system instructions.
        model: OpenRouter model slug (defaults to z-ai/glm-5.3-flash).
        effort: Reasoning effort: low, high or max (defaults to max; high is the cheaper fallback under test).
    """
    return _chat_completion(prompt, system_prompt, model, "Local GLM MCP", effort)


@mcp.tool()
def deepseek_subtask(
    prompt: str,
    cwd: str = "",
    model: str = DEFAULT_MODEL,
    timeout_seconds: int = 180,
    effort: str = default_effort(DEFAULT_MODEL)
) -> str:
    """Run an autonomous subtask using DeepSeek Flash via OpenRouter with file and shell access.

    Args:
        prompt: Task instructions for the agent to execute.
        cwd: Working directory for the subtask (defaults to current directory).
        model: OpenRouter model slug (defaults to deepseek/deepseek-v4.1-flash).
        timeout_seconds: Max seconds to wait for task completion.
        effort: Reasoning effort: low, high or max (defaults to high; max only to escalate).
    """
    return _subtask_execution(prompt, cwd, model, timeout_seconds, effort)


@mcp.tool()
def glm_subtask(
    prompt: str,
    cwd: str = "",
    model: str = GLM_MODEL,
    timeout_seconds: int = 180,
    effort: str = default_effort(GLM_MODEL)
) -> str:
    """Run an autonomous subtask using GLM 5.3 Flash via OpenRouter for critique, audit, or planning.

    Args:
        prompt: Task instructions for the agent to execute.
        cwd: Working directory for the subtask (defaults to current directory).
        model: OpenRouter model slug (defaults to z-ai/glm-5.3-flash).
        timeout_seconds: Max seconds to wait for task completion.
        effort: Reasoning effort: low, high or max (defaults to max; high is the cheaper fallback under test).
    """
    return _subtask_execution(prompt, cwd, model, timeout_seconds, effort)


if __name__ == "__main__":
    mcp.run()
