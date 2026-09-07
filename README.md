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
agy -p "<task>" --add-dir "F:/Factory/renpy/Birth Battle" --mode accept-edits \
    --output-format json --print-timeout 45m
```

`~/scripts/agy-task.sh` wraps that line. It also sets `MSYS_NO_PATHCONV=1`, which Git Bash needs or it rewrites a leading `/` argument into a Windows path.

`--add-dir` is required. Without it `workspacePaths` arrives empty and the Stop hook finds no workspace to check.

`--mode accept-edits` is required too. Print mode soft-denies every tool confirmation, so without it the agent's file writes silently do nothing and the run still reports `status: SUCCESS`. The denial only shows up in `denied_actions`, which is why `agy-task.sh` exits non-zero when that list is not empty.

Commands still need a rule in `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`; `accept-edits` does not cover them. agy 1.1.27 also soft-denies subagent invocation in print mode (`subagent_manager.go: addFromDiff`), so a headless agent reviews its own diff instead of handing it to `reviewer`.

## Verifier and review pass

The `stop-gate` hook blocks an agent from finishing while the repo's own checks fail on files it wrote this session. It picks one verifier per workspace, first match wins:

1. `.agents/verify.cmd`, `.agents/verify.ps1`, or `.agents/verify.sh`, run with the workspace as the working directory
2. a Ren'Py `game/` directory, checked with `renpy.exe <project> lint --error-code`
3. `package.json` with a `lint` or `test` script, run as `npm run lint` then `npm test`
4. `pyproject.toml`, `pytest.ini`, or `setup.cfg`, run as `python -m pytest -q -x`

Nothing matching means no gate. Give a repo its own `.agents/verify.sh` to control exactly what runs. The whole verifier gets 600 seconds.

Once the verifier passes, the first stop of a session that changed code is sent back once more to run the `reviewer` subagent over the diff. Both gates give up after 4 attempts so a broken repo cannot loop forever.

A dead http MCP server makes every headless run hang until the timeout expires. Setting `"disabled": true` does not help, it is ignored. Delete the entry from `~/.gemini/config/mcp_config.json` instead.

## Verify

```bash
python scripts/ai-docs-lint.py --all
python -m unittest discover -s hooks/tests
agy mcp list
agy agents
```
