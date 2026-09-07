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
| `agents/` | `~/.gemini/config/agents/` | Subagents, see the roster below |
| `hooks.json`, `hooks/` | `~/.gemini/config/` | Lifecycle hooks |
| `skills/` | `~/.gemini/config/skills/` | Skills |
| `scripts/` | `~/.gemini/config/scripts/` | Linter, compressor, pre-push check, log audit |
| `mcp_config.json` | `~/.gemini/config/mcp_config.json` | MCP servers |

agy 1.1.27 does not dispatch `PostToolUse` hooks. `PreToolUse`, `PreInvocation` and `Stop` all run, so the docs lint runs inside `stop-gate` at Stop.

Machine-local MCP servers go in `~/.gemini/config/mcp_config.local.json`. The installer merges it over the repo file. It is not tracked.

`AGENTS.md` in the repo root is a pointer for other tools that read that file. It is not installed.

## Subagents

The main agent explores, plans, and delegates. Each subagent starts with a clean context and one job, so nobody inherits another agent's assumptions. None of them can spawn a subagent of its own.

| Agent | Model | Writes files | Returns |
|---|---|---|---|
| `coder` | inherit | yes, only the owned files in its brief | files changed, check results, what was left out |
| `tester` | inherit | tests only | test files changed, verifier tail, gaps left |
| `linter` | flash | yes, lint and format fixes only | pass or fail, files touched |
| `debugger` | pro | no | cause with a file and line, evidence, blast radius, fix direction |
| `reviewer` | pro | no | one line per finding, ordered by severity |
| `security-reviewer` | pro | no | one line per finding, ordered by severity |
| `visual-qa` | inherit | no | table of page, bullet, pass or fail, defect |
| `researcher` | flash | no | answer first, then one source URL per claim |

Send a failure to `debugger` before `coder` whenever the cause is unknown; guessing in an agent that can write files is how a symptom gets patched. Send the diff to `security-reviewer` as well as `reviewer` when it touches auth, input parsing, or anything reachable from the internet.

A brief has to name the owned files, the forbidden files, the check to run, and the return format. `reviewer` gets the diff and nothing else, since sharing the plan that produced the code makes it agree with the code.

All of them are `mainAgent: false`, so `agy --agent <name>` and the `/agents` picker do not see them. Those only list agents that can run a session on their own; `agy --agent tester` answers `Agent "tester" not found, falling back to default` in `cli.log` and then silently uses the default agent. To check the roster really loaded, ask for it:

```bash
MSYS_NO_PATHCONV=1 agy -p "List the names of every subagent you can invoke. Names only, comma separated. Do not use any tool."
```

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
4. `Cargo.toml`, run as `cargo test -q`
5. `go.mod`, run as `go vet ./...` then `go test ./...`
6. `*.sln` or `*.csproj`, run as `dotnet test`
7. `pyproject.toml`, `pytest.ini`, or `setup.cfg`, run as `python -m pytest -q -x`

A verify script only runs in a workspace listed in `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`; elsewhere the gate skips it and logs `untrusted workspace`. The gate also refuses to finish while the directories the session wrote still hold `.bak`, `.orig`, `.old` or `.tmp` files, an unignored `__pycache__`, or an empty directory. Nothing matching means no gate. Give a repo its own `.agents/verify.sh` to control exactly what runs. One 800 second budget covers every verifier, lint and docs run in the whole Stop pass.

## Audit loop

Once the verifier passes, a session that changed code keeps getting sent back until it reports a clean audit. The agent answers by writing `.agents/audit.json`:

```json
{"clean": false, "findings": 3, "round": 1}
```

The gate sends another round when that file is missing, when `clean` is false, or when it is stale, meaning its mtime is older than the newest file the session wrote. A stale report judged code that has since changed. `clean` is true only when the last round found nothing left to fix.

The report is per-run scratch, so the gate deletes it when it lets the agent stop. Add `.agents/audit.json` to the repo's `.gitignore`.

The verifier gate gives up after 4 attempts and the audit loop after 5, so a repo that cannot be fixed does not spin forever.

## Discipline

The rules in `GEMINI.md` that a hook cannot check are grouped in four sections. Clean: every touch deletes the dead code, stale comment and temp file it leaves behind, and fixes any doc sentence the change made false, in the same commit. No shortcuts: fix the root cause, never suppress a lint or type error, never mock around a real bug, never widen a timeout or a permission to make a check pass. Reuse: look in `~/scripts`, `<workspace>/.agents/scripts` and `~/.gemini/config/scripts` before writing a script, promote the second copy of a throwaway into a real tool, extract the second copy of any logic. Improve config: when a run trips over a missing rule, a missing permission or a repeated manual step, the fix goes into this repo and ships as its own `chore(config):` commit.

## Tool gates

Every gate is a `PreToolUse` hook that answers `allow` or `deny` with a reason the agent reads. A broken gate allows the call and prints to stderr, so a bug in a hook cannot wedge a run.

| Hook | Denies |
|---|---|
| `commit-gate` | commit subjects that are not Conventional Commits or run past 50 characters, `--no-verify` on commit, push or merge, and force pushes to `main` or `master` |
| `clock-wait-gate` | `sleep`, `Start-Sleep`, `timeout /t`, a `ping` used as a delay, and `while`/`until` polling loops; a `sleep` inside a bounded `timeout <sec>` wrapper is allowed |
| `no-ai-mentions` | AI attribution and vendor names in written files and in commit or PR text, plus credential patterns anywhere; `README.md`, `GEMINI.md`, `SKILL.md`, `.agents/`, `~/.gemini/` and SDK glue may name the tools, but nothing may carry a secret |
| `write-gate` | new `.bak`, `.orig`, `.old`, `.tmp`, `-v2`, `_backup` and `copy` files, and edits to an existing lint, format or type config, including a `[tool.ruff]` or `[tool.mypy]` section of `pyproject.toml`; creating a config a repo does not have yet is fine |
| `deny-circuit-breaker` | a tool call that has already been denied twice in the same session |

The linter rule is the point of `write-gate`: loosening the config is the cheapest way to make a check pass, so the config is off limits and the code is not.

## Weekly audit

```bash
bash scripts/agy-audit.sh --days 7
```

One page from the logs: stop gate decisions by kind, which verifiers failed, workspaces the gate never ran a verifier in, denied tool calls, and the ten most repeated `cli.log` warnings. It reports and exits 0. Its top line is the input to the `## Improve config` step in `GEMINI.md`.

## Decisions

A run that picked one approach over another writes a line about it to `<workspace>/.agents/DECISIONS.md`, with the date, the decision, and why. The next run reads that file before it starts, so it does not re-argue a settled question or quietly undo one. The file stays under 200 lines: a decision that replaces an older one deletes the line it replaced instead of stacking on top of it. Unlike the audit report, this file is meant to be committed.

## Visual audit

If a repo has `.agents/visual.md`, the audit round also asks for a screenshot pass. The file lists one URL per line under `## Pages` and the things each page has to get right under `## Accept`:

```markdown
## Pages

http://localhost:5173/
http://localhost:5173/settings

## Accept

- Nav bar is visible and not overlapping the content
- No horizontal scrollbar at 1280px wide
```

The agent opens each URL, screenshots it, checks every bullet, fixes what fails, and records the outcome in the audit report:

```json
{"clean": true, "findings": 0, "round": 2, "visual": {"pages": 2, "failed": 0}}
```

While `visual.md` exists, an audit only counts as clean when `visual.failed` is 0.

A dead http MCP server makes every headless run hang until the timeout expires. Setting `"disabled": true` does not help, it is ignored. The installer handles this: it sends a HEAD request to every `serverUrl` with a 3 second timeout and leaves the ones that do not answer out of the installed file, printing a warning that names them. Any HTTP status counts as answering, so a server that returns 404 on `/mcp` is kept.

## Verify

```bash
bash scripts/prepush.sh && agy mcp list
```

Then the subagent roster check above. `agy agents` lists nothing here, because every agent in this repo is a subagent.
