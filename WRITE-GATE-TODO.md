# Write gate: what is left

## Status

Stopped at the owner's request with the gate itself finished and green. Rules 1-7 of the brief:

| Rule | State |
|---|---|
| 1 `~/.gemini/` and this repo's `hooks/`, `hooks.json`, `scripts/` | done, tested through a junction, an MSYS path and a shouted path; artifact directory allowed |
| 2 `.agents/verify.*`, `.agents/visual.md`, `.agents/rules/**` | done, tested the same three ways |
| 3 test files, `tester` subagent only | done, tested for the main agent, a `coder` subagent, a `tester` subagent and a subagent with no record |
| 4 shell tripwire on protected literals | done, tested on five writes and five reads that must pass |
| 5 protected-file hash between baseline and Stop | done for the workspace verifier, tested; the hash covers `hooks/*.py` and `hooks.json` too, but no test moves those, since a test that rewrites the gate's own source would be the thing the gate exists to stop |
| 6 crash or timeout means allow | done: an unreadable payload and a payload the gate chokes on both allow and log one line; `hooks.json` gives the gate 10s |
| 7 paths through `hookpaths.real_path` plus `os.path.realpath` | done, in `hookpaths.resolved` and `hookpaths.inside` |

Not started, and not part of the brief: nothing.

## PreToolUse schema verified

- `F:\Factory\renpy\docs\antigravity\hooks.md:244-258` names the input fields (`toolCall.name`, `toolCall.args`, `stepIdx`, common fields) and the output fields: `decision` required, one of `allow`, `deny`, `ask`, `force_ask`, `deny_unless_prior_grant`; `reason` optional; `permissionOverrides` optional.
- `~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/hooks.md:162-215` is the newer copy. Same contract, plus `overwrite`, which this gate does not use. The gate answers `{"decision": "deny", "reason": ...}` or `{"decision": "allow"}`, which both sources accept.

## Open questions for Daniel

- `AGY_WRITE_GATE_OFF=1` is the documented escape hatch. An agent can set it in its own `run_command`, so it is a speed bump, not a lock. A real lock would need the deny to live in `permissions.deny` in `settings.json`, which the agent also writes. Worth deciding which one you want to be the real wall.
- The tripwire's read-only allowlist is hand-written in `hooks/write_gate.py` (`READ_ONLY`). It overlaps `permissions.allow` in `settings.json` by hand rather than reading it. If that list grows, reading the settings file is the smaller change.
- `/hooks` print mode was not exercised against this worktree: that needs the installer, which was out of scope for this branch.
