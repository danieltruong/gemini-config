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

The repo's `mcp_config.json` holds `mcpjungle`, `playwright`, `deepseek`, `rea` and `claude-code-docs`. Create `~/.gemini/config/mcp_config.local.json` for the rest, then rerun the installer: the servers that need a local app (`reaper`, `blender`, `godot-ai`, `renpy`), a `deepseek` override when this machine needs another Python, and the `Authorization` header for `mcpjungle`. Keep that header out of the repo.

`rea` runs `ssh daniel@192.168.3.45 rea mcp`, so it needs LAN reach to that host and key auth for that user. Its ssh options (`BatchMode=yes`, `ConnectTimeout=5`) make a box without either fail within 5 seconds instead of hanging or asking for a password. To turn it off on such a box, override its `command` and `args` in `mcp_config.local.json` with a command that exits at once (on Windows: `"command": "cmd", "args": ["/c", "exit"]`), then rerun the installer. The installer merges local entries per key, so this entry replaces the ssh command. `"rea": { "disabled": true }` does not work: it only adds that key, and agy ignores `disabled` (see README). The merge cannot remove a repo server.

```json
{ "mcpServers": { "renpy": { "command": "python", "args": ["-m", "renpy_mcp.server"] } } }
```

## 4. Machine facts

Copy `rules/local.example.md` to `rules/local.md`, change its `trigger` to `always_on`, and fill in this machine's paths. Git ignores `rules/local.md`. The installer links `rules/` to `~/.gemini/config/rules/` and warns while `rules/local.md` is missing. It stops if that folder already exists as a real directory; move its files into `rules/` first.

## 5. CLI settings

Edit `~/.gemini/antigravity-cli/settings.json`:

- `model`: `Gemini 4 Argon (Medium)`, the coordinating session. Pick it with `/model` or write it here; the settings file takes the display name from `agy models`, not the ID. Subagents do not inherit it: each agent file sets the `flash` tier, which runs Gemini 3.8 Flash at High thinking. Do not set a temperature; Gemini models need the 1.0 default.
- `trustedWorkspaces`: list project folders. Do not trust a whole drive. A trusted folder auto-loads any `.agents/hooks.json` and MCP servers found under it.

If you also use Gemini CLI, edit `~/.gemini/settings.json`: set `model.name` to `gemini-3.8-flash` and `general.plan.modelRouting` to `false`, so plan mode does not switch models for implementation.

## 6. Verify

```bash
bash scripts/prepush.sh    # docs lint, hook tests, audit self-check
agy mcp list               # shows the repo servers, plus local ones
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
