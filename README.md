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

Machine-local MCP servers go in `~/.gemini/config/mcp_config.local.json`. The installer merges it over the repo file. It is not tracked.

`AGENTS.md` in the repo root is a pointer for other tools that read that file. It is not installed.

## Verify

```bash
python scripts/ai-docs-lint.py --all
python -m unittest discover -s hooks/tests
agy mcp list
agy agents
```
