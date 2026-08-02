# Release certification

These commands certify an installed marketplace artifact. They are not the
normal user workflow.

Version 1.1.0 packages the repaired AutoResearch evaluation lifecycle and the
paper-informed multi-engine composition. It is released only after the public
deterministic phase certifier passes against the exact installed tag candidate.
The no-call checks below use fake processes only; the recorded live receipt is
the evidence for the bounded composition claim.

## Engine certification boundary

`gepa` and `best_of_n` have narrow installed-plugin probe evidence. The pinned
GEPA commit's focused tests verify AutoResearch's drain barrier,
receipt-derived winner, and feedback ordering. The bounded four-stage live
certifier also passed for installed GEPA, AutoResearch, MetaHarness, and the
fresh exact-winner continuation. This is orchestration evidence, not semantic
quality or generalization evidence.

## Prepare the installed artifact

Create a fresh virtual environment, `HOME`, `CODEX_HOME`, and adapter state.
Install the marketplace archive under test. Set `GEPA_CODEX_SKILL_DIR` to that
installed skill. The runners reject the source checkout, the repository
marketplace copy, reused state, a missing manifest, and a version mismatch.

Install Codex CLI 0.146.0 beneath the fresh home, then stage and authenticate
the isolated runtime:

```bash
python "$GEPA_CODEX_SKILL_DIR/scripts/sandbox_runtime.py" stage
python "$GEPA_CODEX_SKILL_DIR/scripts/sandbox_runtime.py" login
python "$GEPA_CODEX_SKILL_DIR/scripts/sandbox_runtime.py" probe
```

Leave `CODEX_API_KEY` and `OPENAI_API_KEY` unset for staged-login proof. Set
`CODEX_ADAPTER_PRE_SUBMISSION_RETRIES=0`. Use a new output directory and
adapter state for every run.

## No-call checks

```bash
python "$GEPA_CODEX_SKILL_DIR/scripts/preflight.py" --engine autoresearch
python "$GEPA_CODEX_SKILL_DIR/scripts/preflight.py" --engine meta_harness
python -m pytest -q -m "not live"
python -m ruff check .
```

## Live certification

Every command below can make a model call. Obtain fresh authorization first.
Run them serially with zero retries.

The paper-informed optimize-everything certifier is the closing release gate:

```bash
RUN_CODEX_LIVE=1 python scripts/release_phase_certifier.py \
  --output-dir /tmp/paper-informed-phase-certification
```

It runs equal `max_evals=10` Phase 1 slices for GEPA, AutoResearch, and
MetaHarness concurrently, selects the best shared deterministic score, and
seeds a fresh AutoResearch process with the exact winner bytes. Its ceiling is
four Luna/high optimizer calls, zero retries, and no judge calls. Equal
evaluation counts are not a dollar-matched reproduction of the published
experiment.

Prove the installed compatibility adapter independently before exercising any
optimizer or evaluator:

```bash
RUN_CODEX_LIVE=1 python scripts/release_adapter_smoke.py \
  --expected-commit "$(git rev-parse HEAD)" \
  --output-dir /tmp/installed-adapter-smoke
```

This command makes one Luna call through the installed `claude` compatibility
launcher. It excludes GEPA, `optimize_anything`, evaluators, judges, and the
research-bullet harness. Do not continue unless its receipt proves exact
installed-file provenance, terminal success, positive usage, and one session
mapping.

```bash
RUN_CODEX_LIVE=1 python scripts/installed_skill_dogfood.py \
  --output-dir /tmp/installed-skill-dogfood

RUN_CODEX_LIVE=1 python scripts/release_inprocess_smoke.py \
  --engine gepa --output-dir /tmp/gepa-smoke
RUN_CODEX_LIVE=1 python scripts/release_inprocess_smoke.py \
  --engine best_of_n --output-dir /tmp/best-of-n-smoke
```

## Individual agentic runtime exercises

These commands exercise one agentic runtime at a time. They do not certify the
paper-informed composition.

```bash
RUN_CODEX_LIVE=1 python scripts/release_dogfood.py --engine autoresearch
RUN_CODEX_LIVE=1 python scripts/release_dogfood.py --engine meta_harness
```

The direct adapter smoke and the opt-in release tests remain available:

```bash
RUN_CODEX_LIVE=1 python -m pytest -q \
  tests/test_adapter.py::test_live_codex_round_trip
RUN_CODEX_LIVE=1 python -m pytest -q tests/test_live_optimize_anything.py
```

Stop without retrying if a process has an ambiguous terminal state, missing
usage, missing session mapping, a failed score, or a receipt mismatch.

## Receipt review

Receipts use schema 2. Before starting the next live command, confirm:

- installed-plugin provenance and the expected plugin, repository, and GEPA
  commits;
- staged ChatGPT login with both API-key variables absent;
- the declared engine, model, reasoning effort, evaluation limits, and
  zero-retry policy;
- a completed terminal state, positive usage, and the adapter's labeled cost
  estimate;
- invocation and session mappings that reconcile to the named evidence files;
- SHA-256 hashes for every named runtime, fixture, journal, and session file.

Receipts contain no credentials, prompts, responses, or candidate text. The
public copy replaces paths beneath the operator's home directory with `$HOME`
and stores thread and session identifiers as SHA-256 values. The private
invocation journal retains the original mapping for reconciliation. The cost
field is a token-derived estimate. It is not provider billing or an exact USD
cap.

Certify the untagged merge commit from a fresh installed archive. Create the
annotated tag only after all receipts pass review. Reinstall that exact tag and
repeat the no-call installation and provenance checks. A failed certification
gets a fix-forward commit, not a reused or moved tag.
