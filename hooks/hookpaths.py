#!/usr/bin/env python3
"""Paths and helpers shared by the Antigravity hooks."""
import os

TMP = os.environ.get("GEMINI_HOOK_TMP") or os.path.expanduser("~/.gemini/tmp")
CLI_SETTINGS = (os.environ.get("GEMINI_CLI_SETTINGS")
                or os.path.expanduser("~/.gemini/antigravity-cli/settings.json"))
DOCS_LINT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "ai-docs-lint.py")

INSTRUCTION_DOCS = {"GEMINI.md", "AGENTS.md", "SKILL.md"}


def is_instruction_doc(path):
    """True for GEMINI.md, AGENTS.md, SKILL.md, or any file under a dir named agents."""
    parts = path.replace("\\", "/").split("/")
    return parts[-1] in INSTRUCTION_DOCS or "agents" in parts[:-1]


def append(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
