---
trigger: model_decision
description: "Apply to any Ren'Py project or .rpy work, even before a .rpy file is open: lint and test commands, renpy MCP tools, save compatibility."
---

# Ren'Py

- `<sdk>` is the Ren'Py SDK path in `rules/local.md`; each project is a subdirectory of it. No `rules/local.md`, or no SDK path in it: ask Daniel for the path before running anything.
- Verify a change with `<sdk>/renpy.exe "<project>" lint --error-code`, then `<sdk>/renpy.exe "<project>" test` for the Ren'Py 8.5 testcase framework. Never call `renpy.sh` on Windows: it is a Linux script and always fails.
- Answer questions about `.rpy` files with the `renpy` MCP tools, not grep: `renpy_run_lint`, `renpy_static_check`, `renpy_check_assets`, `renpy_find_symbol`, `renpy_get_call_graph`, `renpy_dump_symbols`.
- The stop hook blocks finishing while a file you changed still has a lint error. Fix the reported line, re-run lint, then finish.
- Keep saves loadable: add new state with `define` or `default`, never rename or drop state that has shipped, and keep the `from` clause on every existing `call`.
