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

## 4. CLI settings

Edit `~/.gemini/antigravity-cli/settings.json`:

- `model`: `Gemini 3.8 Flash (High)` for everything. The settings file takes the display name from `agy models`, not the ID. Do not step down tiers.
- `trustedWorkspaces`: list project folders. Do not trust a whole drive. A trusted folder auto-loads any `.agents/hooks.json` and MCP servers found under it.

## 5. Verify

```bash
python scripts/ai-docs-lint.py --all
python -m unittest discover -s hooks/tests
agy mcp list      # shows mcpjungle, playwright, plus local servers
agy agents        # shows researcher, coder, reviewer
```

Start a session. The first reply should be terse. That confirms the PreInvocation hook fired.
