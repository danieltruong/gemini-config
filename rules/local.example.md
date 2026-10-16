---
trigger: manual
description: "Template for rules/local.md. Not loaded: copy it to rules/local.md, set trigger to always_on, and fill in this machine's values."
---

# Machine facts

Every other rule stays portable. A path that differs per machine lives only in `rules/local.md`, which git ignores.

- Ren'Py SDK: `<sdk>`, for example `F:/Factory/renpy`. Every project is a subdirectory, for example `<sdk>/Birth Battle`.
- REA: the `rea` MCP server runs on `rea-lxc` (192.168.3.45). Copy a target to its share, for example `\\192.168.3.45\re` mapped as `Q:\`, and pass it as `/srv/re/<file>`.
