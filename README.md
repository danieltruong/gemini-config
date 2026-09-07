# gemini-config

Portable configuration for Google Antigravity (IDE and `agy` CLI).

## Install

Windows:

```powershell
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config
& ~/gemini-config/install.ps1
```

Linux and macOS:

```bash
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config
bash ~/gemini-config/install.sh
```

## What goes where

Antigravity reads global rules from `~/.gemini/GEMINI.md` and global customizations from `~/.gemini/config/`. The installer links this repo into both.

| Repo path | Installed to | Purpose |
|---|---|---|
| `GEMINI.md` | `~/.gemini/GEMINI.md` | Global rules |
| `agents/` | `~/.gemini/config/agents/` | Subagents: `researcher`, `coder`, `reviewer` |
| `hooks.json`, `hooks/` | `~/.gemini/config/` | Lifecycle hooks |
| `skills/` | `~/.gemini/config/skills/` and `~/.agents/skills/` | Skills |
| `scripts/` | `~/.gemini/config/scripts/` | Linter, compressor, pre-push check |
| `mcp_config.json` | `~/.gemini/config/mcp_config.json` | MCP servers |

agy 1.1.27 does not dispatch `PostToolUse` hooks. `PreToolUse`, `PreInvocation` and `Stop` all run. The `ai-docs-lint` entry stays wired for a build that ships it; until then `stop-gate` runs the same check at Stop.

Machine-local MCP servers go in `~/.gemini/config/mcp_config.local.json`. The installer merges it over the repo file. It is not tracked.

`AGENTS.md` in the repo root is a pointer for other tools that read that file. It is not installed.

## Unattended run

```bash
cd "F:/Factory/renpy/Birth Battle"
agy -p "<task>" --add-dir "F:/Factory/renpy/Birth Battle" --output-format json --print-timeout 45m
```

`--add-dir` is required. Without it `workspacePaths` arrives empty and the Ren'Py lint gate finds no project to check.

A dead http MCP server makes every headless run hang until the timeout expires. Setting `"disabled": true` does not help, it is ignored. Delete the entry from `~/.gemini/config/mcp_config.json` instead.

## Verify

```bash
python scripts/ai-docs-lint.py --all
python -m unittest discover -s hooks/tests
agy mcp list
agy agents
```
