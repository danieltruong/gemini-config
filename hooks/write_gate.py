#!/usr/bin/env python3
"""PreToolUse hook: no backup-copy files, and no loosening a lint, format or type config."""
import fnmatch
import os
import re

import hookpaths

WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file")
CONFIGS = (
    ".eslintrc*", "eslint.config.*",
    ".prettierrc*", "prettier.config.*",
    "tsconfig*.json", "biome.json*",
    "ruff.toml", ".ruff.toml", "mypy.ini", ".editorconfig", ".stylelintrc*",
)
TOOL_SECTION = re.compile(r"^\[tool\.(?:ruff|mypy)\b", re.M)
CONFIG_REASON = ("{name} is a lint, format or type config; config protected, fix the code, "
                 "not the linter. Daniel changes this file by hand if it really must change.")


def check(tool, args):
    if tool not in WRITE_TOOLS:
        return None
    path = args.get("TargetFile") or args.get("AbsolutePath") or ""
    name = os.path.basename(path.replace("\\", "/"))
    if hookpaths.BACKUP_NAME.search(name):
        return (f"{name} is a backup or copy file. Git holds history: edit the original, "
                "delete the copy.")
    if name == "pyproject.toml":
        return CONFIG_REASON.format(name="[tool.ruff]/[tool.mypy] in pyproject.toml") \
            if TOOL_SECTION.search(hookpaths.tool_text(args)) else None
    if not any(fnmatch.fnmatch(name, pat) for pat in CONFIGS):
        return None
    # writing the first config a repo has is fine; only loosening an existing one is not
    return CONFIG_REASON.format(name=name) if os.path.isfile(path) else None


if __name__ == "__main__":
    hookpaths.pre_tool_gate(check)
