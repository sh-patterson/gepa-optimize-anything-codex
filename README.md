# GEPA Optimize Anything for Codex

This marketplace packages GEPA's `optimize_anything` skill for Codex. GEPA
keeps ownership of candidates, evaluators, engines, and result objects. The
plugin adds the Linux runtime needed for its two agentic engines to use Codex.

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

| Engine | Execution | Model interface | Platform |
|---|---|---|---|
| `gepa` | In process | GEPA's upstream LM interface | Any GEPA-supported host |
| `best_of_n` | In process | GEPA's upstream LM interface | Any GEPA-supported host |
| `autoresearch` | Codex subprocess | Installed Codex adapter | Linux with Bubblewrap |
| `meta_harness` | Codex subprocess | Installed Codex adapter | Linux with Bubblewrap |

The public Codex adapter supports `autoresearch` and `meta_harness`. It does not
replace GEPA's in-process LM interface or add a new optimizer.

## Requirements

Install the pinned GEPA dependency with the repository's `live` extra. The
in-process engines also need the credentials required by their configured
provider model.

Agentic runs need Linux, Bubblewrap, `jq`, and Codex CLI 0.146.0 installed at a
Bubblewrap-visible path:

```bash
npm install --prefix "$HOME/.local" @openai/codex@0.146.0
export PATH="$HOME/.local/node_modules/.bin:$PATH"
python "$SKILL_DIR/scripts/sandbox_runtime.py" stage
```

Use `sandbox_runtime.py login` once for an isolated ChatGPT login, or set
`CODEX_API_KEY`. Set `CODEX_HOME` to
`~/.cache/gepa-optimize-anything-codex/codex` and give each run a unique
`CODEX_ADAPTER_STATE_DIR` beneath
`~/.cache/gepa-optimize-anything-codex/runs`. Run
`preflight.py --engine <engine>` before starting an agentic optimizer.

## Limits

Do not set `max_token_cost` for `autoresearch` or `meta_harness`. The adapter
rejects it before Codex starts because Codex cannot enforce GEPA's exact USD
contract. Journaled token usage produces an estimate, not a provider billing
receipt.

Callers must explicitly set `max_evals=10` for agentic engines and, for
MetaHarness, `max_iterations=3` with `max_candidates_per_iter=3`. The adapter
alone enforces a default of four atomic starts per state directory. It retries
once only when Codex is known not to have started.
Ambiguous, usage-bearing, and completed calls are never retried.

`sandbox=True` is the supported Linux path. `--no-sandbox` remains an explicit
preflight opt-out. macOS agentic execution is not supported.

## Results

The winning artifact and score are available on GEPA's result object as
`best_candidate` and `best_score`. GEPA writes engine work to `run_dir` and
evaluation records plus summaries to `output_dir`.

Agentic state contains metadata-only invocation records under
`CODEX_ADAPTER_STATE_DIR/invocations/` and session mappings under
`CODEX_ADAPTER_STATE_DIR/sessions/`. It stores no prompts, responses, or
credentials.

Release certification commands and receipt checks live in
[`release/README.md`](release/README.md). The full optimizer API, evaluator
guidance, data modes, tracking, composition helpers, and budget semantics live
in the installed skill and its references. The exact upstream GEPA pin is
recorded in [`UPSTREAM.md`](UPSTREAM.md).
