# GEPA Optimize Anything for Codex

This marketplace packages GEPA's `optimize_anything` skill for Codex. GEPA
keeps ownership of candidates, evaluators, engines, and result objects. The
plugin adds a Codex-native runtime shared by all four engines. The stable
`openai-codex` SDK/App Server path is primary; direct Codex CLI JSONL is a
pre-start fallback behind the same interface.

## Install

```bash
codex plugin marketplace add sh-patterson/gepa-optimize-anything-codex
codex plugin add gepa-optimize-anything@gepa-optimize-anything-codex
```

Start a new Codex task after installation. Ask for the installed skill in plain
language:

```text
Use $gepa-optimize-anything:gepa-optimize-anything-codex to improve
support-policy.json against tests/route_cases.json with MetaHarness.
```

The skill helps define the evaluator, choose an engine, set bounded work, run
preflight, and inspect the result.

## Supported engines

| Engine | Execution | Model interface | Evidence |
|---|---|---|---|
| `gepa` | In process | Native `CodexLM` | Deterministic contract + no-model App Server probe |
| `best_of_n` | In process | Native `CodexLM` | Deterministic contract + no-model App Server probe |
| `autoresearch` | Workspace agent | Native `AgentRunner`, persistent Codex thread | Deterministic continuation/lifecycle contract |
| `meta_harness` | Workspace agent | Native `AgentRunner`, isolated Codex threads | Deterministic isolation contract |

`gepa` and `best_of_n` have narrow probe receipts. The pinned GEPA commit's
AutoResearch tests verify its evaluation-session drain barrier, receipt-derived
winner, and feedback ordering. A historical phase-certification receipt also ran
GEPA, AutoResearch, and MetaHarness against one shared evaluator, selected the
best comparable score, and seeded a fresh AutoResearch continuation with the
exact winner bytes. That external evidence is not a current certification of
this checkout, and it never claimed semantic quality, generalization, or a
dollar-matched reproduction of the published Omni experiment.

The installed `scripts/codex_lm.py` callable supplies Codex to the two
in-process engines. `scripts/codex_agent_runner.py` supplies the same runtime
to the two workspace engines. Neither adds a new optimizer.

Public deterministic phase certification passed historically for the full three-engine
composition, but its row is only a historical audit pointer, not a receipt stored
in this checkout. Treat the phase claim as unverified until a fresh run produces
a sanitized receipt under `release/receipts/` for the exact installed artifact
and its hash is recorded in the audit log.

## Requirements

Install the repository's `live` extra. It pins the maintained GEPA fork and
`openai-codex==0.144.4`. The native runtime reuses Codex-managed ChatGPT
authentication; it never copies credentials into plugin evidence.

Run the shipped no-model readiness probe before optimization:

```bash
python "$SKILL_DIR/scripts/native_preflight.py" \
  --evidence-dir "$RUN_DIR/runtime-evidence" \
  --codex-home "$HOME/.codex"
```

The Codex Desktop task must be allowed to access its existing Codex home so App
Server can read authentication and write its own state. If App Server is
unavailable before the run starts, the bundled direct CLI backend may be
selected and the fallback reason is recorded.

## Limits

Do not set `max_token_cost` for `autoresearch` or `meta_harness`. Codex Desktop
reports token usage, not a provider USD receipt, so the native runtime records
`cost_status="unknown"` and rejects dollar-budget claims.

Callers must explicitly set `max_evals=10` for agentic engines and, for
MetaHarness, `max_iterations=3` with `max_candidates_per_iter=3`. Set an agent
timeout and `stop_at_score` where the evaluator has a known ceiling.
Ambiguous, usage-bearing, and completed calls are never retried. AutoResearch
continuation stops when an iteration makes no evaluation progress.

## Results

The winning artifact and score are available on GEPA's result object as
`best_candidate` and `best_score`. GEPA writes engine work to `run_dir` and
evaluation records plus summaries to `output_dir`.

Native evidence contains metadata-only claims and terminal records under the
chosen evidence directory. It stores no credentials and labels provider cost
unknown when Codex does not report billing.

Release certification commands and receipt checks live in
[`release/README.md`](release/README.md). The full optimizer API, evaluator
guidance, data modes, tracking, composition helpers, and budget semantics live
in the installed skill and its references. The exact upstream GEPA pin is
recorded in [`UPSTREAM.md`](UPSTREAM.md).
