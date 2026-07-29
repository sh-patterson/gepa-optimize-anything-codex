# Hard-budget status

Status: externally blocked as of 2026-07-28.

GEPA's `--max-budget-usd` contract requires two provider guarantees:

1. A USD ceiling enforced for one Codex invocation.
2. A provider-originated cost receipt for that invocation.

Codex CLI `0.144.6` provides neither guarantee. `codex exec` has no USD-budget
option. Its JSONL completion event reports token usage, not billed cost. The
Responses API has token limits and usage fields but no per-request USD ceiling
or billed-cost field.

OpenAI organization and project spend limits are cumulative controls. They do
not bind one invocation. The organization Cost API requires an admin key and
returns costs in daily buckets, so it is not a terminal per-invocation receipt.

The adapter therefore continues to reject `--max-budget-usd` before spawning
Codex. A release runner may apply a project hard limit from durable receipts as
an operational backstop before it starts a later invocation. That control can
lag a submitted call and its accounting, so it does not satisfy GEPA's
per-invocation hard-budget contract. A paid termination probe would still be
unbounded by the requested contract and must not run. Revisit this decision only
when Codex exposes both guarantees.

Evidence:

- [Codex CLI command reference](https://learn.chatgpt.com/docs/developer-commands.md?surface=cli#cli-codex-exec)
- [Codex non-interactive JSONL output](https://learn.chatgpt.com/docs/non-interactive-mode.md)
- [Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Usage and Cost API example](https://developers.openai.com/cookbook/examples/completions_usage_api)
- `tests/test_adapter.py::test_max_budget_is_rejected_before_codex_spawn`
