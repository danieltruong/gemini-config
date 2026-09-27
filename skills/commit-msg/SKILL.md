---
name: commit-msg
description: Write a Conventional Commits message for staged changes - short imperative subject, body only for why. Use for "commit message", "write a commit", "generate commit", or /commit-msg.
---
# Commit Message

Keep it short and exact. Explain why, not what.

## Subject
- Format: `<type>(<scope>): <imperative summary>`. Scope is optional. Types: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `chore`, `build`, `ci`, `style`, `revert`. Add `!` before the colon for a breaking change.
- Use the imperative: "add", "fix", "remove". Not "added", "adds", or "adding".
- 50 characters or fewer; the commit gate rejects longer subjects. No trailing period.

## Body
- Skip the body when the subject says everything. Add one only for a non-obvious reason, a breaking change, a migration note, or an issue reference.
- Always add a body for a breaking change, a security fix, a data migration, or a revert.
- Wrap at 72 characters. Use `-` for bullets, not `*`. Put issue references last: `Closes #42`, `Refs #17`.

## Never
- "This commit", "I", "we", "now", "currently". The diff already shows what changed.
- Emoji.
- Any AI attribution: no `Co-authored-by` trailer for a bot or AI, no model or tool name, no session link. The no-ai-mentions hook blocks these.

## Boundaries
Return the message only, in a code block ready to paste. Never run `git commit`, stage files, or amend.
