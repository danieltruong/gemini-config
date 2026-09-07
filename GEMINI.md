# Global Rules for Antigravity

## Style

- Talk short. Fragment ok. Path, command, number, unit, negation — keep exact.
- Human words (commits, PR text, docs, comments) stay plain English: short sentence, one idea each.

## Build

1. Ask: change need exist? Skip guess-work, say so one line.
2. Reuse what repo have: helper, type, constant, pattern. Search before write.
3. Prefer stdlib, then native platform feature, then installed dependency. Add dependency only when few lines cannot do job.
4. Ship shortest diff that work, after read touched code end to end.
5. Fix root cause in shared function, then check every caller.
6. Leave one runnable check behind for non-trivial logic.

## Tools

- Native tool first: `view_file`, `grep_search`, `find_by_name`, `replace_file_content`, `write_to_file`, `run_command`. Shell script only when native tool cannot.
- MCP when it cover domain: `mcpjungle` for GitHub, Jira, Confluence, context7 docs; `playwright` for local browser; `renpy` for Ren'Py project.
- Load skill before hand-roll its procedure: `caveman-commit` for commit message, `ponytail-review` or `caveman-review` for code review, `a11y-audit` for accessibility, `mcp-builder` for MCP server, `skill-creator` for new skill, `webapp-testing` for browser flow, `pdf` for PDF work.
- Give long or parallel work to subagent with fresh context: built-in `research` for codebase explore, `researcher` for external docs, `coder` for multi-file change, `reviewer` for diff review. Split task above ~45 minutes or ~3 file groups.
- Wait on harness event. Background command or subagent tell you when done.

## Process

1. Explore: read every file change touch, callers too.
2. Plan: list files and checks, one line each.
3. Execute.
4. Verify: run repo verifier. `.agents/verify.cmd`, `.ps1` or `.sh` first; else `npm run lint` and `npm test`; else `python -m pytest -q -x`.
5. Review: run `reviewer` subagent on diff, or read diff yourself when subagent unavailable. Fix findings tagged bug, security, or wrong result. Skip nits.
6. Finish.

- Fan out independent file groups to subagents with `invoke_subagent`, Workspace `branch`.
- Headless results that feed script use `--json-schema`.
- Never declare task done while verifier fail.

## Ren'Py

- SDK live at `F:/Factory/renpy`. Every project is subdirectory of it, example `F:/Factory/renpy/Birth Battle`.
- Verify change with `F:/Factory/renpy/renpy.exe "<project>" lint --error-code`, then `F:/Factory/renpy/renpy.exe "<project>" test` for Ren'Py 8.5 testcase framework. Never call `renpy.sh` on Windows: it Linux script, always fail.
- Answer question about `.rpy` file with `renpy` MCP tools, not grep: `renpy_run_lint`, `renpy_static_check`, `renpy_check_assets`, `renpy_find_symbol`, `renpy_get_call_graph`, `renpy_dump_symbols`.
- Unattended run must never call `renpy_launch_game`, `renpy_wipe_persistent`, or `renpy_build_distribution`. They need human at keyboard.
- Stop hook block finish while file you change still have lint error. Fix reported line, re-run lint, then finish.
- Keep save loadable: add new state with `define` or `default`, never rename or drop one that ship, keep `from` clause on every existing `call`.

## Models

Use `gemini-3.8-flash-high` for all Gemini work. No step down to lower Flash tier or effort. Escalate to `claude-opus-4-6-thinking` only for long autonomous task that already loop or stall once on Flash. Subagent: `reviewer` run on pro, `researcher` run on flash.

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