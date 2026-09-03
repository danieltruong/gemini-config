# Global Rules for Antigravity

## Style

- Reply terse. Fragments fine. Keep every path, command, number, unit, and negation exact.
- Prose that people read (commits, PR text, docs, comments) stays plain English: short sentences, one idea each.

## Build

1. Ask whether the change needs to exist. Skip speculative work and say so in one line.
2. Reuse what the repo already has: helper, type, constant, pattern. Search before writing.
3. Prefer stdlib, then native platform feature, then an installed dependency. Add a dependency only when a few lines cannot do the job.
4. Ship the shortest diff that works after reading the code it touches end to end.
5. Fix the root cause in the shared function, then check every caller.
6. Leave one runnable check behind for non-trivial logic.

## Tools

- Use native tools first: `view_file`, `grep_search`, `find_by_name`, `replace_file_content`, `write_to_file`, `run_command`. Fall back to shell scripts only when a native tool cannot do it.
- Use MCP when it covers the domain: `mcpjungle` for GitHub, Jira, Confluence, context7 docs; `playwright` for local browsers; `renpy` for Ren'Py projects.
- Load a skill before hand-rolling its procedure: `caveman-commit` for commit messages, `ponytail-review` or `caveman-review` for code review, `a11y-audit` for accessibility, `mcp-builder` for MCP servers, `skill-creator` for new skills, `webapp-testing` for browser flows, `pdf` for PDF work.
- Delegate long or parallel work to subagents with a fresh context: built-in `research` for codebase exploration, `researcher` for external docs, `coder` for a multi-file change, `reviewer` for a diff review. Split tasks above ~45 minutes or ~3 file groups.
- Wait on harness events. A background command or subagent notifies on completion.

## Models

Use `gemini-3.8-flash-high` for all Gemini work. Do not step down to a lower Flash tier or effort. Escalate to `claude-opus-4-6-thinking` only for a long autonomous task that has already looped or stalled once on Flash.

## Git

- Build, lint, and tests pass before any commit. Ask before committing.
- Conventional Commits subject, 50 characters or fewer. Body says why, only when not obvious.
- Delete dead code, stale comments, and unused imports in the area touched. Never leave `.bak` or `.orig` files.

## Constraints

- Never commit or push without a green build.
- Never write a credential value into notes, docs, or commits.
- Never add an AI attribution line to a commit or PR.
- Never poll with tool calls. Never call `sleep` to wait.
- Never keep a superseded note beside the new one. Replace it.
