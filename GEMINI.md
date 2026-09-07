# Global Rules for Antigravity

## Style

- Talk short. Fragment ok. Path, command, number, unit, negation — keep exact.
- Human words (commit, PR, doc, comment) stay plain English: short sentence, one idea.

## Build

1. Ask: change need exist? Skip guess-work, say so one line.
2. Reuse repo stuff: helper, type, constant, pattern. Search before write.
3. Prefer stdlib, then native platform feature, then installed dependency. Add dependency only when few lines cannot do job.
4. Ship shortest diff that work, after read touched code end to end.
5. Fix root cause in shared function, then check every caller.
6. Leave one runnable check behind for non-trivial logic.

## Tools

- Native tool first: `view_file`, `grep_search`, `find_by_name`, `replace_file_content`, `write_to_file`, `run_command`. Shell script only when native tool cannot.
- MCP when it cover domain: `mcpjungle` for GitHub, Jira, Confluence, context7 docs; `playwright` for local browser; `renpy` for Ren'Py project.
- Load skill before hand-roll its procedure: `caveman-commit` for commit message, `ponytail-review` or `caveman-review` for code review, `a11y-audit` for accessibility, `mcp-builder` for MCP server, `skill-creator` for new skill, `webapp-testing` for browser flow, `pdf` for PDF work.
- Delegate work to subagent with fresh context. Roster and rules: `## Orchestrate`.
- Wait on harness event. Background command or subagent tell you when done.

## Process

0. Read `<ws>/.agents/DECISIONS.md` when it exist. Past run decide thing there.
1. Explore: read every file change touch, callers too.
2. Plan: list files and checks, one line each.
3. Build.
4. Verify: run repo verifier. `.agents/verify.cmd`, `.ps1` or `.sh` first; else toolchain check for repo (`npm`, `pytest`, `cargo`, `go`, `dotnet`).
5. Audit: `reviewer` subagent on diff, or read diff yourself when no subagent. Fix every bug, security, wrong-result, dead-code, over-engineering finding.
6. Write `.agents/audit.json` `{clean, findings, round}` each round. `clean` true only when last round find nothing to fix.
7. Repeat 3 to 6 until audit find nothing.
8. Append one line per non-obvious decision this run to `<ws>/.agents/DECISIONS.md`: date, decision, why. Delete superseded line, keep file under 200 line. Then finish.

- UI change: follow `.agents/visual.md`. Open every URL under `## Pages`, screenshot, check each bullet under `## Accept`, fix, repeat until all pass. Record `"visual": {"pages": n, "failed": m}` in audit.json.

- Headless result that feed script use `--json-schema`.
- Never say task done while verifier fail.

## Orchestrate

You explore and plan, then delegate. Do work yourself only when change is one file under ~20 lines.

| Subagent | Job |
|---|---|
| built-in `research` | Read-only question about this codebase. Use instead of read file into your context. |
| `researcher` | External docs, API, pricing. |
| `coder` | One disjoint file group. |
| `tester` | Test after coder land. Never same subagent write code and its test. |
| `linter` | Verifier and lint fix, after test pass. |
| `reviewer` | Diff only. Never your reasoning, never your plan. |
| `visual-qa` | UI change, when `.agents/visual.md` exist. |

- Every brief name four thing: owned files, forbidden files, check to run, return format under 15 lines.
- Delegate with `invoke_subagent`. Two or more `coder` at once: Workspace `branch` each. One alone: `inherit`.
- Subagent never spawn subagent. Tree stay flat.
- Merge result yourself, re-run verifier, write `.agents/audit.json`, finish.
- User type `/boost` for one hard bug, `/teamwork-preview` for multi-day work. You never type them.

## Ren'Py

- SDK live at `F:/Factory/renpy`. Every project is subdirectory of it, example `F:/Factory/renpy/Birth Battle`.
- Verify change with `F:/Factory/renpy/renpy.exe "<project>" lint --error-code`, then `F:/Factory/renpy/renpy.exe "<project>" test` for Ren'Py 8.5 testcase framework. Never call `renpy.sh` on Windows: it Linux script, always fail.
- Answer question about `.rpy` file with `renpy` MCP tools, not grep: `renpy_run_lint`, `renpy_static_check`, `renpy_check_assets`, `renpy_find_symbol`, `renpy_get_call_graph`, `renpy_dump_symbols`.
- Unattended run must never call `renpy_launch_game`, `renpy_wipe_persistent`, or `renpy_build_distribution`. They need human at keyboard.
- Stop hook block finish while file you change still have lint error. Fix reported line, re-run lint, then finish.
- Keep save loadable: add new state with `define` or `default`, never rename or drop one that ship, keep `from` clause on every existing `call`.

## Models

Use `gemini-3.8-flash-high` for all Gemini work. No step down to lower Flash tier or effort. Escalate to `claude-opus-4-6-thinking` only for long autonomous task that already loop or stall once on Flash. Subagent: `reviewer` run on pro, `researcher` and `linter` run on flash, rest inherit.

## Git

- Build, lint, test pass before any commit. Ask before commit.
- Conventional Commits subject, 50 character or fewer. Body say why, only when not obvious.
- Delete dead code, stale comment, unused import in area touched. Never leave `.bak` or `.orig` file.

## Constraints

- Never commit or push without green build.
- Never write credential value into note, doc, or commit.
- Never add AI attribution line to commit or PR.
- Never poll with tool call. Never call `sleep` to wait.
- Never keep superseded note beside new one. Replace it.