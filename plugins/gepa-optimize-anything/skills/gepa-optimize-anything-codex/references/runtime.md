# Codex agentic runtime

GEPA currently starts its agentic backends through an internal command named
`claude`. The staged launcher implements that narrow process contract with
`codex exec`. It is an implementation detail, not a user-facing model API.

## Stable installation

`sandbox_runtime.py stage` copies the launcher and adapter into
`~/.local/share/gepa-optimize-anything-codex/bin` and creates the isolated
Codex home at `~/.cache/gepa-optimize-anything-codex/codex`. Stage is
idempotent and creates no per-run state.

`sandbox_runtime.py login` authenticates that isolated Codex home through
ChatGPT. `CODEX_API_KEY` is the alternative. `OPENAI_API_KEY` remains reserved
for GEPA's in-process model interface.

## Per-run state

Give every agentic run a unique `CODEX_ADAPTER_STATE_DIR` beneath
`~/.cache/gepa-optimize-anything-codex/runs`. The adapter writes metadata-only
invocation records and session mappings there. It does not copy authentication
into run state.

`sandbox_runtime.py probe` creates temporary run state beneath the same root,
enters GEPA's Bubblewrap jail, verifies the staged launcher, imports the
adapter, checks Codex, confirms authentication, and proves both cache
directories writable. The temporary state disappears after the probe.

## Limits

The supported agentic path uses Linux and `sandbox=True`. Preflight validates
Bubblewrap, Codex, authentication, cache paths, adapter limits, and the probe.
`--no-sandbox` is an explicit opt-out.

The adapter rejects agentic `max_token_cost` before starting Codex. Codex does
not provide the provider-enforced per-invocation USD ceiling required by that
flag. Bound work with evaluations, engine iterations, adapter starts, and
`stop_at_score`. Journaled usage supports a labeled token-derived estimate
only.

## Engine evidence boundary

`gepa` and `best_of_n` have narrow probe evidence through the installed
`CodexLM` callable. AutoResearch is **NOT VERIFIED** for a yielded or
long-running evaluator. The adapter's completed JSONL receipt proves the outer
Codex process completed. It cannot prove that GEPA's separate evaluation server
has drained or that feedback reached the next AutoResearch proposal.

MetaHarness has separate evidence requirements. Treat it as unverified until a
bounded probe reconciles its proposal, evaluator, and winner receipts.
