---
trigger: model_decision
description: "Apply when delegating subtasks to DeepSeek Flash or GLM Flash via OpenRouter, querying DeepSeek or GLM, or using the openrouter MCP tools."
---

# OpenRouter Delegation

Dual-model strategy via OpenRouter:

- **Code Generation & Execution**: `deepseek/deepseek-v4.1-flash` ($0.14/M prompt, $0.28/M completion)
  - Use for: code generation, refactoring, test authoring, filesystem navigation, terminal-heavy tasks.
  - Strengths: High SWE benchmark scores (74.2 DeepSWE, 90.6 Terminal-Bench), high output throughput, low completion cost.
  - Query: Call `deepseek_chat` MCP tool for fast text or code analysis.
  - Subtasks: Call `deepseek_subtask` MCP tool for autonomous file edits and commands.
  - Standalone: Run `pwsh ~/scripts/agy-deepseek.ps1 -p "<prompt>"` or `bash ~/scripts/agy-deepseek.sh`.

- **Critique, Audit & Planning**: `z-ai/glm-5.3-flash` ($0.15/M prompt, $0.50/M completion)
  - Use for: independent code review, security passes, invariant auditing, failure root-cause diagnosis, task planning.
  - Strengths: Nuanced instruction following, multi-step causal deduction, avoids code generator blind spots.
  - Query: Call `glm_chat` MCP tool for critique, auditing, or planning queries.
  - Subtasks: Call `glm_subtask` MCP tool for autonomous review and verification subtasks.
  - Standalone: Run `pwsh ~/scripts/agy-glm.ps1 -p "<prompt>"` or `bash ~/scripts/agy-glm.sh`.

## Security
- Never log or hard-code API tokens; read `OPENROUTER_API_KEY` from the environment.
