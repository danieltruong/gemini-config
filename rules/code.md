---
trigger: glob
globs: "*.ts, *.tsx, *.js, *.jsx, *.mjs, *.cjs, *.py, *.go, *.rs, *.java, *.cs, *.sh, *.bash, *.ps1, *.sql, *.tf, *.bicep, *.yaml, *.yml, *.json, *.toml, *.html, *.css, *.scss, *.rpy, Dockerfile, Makefile"
description: "Code style for source, config and markup files: comments and duplication."
---

# Code style

## Comments

- Comment only the non-obvious why: a workaround, a subtle invariant, a deliberate deviation. Never narrate what the code does.
- No essays in source: no history paragraph, no rationale narrative, no list of edge cases. More than 2 lines of explanation belongs in a doc under `docs/`, linked from a one-line comment.
- JSDoc and docstrings: signature plus a one-line summary. Skip parameter and return tags that restate obvious types. Full docs only on a public API another repo consumes.
- A deliberate shortcut with a known limit (global lock, O(n²) scan) gets one `ceiling:` comment: the limit and when to upgrade, for example `# ceiling: global lock, per-account locks if throughput matters`.

## Duplication

- The second copy of any logic is extracted into a shared function or constant that both callers already reach. A third copy means the extraction was skipped: fix it at the root and delete every copy.
- Two things that look alike but change for different reasons stay separate.
- Plain function over class, flat over nested.
