---
trigger: model_decision
description: "Apply when delegating subtasks to DeepSeek Flash, GLM Flash or GLM 5.3 via OpenRouter, querying DeepSeek or GLM, or using the openrouter MCP tools."
---

# OpenRouter Delegation

Model split via OpenRouter, quality over speed. Roles set by blind-judged A/B, 2026-10-07 (n=5 diffs; glm-5.3 high vs max n=2).

- **Code Generation & Execution**: `deepseek/deepseek-v4.1-flash` ($0.30/M prompt, $1.20/M completion)
  - Use for: implementation from spec, refactoring, test suite authoring, terminal-heavy tasks.
  - **Reasoning Effort**: `high` for standard features, algorithms, test fixtures.
  - **Escalation**: Frontier algorithms or complex state engines where flash hits capability ceiling, rerun `deepseek/deepseek-v4.1-flash` at `max`. Still stuck: full model, `glm_subtask(model="z-ai/glm-5.3", effort="max")`. Its $0.07/$7.00 list price unreliable (endpoints charge $1.32 to $8.80/M completion), so keep provider pin below.
  - **Query**: Call `deepseek_chat` MCP tool.
  - **Subtasks**: Call `deepseek_subtask` MCP tool.
  - **Standalone**: Run `pwsh ~/scripts/openrouter/agy-deepseek.ps1 -p "<prompt>"` or `bash ~/scripts/openrouter/agy-deepseek.sh`.

- **Critique, Audit, Planning & Root-Cause**: `deepseek/deepseek-v4.1-flash` at `high`
  - Use for: independent pre-commit diff review, invariant verification, task planning, failure root-cause diagnosis.
  - **Review Rule**: Review of V4.1 Flash code runs as a separate V4.1 Flash high call; the A/B showed no gain from same-tier cross-family review (GLM Flash returned nothing on 4 of 5 diffs). High-risk diffs go to glm-5.3 max.
  - **Escalation**: Same as Code: rerun V4.1 Flash at `max`, then `glm_subtask(model="z-ai/glm-5.3", effort="max")`.
  - **Query**: Call `deepseek_chat` MCP tool.
  - **Subtasks**: Call `deepseek_subtask` MCP tool.

- **Security Audit, Plan Verification & High-Risk Diffs** (auth, parser, concurrency): `z-ai/glm-5.3` at `max`
  - **Call**: `glm_subtask(model="z-ai/glm-5.3", effort="max")`, or `glm_chat` with same arguments. Never `high`: scored lower in A/B and missed a major.

- **Creative Writing, Script Authoring & Vision**: `z-ai/glm-5.3-flash` at `max` ($0.15/M prompt, $0.50/M completion)
  - **Mandate**: All creative writing and script authoring (character dialogue, narrative scenes, game scenarios, story text, branching visual novel logic) must route to GLM Flash.
  - **Vision**: Vision goes through `glm_subtask` with the image file in cwd; the chat tools take text only. Untested.
  - **Reasoning Effort**: Reasoning cannot be turned off on this model.
  - **Voice Tuning**: Enforce character voice and cadence via few-shot exemplars and explicit style constraints rather than relying purely on reasoning tokens, preventing clinical or stiff dialogue.
  - **Query**: Call `glm_chat` MCP tool.
  - **Subtasks**: Call `glm_subtask` MCP tool.
  - **Standalone**: Run `pwsh ~/scripts/openrouter/agy-glm.ps1 -p "<prompt>"` or `bash ~/scripts/openrouter/agy-glm.sh`.
  - **Open failure (2026-10-07)**: on long outputs it can return reasoning only, no text (4 of 5 long reviews); check reply non-empty.

`glm_chat` and `glm_subtask` default to `z-ai/glm-5.3-flash`; pass `model="z-ai/glm-5.3"` for security and plan verification.

## Reasoning Effort Parameters
- OpenRouter unified schema: pass `reasoning: {"effort": "<level>"}` in API payload.
- Supported levels on both families: `low`, `high`, `max`.
- DeepSeek: default `high`; `max` only for escalation (on review scored 72.0 vs 70.5 at twice cost: Macroscope AI code review benchmark, 2026-09-22, https://macroscope.com/content/ai-code-review-benchmark-best-models).
- GLM: default `max`, OpenRouter's own default for GLM 5.3; `high` scored lower in 2026-10-07 A/B (3.0 vs 4.0 judge on two diffs), so stays off.
- Every request pins providers: `provider: {"quantizations": ["fp8", "bf16", "fp16"], "require_parameters": true}`. Never allow `unknown` for DeepSeek or GLM: unlabelled endpoints cheapest, win load balancing. Only closed-weight vendors (`anthropic/`, `openai/`, `google/`, `x-ai/`) add it, since first-party endpoints labelled `unknown`.

- Launcher killed before cleanup leaves `modelProvider` patched in agy settings: run `python ~/scripts/openrouter/settings_lease.py recover`.

## Security
- Never log or hard-code API tokens; read `OPENROUTER_API_KEY` from the environment.
