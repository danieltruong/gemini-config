#!/usr/bin/env python3
"""PreToolUse hook: nothing published names an AI author or carries a credential value."""
import os
import re

import hookpaths

MENTION = re.compile(
    r"\b(?:claude|anthropic|gemini|antigravity|copilot|chatgpt|codex|cursor\.ai)\b"
    r"|co-authored-by:\s*.*\b(?:bot|ai)\b"
    r"|\b(?:ai|llm|agent)[- ](?:generated|assisted|written|authored|coded|drafted)\b"
    r"|\b(?:generated|written|authored|coded|drafted|built|made|produced|fixed|refactored)"
    r" (?:by|with|using|via) (?:an? |the )?(?:ai|llm|language model|coding agent|agent)\b"
    r"|generated with", re.I)
SECRETS = (
    ("AWS access key id", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("JSON web token", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Atlassian API token", re.compile(r"ATATT3[A-Za-z0-9_-]{20,}")),
)
MESSAGE_COMMANDS = re.compile(r"\bgit\s+(?:commit|tag|merge|notes)\b|\bgh\s+(?:pr|issue|release)\b")
QUOTED = r"""(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""
MESSAGE_ARG = re.compile(r"(?:-m|--message|-b|--body|-t|--title)(?:=|\s+)" + QUOTED)
HEREDOC = re.compile(r"<<-?\s*'?(\w+)'?\n(.*?)\n\1", re.S)
# config and docs about the toolchain have to name it; SDK glue has to import it
EXEMPT_DIRS = ("/.agents/", "/.gemini/", "/gemini-config/", "/sdk/", "/api/")
EXEMPT_NAMES = {"README.md", "GEMINI.md", "AGENTS.md", "SKILL.md"}


def exempt(path):
    p = "/" + os.path.abspath(path).replace("\\", "/").lstrip("/")
    name = os.path.basename(p)
    return (any(d in p for d in EXEMPT_DIRS) or name in EXEMPT_NAMES
            or "client" in name.lower())


def message_text(cmd):
    """Only the message-carrying arguments, so a path or unrelated word cannot trip the gate."""
    parts = ["".join(m) for m in MESSAGE_ARG.findall(cmd)]
    parts += [m[1] for m in HEREDOC.findall(cmd)]
    return "\n".join(parts)


def findings(text, mentions=True):
    kinds = [name for name, pat in SECRETS if pat.search(text)]
    if kinds:
        return (f"Text carries a credential value ({', '.join(kinds)}). Never write a secret "
                "into code, tests, fixtures, docs or commits. Reference the file or "
                "registration that holds it instead.")
    hits = sorted({m.group(0) for m in MENTION.finditer(text)}) if mentions else []
    if hits:
        return (f"Published text must not mention {hits}. No AI attribution, no "
                "Co-authored-by bot trailer, no session link, in code, docs or commits. "
                "Rewrite without it.")
    return None


def check(tool, args):
    if tool == "run_command":
        cmd = args.get("CommandLine", "")
        return findings(message_text(cmd)) if MESSAGE_COMMANDS.search(cmd) else None
    if tool not in ("write_to_file", "replace_file_content",
                    "multi_replace_file_content", "sed_file"):
        return None
    path = args.get("TargetFile") or args.get("AbsolutePath") or ""
    # an exempt path may name the toolchain, but no path may carry a credential
    return findings(hookpaths.tool_text(args), mentions=not (path and exempt(path)))


if __name__ == "__main__":
    hookpaths.pre_tool_gate(check)
