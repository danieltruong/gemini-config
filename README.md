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
| `rules/` | `~/.gemini/config/rules/` | Rules for one file type or topic, plus machine facts in `rules/local.md` |
| `agents/` | `~/.gemini/config/agents/` | Subagents, see the roster below |
| `hooks.json`, `hooks/` | `~/.gemini/config/` | Lifecycle hooks |
| `skills/` | `~/.gemini/config/skills/` | Skills; `commit-msg` writes commit messages the commit gate accepts |
| `scripts/` | `~/.gemini/config/scripts/` | Docs linter, docs compressor, pre-push check, hook log audit, and `agy-audit.py` for the transcript and quality audit |
| `scripts/openrouter/` | `~/scripts/openrouter/` | OpenRouter launchers (`agy-deepseek`, `agy-glm`), their protocol bridge and the `deepseek` MCP server, see `rules/openrouter.md` |
| `mcp_config.json` | `~/.gemini/config/mcp_config.json` | MCP servers |

`rules/local.md` holds this machine's paths. Git ignores it. Copy it from `rules/local.example.md`; ONBOARD.md has the steps.

agy 1.1.27 does not dispatch `PostToolUse` hooks. `PreToolUse`, `PreInvocation` and `Stop` all run, so the docs lint runs inside `stop-gate` at Stop. The docs lint also checks the frontmatter of every `rules/*.md` and the token budget of the always-on rules.

The `reinforce` hook runs at `PreInvocation`. On the first turn and every tenth it repeats three working rules: reply tersely, ship the shortest working diff, add no filler. On every turn it records the baseline `stop-gate` compares against, and once per conversation it repeats any note about work an earlier run left unchecked.

Machine-local MCP servers go in `~/.gemini/config/mcp_config.local.json`. The installer merges it over the repo file. It is not tracked. The installer also turns a leading `~/` (or `~\` on Windows) in any server's `args` into the real home path.

The `deepseek` MCP server runs on whichever `python` is first on PATH. Run `pip install mcp` with that same Python. The server reads `OPENROUTER_API_KEY` from the environment. To use another Python, override the `deepseek` entry's `command` in `mcp_config.local.json`. The same server holds the GLM tools. DeepSeek V4.1 Flash writes code and does review, planning and root-cause work. GLM 5.3 Flash writes creative text and scripts and handles images. GLM 5.3 checks security, plan verification and high-risk diffs. `rules/openrouter.md` has the full split.

`AGENTS.md` in the repo root is a pointer for other tools that read that file. It is not installed.

## Subagents

The main agent does the judgement work itself. It hands work to a subagent only for parts that can run in parallel without each other, for a check that has to be independent, or when the work would not fit in its own context. Each subagent starts with a clean context and one job, so nobody inherits another agent's assumptions. None of them can spawn a subagent of its own.

Every agent file sets `model: inherit`, so each one runs on the session model, Gemini 3.8 Flash (High). Artificial Analysis scores 3.8 Flash above 3.1 Pro (41 vs 30 on its index) and well ahead on agentic benchmarks. The thinking level behind the `flash` and `pro` tiers is not documented, and inherit is the only way to be sure a subagent runs at High. So no subagent sets a Gemini tier. The exception is an OpenRouter model pinned by `rules/openrouter.md`; `agy-audit.py` does not count those as drift.

| Agent | Writes files | Returns |
|---|---|---|
| `investigator` | no | table of file and line for each answer |
| `builder` | yes, 1 or 2 files | a short receipt of the edit, or a refusal when the job needs more files |
| `coder` | yes, only the owned files in its brief | files changed, check results, what was left out |
| `tester` | tests only | test files changed, verifier tail, gaps left |
| `linter` | yes, lint and format fixes only | pass or fail, files touched |
| `debugger` | no | cause with a file and line, evidence, blast radius, fix direction |
| `reviewer` | no | one line per finding, ordered by severity |
| `security-reviewer` | no | one line per finding, ordered by severity |
| `visual-qa` | no | table of page, bullet, pass or fail, defect |
| `researcher` | no | answer first, then one source URL per claim |

Diagnose an unknown cause before any fix. The main agent does it itself, or sends it to `debugger` when the evidence would flood its context or needs an independent look; guessing in an agent that can write files is how a symptom gets patched. Send the diff to `security-reviewer` as well as `reviewer` when it touches auth, input parsing, or anything reachable from the internet.

A brief has to name the goal, the owned files, the forbidden files, what sibling agents own, the check to run, and the return format. `reviewer` gets the diff and nothing else, since sharing the plan that produced the code makes it agree with the code.

All of them are `mainAgent: false`, so `agy --agent <name>` and the `/agents` picker do not see them. Those only list agents that can run a session on their own; `agy --agent tester` answers `Agent "tester" not found, falling back to default` in `cli.log` and then silently uses the default agent. To check the roster really loaded, ask for it:

```bash
MSYS_NO_PATHCONV=1 agy -p "List the names of every subagent you can invoke. Names only, comma separated. Do not use any tool."
```

## Unattended run

```bash
cd "<project>"
agy -p "<task>" --add-dir "<project>" --mode accept-edits \
    --output-format json --print-timeout 45m
```

`<project>` is the project folder. This machine's Ren'Py SDK path, which holds its projects, is in `rules/local.md`.

`~/scripts/agy-task.sh` wraps that line. It also sets `MSYS_NO_PATHCONV=1`, which Git Bash needs or it rewrites a leading `/` argument into a Windows path.

`--add-dir` is required. Without it `workspacePaths` arrives empty and the Stop hook finds no workspace to check.

`--mode accept-edits` is required too. Print mode soft-denies every tool confirmation, so without it the agent's file writes silently do nothing and the run still reports `status: SUCCESS`. The denial only shows up in `denied_actions`, which is why `agy-task.sh` exits non-zero when that list is not empty.

Commands still need a rule in `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`; `accept-edits` does not cover them. agy 1.1.27 also soft-denies subagent invocation in print mode (`subagent_manager.go: addFromDiff`), so a headless agent reviews its own diff instead of handing it to `reviewer`.

## Verifier and review pass

The `stop-gate` hook blocks an agent from finishing while the repo's own checks have not passed on the tree in front of it. It picks one verifier per workspace, first match wins:

1. `.agents/verify.cmd`, `.agents/verify.ps1`, or `.agents/verify.sh`, run with the workspace as the working directory
2. a Ren'Py `game/` directory, checked with `renpy.exe <project> lint --error-code`
3. `package.json` with a `lint` or `test` script, run as `npm run lint` then `npm test`
4. `Cargo.toml`, run as `cargo test -q`
5. `go.mod`, run as `go vet ./...` then `go test ./...`
6. `*.sln` or `*.csproj`, run as `dotnet test`
7. `pyproject.toml`, `pytest.ini`, or `setup.cfg`, run as `python -m pytest -q -x`

The gate runs nothing at all in a workspace that is not listed in `trustedWorkspaces` in `~/.gemini/antigravity-cli/settings.json`. Such a workspace gets one message that says the work was not checked because the workspace is not trusted, names the verifier to run by hand and names the list to add itself to. The stop then goes through, and nothing is retried. Give a repo its own `.agents/verify.sh` to control exactly what runs. One 700 second budget covers every verifier, lint and docs run in the whole Stop pass, which leaves room under the 900 second hook timeout.

## What counts as changed

The gate does not read the transcript to work out what happened. It fingerprints each workspace with git:

- `git rev-parse HEAD` and `git diff --binary HEAD`, hashed together as the tracked part; where the workspace is the repository root the stash ref joins them, since the stash is repo-wide
- every path from `git ls-files --others --exclude-standard`, each with a hash of its content; an untracked directory git will not look inside, such as a nested repository, is walked and its files hashed; a file over 2 MB is hashed by its size, its mtime in nanoseconds and its first and last 64 KB

Every git call is scoped to the workspace with `-- .`, so a workspace that is a subdirectory of a bigger repository ignores whatever changed elsewhere in that repository. Paths git already ignores stay out of all of this by design: a repository names its own build output, and the gate takes it at its word.

A workspace has work in it when that fingerprint differs from the one this conversation started with. It does not matter what moved it: an edit tool, `echo x>app.py`, `cp`, `rm`, `patch`, `git commit`, `git stash`, `git reset --hard`, a `Set-Content` from PowerShell, or a subagent. Adding the new file to `.gitignore` does not hide it either, because `.gitignore` is itself tracked.

A dirty tree is not by itself work. A repository left with 263 changed paths from last week, in a conversation that only answers a question, moves nothing and costs one fingerprint: no verifier run, no review demanded.

The baseline is taken at the first event of a conversation, by the `PreInvocation` hook, before the turn runs. It records untracked files by content, not by name, so rewriting one of them is a change. It is also what "new since this run started" means for leftovers and for reviews.

A conversation that reaches a Stop without a baseline, because `PreInvocation` never fired or carried no workspace, gets its record there instead, marked as the fallback it is: the work has already happened, so the record cannot date it. A fallback is still a floor, so anything that moves after it is work, and the other half of the answer is the transcript holding one call that could have written: an edit tool, a command, an MCP tool or a subagent. The command text is never matched. A transcript that cannot be read is not itself work, so a pristine tree with nothing readable behind it is released with no verifier run. When there is no record of the workspace at all and no readable transcript either, the gate has nothing to date the dirt with and fails closed instead: a tree that is dirty against `HEAD` counts as work, a pristine one still goes through. With no conversation id anywhere the record is keyed on the workspace alone, so the next event has a floor to compare with. Two such conversations in one workspace share that floor and that retry ledger, so one can be asked to account for the other's dirt. The next `PreInvocation` replaces a fallback record with a real baseline.

A workspace that is not a git repository has no fingerprint, so there the edit-tool targets in the transcript are the only change signal. Such a workspace is never blocked: it gets one message saying it was not checked because it is not a git repository, and the stop goes through.

## What counts as checked

**The verifier**: the gate runs it itself and records the fingerprint it passed on. Nothing in the transcript is evidence: a `pytest || true` that reports exit 0, an `ls .agents/verify.sh`, a pass in another workspace and a backgrounded run all leave the gate to run the real thing. A fingerprint already recorded as passing is not re-run, so an unchanged tree costs no time.

The gate fingerprints the workspace again after the run. Untracked files that appeared meanwhile are that run's own output: they are stored with the record, and left out of the cache lookup, the review comparison and the leftover sweep. A `coverage.txt` written on every run therefore costs nothing after the first. A tracked file the verifier rewrote is not excused, because it moves the tracked hash that the review record is matched against. This happens on every stop the model chose, including one where `fullyIdle` is false. A stop the model did not choose (cancelled, errored, out of steps) runs nothing and only leaves a marker; a judge's stop that ended that way records no review either.

One stop runs a given workspace's verifier at a time. The lock is a file created with `O_EXCL`, so of several stops racing for it only one gets it; the others wait, then read the pass it left. Runs never overlap. Three stops finishing over one unchanged tree cost one run, measured. A failing verifier records no pass, so each waiting stop runs it again, and so does one whose verifier rewrote a tracked file, because that pass covers a different tree than the next stop is holding.

A waiting stop stops waiting the moment the pass it is waiting for lands, so a long verifier costs the others its run, not its lock. The holder touches its lock every two seconds while the run lasts, so 30 seconds means "no heartbeat for that long", not "slower than that". A lock past it is taken over by renaming it away, and only the stop whose rename landed carries on. A stop drops only the lock it wrote itself. A stop that cannot wait the lock out inside its own budget runs no verifier, logs `lock busy` and leaves the marker standing; nothing later in that stop clears a marker for a workspace whose verifier did not run. `python -m pytest` exiting 5 means it collected no tests, which is a repository without a verifier rather than a failing one, and nothing is recorded as having passed there.

**The review**: subagent Stop events reach this hook too, and the gate identifies the conversation from its parent's record under `brain/<parent>/.system_generated/subagents/<cid>.json`. When a subagent whose `typeName` is exactly `reviewer` (or `visual-qa`, where the workspace has `.agents/visual.md`) stops, the gate records the workspace fingerprint it saw. The record is only written when the tracked part is the same at the start and at the end of that subagent's run, so a `reviewer` that edits code reviews nothing, and only when that judge had a real baseline of its own. Any other type never counts, however its prompt is worded. A workspace with no fingerprint at all, because it is not a git repository, gets no credit from a stored record either.

The parent may stop when the tracked part still equals the reviewed one and no untracked file has appeared that the reviewer did not see itself. The reviewed set is the untracked files present both when the judge started and when it stopped, held by content: a file it created in its own artifact directory costs nothing, while one it created anywhere else, or one rewritten after it stopped, is new work its parent still has to account for.

One judge run covers every workspace in its own payload, because that is the only list it is given; a workspace it was never pointed at gets nothing from it. `reviewer` and `visual-qa` are the only two type names that count.

A judge owes no verifier run and no review, but its own Stop still runs the leftover sweep.

The gate never reads the reviewer's answer. A reviewer that looks at the diff and says nothing useful still satisfies this check; the gate proves that a reviewer saw this exact tree, not that it did a good job.

A subagent that is not a judge owes the verifier but not a review: the run that spawned it reviews the work. A change that touches nothing but instruction docs owes neither, because `ai-docs-lint.py` is the check for those. Instruction docs are Markdown only: `GEMINI.md`, `AGENTS.md`, `SKILL.md`, and anything under `agents/`, `skills/` or `rules/` at the repo root or under `.agents/`. `src/agents/x.py` is source code.

The gate also refuses to finish while an untracked `.bak`, `.orig`, `.old`, `.tmp` or `__pycache__` path has appeared since the conversation started. A directory that was already there, empty or not, is not the run's mess.

When `workspacePaths` arrives empty, which is most stops, the gate derives the workspaces through `git rev-parse --show-toplevel`: from the paths the edit tools named and from the working directory of the commands, since a shell write names no file at all. A derived workspace is judged only when it is dirty against `HEAD` or has moved since its record, so a `git log` in an unrelated clean repository pulls nothing in. A working directory that sits above a workspace an edit already named is that workspace's parent, not another one, and is dropped. When `conversationId` is missing it comes from the `brain/<cid>/` directory of the transcript path.

The gate forces one retry per conversation, per kind of gap, per fingerprint: fixing something moves the fingerprint and earns a fresh try. Three forced retries is the most any one conversation gets in total; after that the stop goes through with a line saying the gate has stopped asking. A release prints one line to the terminal and writes a marker in `~/.gemini/tmp/pending/` naming what is still owed. The next invocation in that workspace reads it out once per conversation; only a stop over a verified fingerprint clears it, and every state file older than a day is dropped unread. An unreadable state file counts as a retry already spent. A stop with no conversation id anywhere keeps its ledger under the workspace plus its transcript path, or the tree it saw when there is no transcript, so it still blocks once without two such stops sharing one ceiling. A marker is cleared by a stop over a verified fingerprint, by one where the workspace is back to the fingerprint the conversation started from, and by one where nothing is missing any more, which is how a marker in a repository with no verifier is cleared once a review covers the tree.

Known gap until the write gate lands: nothing stops an agent from editing `.agents/verify.*`, the files under `hooks/`, the gate's own state under `~/.gemini/tmp/`, or a subagent record under `~/.gemini/`, so a determined agent can still forge what this gate reads. Accepted with it: in a trusted workspace the gate runs code the repository controls, on every stop the model chose, including one where `fullyIdle` is false.

## Discipline

The rules in `GEMINI.md` that a hook cannot check are grouped in four sections. Constraints: never suppress a lint or type error, never mock around a real bug, never widen a timeout or a permission to make a check pass. Clean: every touch deletes the dead code, stale comment and temp file it leaves behind, and fixes any doc sentence the change made false, in the same commit. Tools: look in `~/scripts`, `<workspace>/.agents/scripts` and `~/.gemini/config/scripts` before writing a script, and promote the second copy of a throwaway into a real tool. Improve config: when a run trips over a missing rule, a missing permission or a repeated manual step, the fix goes into this repo and ships as its own `chore(config):` commit.

The build ladder, the comment rule and the rule to extract the second copy of any logic are in `rules/yagni.md` and `rules/code.md`.

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

## Compress instruction docs

```bash
python scripts/compress-docs.py GEMINI.md skills/commit-msg/SKILL.md
```

Rewrites each file in place in plain, short English, then checks that every code block, URL, heading and path survived. A file that fails the check twice is put back. Originals are kept in a dated folder the script prints. It uses the Gemini API when `GEMINI_API_KEY` is set and `google-genai` is installed, otherwise one headless `agy` turn. `--help` names the default models and how to change them. The engine is vendored under `scripts/compress/`; see its `NOTICE`.

## Weekly audit

```bash
bash scripts/agy-audit.sh --days 7
python scripts/agy-audit.py --days 7
```

`agy-audit.sh` prints one page from the logs: stop gate decisions by kind, which verifiers failed, workspaces the gate never ran a verifier in, denied tool calls, and the ten most repeated `cli.log` warnings. It reports and exits 0. Its top line is the input to the `## Improve config` step in `GEMINI.md`.

`agy-audit.py` reads the transcripts. It ends with a quality trend over the last 4 full weeks. When a metric got worse in both of the last 2 weeks, it flags it and lists the config commits made in those weeks.

## Decisions

A run that picked one approach over another writes a line about it to `<workspace>/.agents/DECISIONS.md`, with the date, the decision, and why. The next run reads that file before it starts, so it does not re-argue a settled question or quietly undo one. The file stays under 200 lines: a decision that replaces an older one deletes the line it replaced instead of stacking on top of it. Unlike the audit report, this file is meant to be committed.

## Visual audit

If a repo has `.agents/visual.md`, the block that asks for a reviewer also asks for a screenshot pass. The file lists one URL per line under `## Pages` and the things each page has to get right under `## Accept`:

```markdown
## Pages

http://localhost:5173/
http://localhost:5173/settings

## Accept

- Nav bar is visible and not overlapping the content
- No horizontal scrollbar at 1280px wide
```

The gate enforces it the same way as the review: with that file in the workspace, a `visual-qa` subagent has to have stopped over the current fingerprint. That agent opens each URL, screenshots it and checks every bullet; the run fixes what fails before it finishes.

A dead http MCP server makes every headless run hang until the timeout expires. Setting `"disabled": true` does not help, it is ignored. The installer handles this: it sends a HEAD request to every `serverUrl` with a 3 second timeout and leaves the ones that do not answer out of the installed file, printing a warning that names them. Any HTTP status counts as answering, so a server that returns 404 on `/mcp` is kept.

## Verify

```bash
bash scripts/prepush.sh && agy mcp list
```

Then the subagent roster check above. `agy agents` lists nothing here, because every agent in this repo is a subagent.
