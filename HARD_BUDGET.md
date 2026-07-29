# Agentic spend-control contract

Status: bounded-work policy adopted for v1.

GEPA's `--max-budget-usd` contract requires two provider guarantees:

1. A USD ceiling enforced for one Codex invocation.
2. A provider-originated cost receipt for that invocation.

Codex CLI `0.146.0` provides neither guarantee. `codex exec` has no USD-budget
option. Its JSONL completion event reports token usage, not billed cost. The
Responses API has token limits and usage fields but no per-request USD ceiling
or billed-cost field.

OpenAI organization and project spend limits are cumulative controls. They do
not bind one invocation. The organization Cost API requires an admin key and
returns costs in daily buckets, so it is not a terminal per-invocation receipt.

The adapter therefore rejects `--max-budget-usd` before spawning Codex. Project
limits remain an optional operational backstop, but their delayed accounting
does not satisfy GEPA's per-invocation hard-budget contract.

## Supported v1 policy

The Codex agentic engines promise bounded work, not a provider-enforced dollar
ceiling. Callers must set evaluation and proposer bounds. The adapter enforces
an atomic invocation-start cap and retries only a failure known to have occurred
before Codex started. It never retries an ambiguous, usage-bearing, or completed
call. A host timeout is an optional emergency stop rather than a default budget.

The release dogfood is stricter: one proposer iteration, three evaluations, one
adapter invocation, zero retries, and serial engines. Its token-derived cost is
evidence and reconciliation metadata, not a billing receipt.

Evidence:

- [Codex CLI command reference](https://learn.chatgpt.com/docs/developer-commands.md?surface=cli#cli-codex-exec)
- [Codex non-interactive JSONL output](https://learn.chatgpt.com/docs/non-interactive-mode.md)
- [Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Usage and Cost API example](https://developers.openai.com/cookbook/examples/completions_usage_api)
- `tests/test_adapter.py::test_max_budget_is_rejected_before_codex_spawn`
