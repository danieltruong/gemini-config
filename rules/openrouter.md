---
trigger: model_decision
description: "Apply when delegating subtasks to DeepSeek Flash, GLM Flash or GLM 5.3 via OpenRouter, querying DeepSeek or GLM, or using the openrouter MCP tools."
---

# OpenRouter Delegation

Dual-model strategy via OpenRouter prioritizing quality over speed:

- **Creative Writing & Script Authoring**: `deepseek/deepseek-v4.1-flash` ($0.30/M prompt, $1.20/M completion)
  - **Mandate**: All creative writing and script authoring (character dialogue, narrative scenes, game scenarios, story text, branching visual novel logic) must route to DeepSeek.
  - **Reasoning Effort**: `high` for branching scenario logic, flag state management, and multi-character continuity.
  - **Voice Tuning**: Enforce character voice and cadence via few-shot exemplars and explicit style constraints rather than relying purely on reasoning tokens, preventing clinical or stiff dialogue.
  - **Query**: Call `deepseek_chat` MCP tool.
  - **Subtasks**: Call `deepseek_subtask` MCP tool.

- **Code Generation & Execution**: `deepseek/deepseek-v4.1-flash`
  - Use for: implementation from spec, refactoring, test suite authoring, terminal-heavy tasks.
  - **Reasoning Effort**: `high` for standard features, algorithms, and comprehensive test fixtures.
  - **Escalation**: Frontier algorithms or complex state engines where flash hits capability ceiling, rerun `deepseek/deepseek-v4.1-flash` at `max`. Still stuck: full model, `glm_subtask(model="z-ai/glm-5.3", effort="max")`. Its $0.07/$7.00 list price unreliable (endpoints charge $1.32 to $8.80/M completion), so keep provider pin below.
  - **Query**: Call `deepseek_chat` MCP tool.
  - **Subtasks**: Call `deepseek_subtask` MCP tool.
  - **Standalone**: Run `pwsh ~/scripts/openrouter/agy-deepseek.ps1 -p "<prompt>"` or `bash ~/scripts/openrouter/agy-deepseek.sh`.

- **Critique, Audit, Planning & Verification**: `z-ai/glm-5.3-flash` ($0.15/M prompt, $0.50/M completion)
  - Use for: independent pre-commit diff review, security auditing, invariant verification, task planning, failure root-cause diagnosis.
  - **Cross-Model Review Rule**: Generative code written by DeepSeek is independently audited by GLM to eliminate self-review bias.
  - **Reasoning Effort**: `max` for every review, audit, plan check and diagnosis, high-risk diffs (auth, parser, concurrency) included. Reasoning cannot be turned off on this model.
  - **Query**: Call `glm_chat` MCP tool.
  - **Subtasks**: Call `glm_subtask` MCP tool.
  - **Standalone**: Run `pwsh ~/scripts/openrouter/agy-glm.ps1 -p "<prompt>"` or `bash ~/scripts/openrouter/agy-glm.sh`.

## Reasoning Effort Parameters
- OpenRouter unified schema: pass `reasoning: {"effort": "<level>"}` in API payload.
- Supported levels on both families: `low`, `high`, `max`.
- DeepSeek: default `high`; `max` only for escalation (on review scored 72.0 vs 70.5 at twice cost: Macroscope AI code review benchmark, 2026-09-22, https://macroscope.com/content/ai-code-review-benchmark-best-models).
- GLM: default `max`, OpenRouter's own default for GLM 5.3; `high` cheaper fallback, under A/B test.
- Every request pins providers: `provider: {"quantizations": ["fp8", "bf16", "fp16"], "require_parameters": true}`. Never allow `unknown` for DeepSeek or GLM: unlabelled endpoints cheapest, win load balancing. Only closed-weight vendors (`anthropic/`, `openai/`, `google/`, `x-ai/`) add it, since first-party endpoints labelled `unknown`.

## Security
- Never log or hard-code API tokens; read `OPENROUTER_API_KEY` from the environment.
