# Onboarding

## 1. Prerequisites

- Python 3.10 or newer on PATH
- Git with GitHub credentials
- `agy` CLI installed
- Node.js for the Playwright MCP server (optional)
- `jq` on Linux and macOS

## 2. Install

```bash
git clone https://github.com/danieltruong/gemini-config.git ~/gemini-config
pwsh -File ~/gemini-config/install.ps1     # Windows
bash ~/gemini-config/install.sh            # Linux, macOS
```

## 3. Machine-local MCP servers

Create `~/.gemini/config/mcp_config.local.json` with any server that has machine-specific paths, then rerun the installer.

```json
{ "mcpServers": { "renpy": { "command": "python", "args": ["-m", "renpy_mcp.server"] } } }
```

## 4. Machine facts

Copy `rules/local.example.md` to `rules/local.md`, change its `trigger` to `always_on`, and fill in this machine's paths. Git ignores `rules/local.md`. The installer links `rules/` to `~/.gemini/config/rules/` and warns while `rules/local.md` is missing. It stops if that folder already exists as a real directory; move its files into `rules/` first.

## 5. CLI settings

Edit `~/.gemini/antigravity-cli/settings.json`:

- `model`: `Gemini 3.8 Flash (High)`. The settings file takes the display name from `agy models`, not the ID. Every subagent inherits this model, so it sets their thinking level too. Never pick a Low, Medium or Fast preset. Do not set a temperature; Gemini 3 needs the 1.0 default.
- `trustedWorkspaces`: list project folders. Do not trust a whole drive. A trusted folder auto-loads any `.agents/hooks.json` and MCP servers found under it.

If you also use Gemini CLI, edit `~/.gemini/settings.json`: set `model.name` to `gemini-3.8-flash` and `general.plan.modelRouting` to `false`, so plan mode does not switch models for implementation.

## 6. Verify

```bash
bash scripts/prepush.sh    # docs lint, hook tests, audit self-check
agy mcp list               # shows playwright, plus local servers
```

Subagents are `mainAgent: false`, so `agy agents` does not list them; README.md shows how to ask for the roster.

Start a session. The first reply should be terse. That confirms the PreInvocation hook fired.

Check that the rules load. Add a temporary `rules/canary.md`:

```markdown
---
trigger: always_on
---

End every reply with the word PELICAN.
```

Start a new session and ask anything. A reply that ends with PELICAN shows `~/.gemini/config/rules/` is read. Delete `rules/canary.md` straight after.
