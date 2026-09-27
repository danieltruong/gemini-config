---
trigger: glob
globs: "*.ts, *.tsx, *.js, *.jsx, *.vue, *.svelte, *.astro, *.html, *.css, *.scss, vite.config.*"
description: "Front-end changes: the visual acceptance loop in .agents/visual.md and the visual-qa subagent."
---

# Front-end work

- A workspace with `.agents/visual.md` lists one URL per line under `## Pages` and acceptance bullets under `## Accept`.
- After a UI change, open every URL under `## Pages`, take a screenshot, and check each bullet under `## Accept`. Fix what fails and check again. A bullet that still fails after 2 fixes: stop and report it to Daniel.
- After the last change, spawn `visual-qa`. Its clean run on the final tree is part of the review the stop gate needs.
- Accessibility work: load the `a11y-audit` skill.
