---
name: gepa-optimize-anything-codex
description: >-
  Run GEPA Optimize Anything through Codex. Improve prompts, code, configs, specs,
  schemas, SQL, and other scored text artifacts with GEPA, AutoResearch,
  MetaHarness, or best-of-N. Use when Codex needs to build an evaluator and
  proposer loop, compare optimization engines, tune a candidate against an
  objective metric or LLM judge, or run GEPA's agentic engines through Codex.
---
# `optimize_anything`

This is a Codex port of GEPA's upstream skill. The installed runtime adapts
GEPA's agentic process boundary to Codex. See `references/runtime.md` for that
internal contract.

For every `autoresearch` or `meta_harness` run:

1. Resolve `SKILL_DIR` to the absolute directory containing this `SKILL.md`. Use the installed skill path supplied by Codex. Do not ask the user to find the plugin cache.
2. Install pinned GEPA if the active Python environment does not already provide it.
3. Run `npm install --prefix "$HOME/.local" @openai/codex@0.146.0` and prepend `"$HOME/.local/node_modules/.bin"` to `PATH`.
4. Stage the adapter with `RUNTIME_BIN="$(python "$SKILL_DIR/scripts/sandbox_runtime.py" stage)"`.
5. Unless `CODEX_API_KEY` is set, run `python "$SKILL_DIR/scripts/sandbox_runtime.py" login` once to authenticate the isolated runtime through ChatGPT.
6. Prepend `RUNTIME_BIN` to `PATH`. Set `CODEX_HOME="$HOME/.cache/gepa-optimize-anything-codex/codex"` and a unique `CODEX_ADAPTER_STATE_DIR` beneath `~/.cache/gepa-optimize-anything-codex/runs`.
7. Run `"$SKILL_DIR/scripts/preflight.py" --engine <engine>`.
8. Launch the user's Python program with `sandbox=True`.

## Codex adapter limits

The agentic adapter supports Linux only. Keep GEPA's default `sandbox=True`. Staging puts the adapter under Bubblewrap's read-only `~/.local` bind. Keep `CODEX_HOME` and adapter state under its writable `~/.cache` bind. Authenticate that isolated home with `sandbox_runtime.py login`, or use `CODEX_API_KEY`. The jail does not expose the normal `~/.codex` login directory. An explicit `--no-sandbox` preflight is the opt-out path for hosts that intentionally use the normal Codex login.

The adapter maps the pinned GEPA default `claude-sonnet-4-6` to `gpt-5.6-luna`. It rejects other source models before starting Codex. Sandboxed runs require either the staged ChatGPT login or `CODEX_API_KEY`; the adapter preserves the existing `CODEX_HOME` and never copies authentication into its session-state directory. Unsandboxed runs may use an existing normal `codex login`. `OPENAI_API_KEY` remains available to GEPA's in-process models but is never translated into Codex authentication.

Do not set `max_token_cost` with `autoresearch` or `meta_harness`. GEPA passes it as `--max-budget-usd`; the adapter rejects that flag before spawning Codex because it cannot enforce a USD cap. Callers must explicitly set `max_evals=10` and, for `meta_harness`, `max_iterations=3` plus `max_candidates_per_iter=3`. The adapter alone enforces a default of four atomic starts per unique state directory. It retries once only when Codex is known not to have started, and that retry consumes an invocation slot. It never retries an ambiguous, usage-bearing, or completed call. Set `CODEX_ADAPTER_MAX_INVOCATIONS` to a different positive integer to override the atomic start cap; a claimed slot remains consumed after failure. Set `stop_at_score` whenever the metric has a known ceiling. A host timeout is an optional emergency stop, not a default budget. `total_cost_usd` is a conservative standard-tier estimate from observed tokens, not provider billing. The adapter accepts pinned GEPA's exact web-tool denial, disables Codex's standalone web search, and rejects unknown policy values plus `--settings` before Codex starts. Shell network access remains available, matching GEPA's unsandboxed agent path.

**Naming, precisely.** `optimize_anything` is the tool: a general API for optimizing text
artifacts. **GEPA** is one specific optimizer behind it — reflective evolutionary search, the
default backend (`engine="gepa"`) — and, for legacy reasons, also the name of the Python package
that ships all of this. In this skill, "the gepa backend" always means the optimizer; statements
about "the optimizer" or "the backend" apply to whichever engine you chose.

`optimize_anything` does **black-box optimization**: you provide (1) a seed artifact, (2) an
**evaluator** that scores any artifact and returns feedback, and (3) a backend, which repeatedly
proposes improved artifacts and scores them through your evaluator. "Black-box" refers to the
**evaluator**, not the artifact: the backend never sees how the score is computed — no gradients,
no metric internals — only the scalar score and the feedback text you emit. The candidate itself
*is* visible: the proposer reads and rewrites it, applying the LLM's understanding of your artifact.
The framework just imposes no structure on it — any string an evaluator can score works. The
leverage is in your score and your feedback.

**You write the task and evaluator once, then choose the search algorithm with one `engine`
argument** — and the same code runs under any of them:
- **`gepa`** — the GEPA optimizer: reflective evolutionary search, in-process (an LLM reflects on
  feedback and mutates candidates; keeps a Pareto frontier). The default; strongest when feedback
  is rich.
- **`autoresearch`** — an agentic optimizer: one Codex subprocess iterates like a researcher in
  a work dir, scoring candidates through an HTTP eval server.
- **`meta_harness`** — an agentic proposer (Codex subprocess) that reads the frontier/history each
  iteration and writes new candidates for the engine to benchmark.

(There is also a `best_of_n` engine — sample N independent candidates, keep the best. It is
deliberately naive: use it as a **baseline** to compare an optimizer against, not as the optimizer.)

This plugin translates the Linux-only `autoresearch` and `meta_harness`
subprocess boundary to Codex. `scripts/codex_lm.py` exports `CodexLM`, the
installed callable for the in-process `gepa` and `best_of_n` engines. It pins
`gpt-5.6-luna` with `high` reasoning and requires a fresh state and session
directory for each engine. macOS agentic execution is unsupported.

The `gepa` and `best_of_n` paths have narrow probe evidence. The pinned GEPA
commit's tests verify AutoResearch's evaluation-session drain barrier,
receipt-derived winner, and feedback ordering. The public deterministic phase
certification at commit `3a9ff30` ran installed GEPA, AutoResearch, and
MetaHarness against one evaluator, selected the best shared score, and passed
the exact winner bytes to a fresh AutoResearch continuation. This certifies the
paper-informed composition boundary, not semantic quality, generalization, or
a dollar-matched reproduction of the published Omni experiment.

This makes it easy to start with one backend and benchmark others on the identical task/evaluator.
There are also **composition/pipeline helpers** that combine backends over the same task:
`optimize_sequential` (a pipeline — each stage's best seeds the next), `optimize_parallel`,
`optimize_best_of`, `optimize_vote` (re-score each branch's best for a fair pick), and an adaptive
scheduler that rotates backends on score plateaus — see `references/api.md`.

When the best engine is not known in advance, use the paper-informed multi-engine method in
`references/omni.md`. It turns two observed results into a run contract: no engine dominated the
published comparison, and a fresh optimizer often improved a candidate after the first optimizer
plateaued. Run bounded early branches against one frozen evaluator, select the best candidate,
then seed a fresh continuation from those exact bytes. Keep the published result, the Codex
adaptation, and the evidence required to certify a run separate.

## What can be a candidate
A candidate is **any string your evaluator can score**. "Text in → a number out (higher is better),
plus optional feedback" is the entire contract, which covers a wide range of artifacts:
- **prompts** — system/user prompts, instruction templates, rubrics, few-shot exemplars
- **programs / code** — functions, whole files, CUDA kernels, scored by compiling + running + benchmarking
- **configs / specs / schemas / regex / SQL** — any text whose effect you can measure
- **agent scaffolds** — tool instructions, planner/critic prompts, orchestration text
- **pure search artifacts** — a mathematical construction, a packing layout, a plan, encoded as text

The score can be an **objective metric** (accuracy, pass rate, runtime, cost) **or an LLM-as-judge**
rating for subjective tasks (writing quality, helpfulness, style). At this API the candidate is a
single string (`seed_candidate: str | None`; `None` = seedless — the engine bootstraps from
`objective`/`background`). Multi-component dict candidates exist only in the lower-level
`gepa.gepa_launcher.optimize_anything` API, not here.

## Three optimization modes (choose by how you pass data)
The mode is determined by whether you provide `dataset` and `valset`:
1. **Single-task** (`dataset=None, valset=None`) — solve one hard problem; the candidate *is* the
   solution; the evaluator is called with no example. *E.g. one CUDA kernel, a circle-packing layout.*
2. **Multi-task** (`dataset=<list>, valset=None`) — solve a batch of related problems with one shared
   candidate, transferring insight across them; evaluator called per example. *E.g. a single prompt
   that works across many tasks.*
3. **Generalization** (`dataset=<list>, valset=<list>`) — build a candidate that transfers to
   **unseen** problems; optimize on `dataset`, select on `valset`. *E.g. a prompt tuned to generalize.*
   *Note:* GEPA is the algorithm designed around this mode, and `valset`-based held-out selection is
   currently implemented only by the gepa backend — the other backends fold `valset` into the
   training pool (they can still generalize; there's just no separate selection split).

`test_set` is **separate from the modes and reporting-only**: the seed and the final candidate are
scored on it after optimization for an unbiased number — it never enters the search, selection, or
budget (for the agentic backends it is sealed at the eval server's HTTP layer, so the agent cannot
even see it). See `references/api.md` for details and when to use each mode.

## Install
```bash
pip install "gepa[full] @ git+https://github.com/sh-patterson/gepa.git@3a6f93c5dd0beb68825973b3b2f2cae23060bbbb"
# [full] pulls cloudpickle — needed to pickle closure evaluators for
                           # parallel workers / opt-in evaluation caching; plain `pip install gepa`
                           # can fail there when your evaluator closes over data.
# Proposer LLM: in-process engines use GEPA's upstream LM interface. Configure
# the provider model and credentials required by that interface.
# Agentic backends additionally need the Codex CLI, `jq`, and the staged runtime.
# Resolve SKILL_DIR to the directory containing this installed SKILL.md:
RUNTIME_BIN="$(python "$SKILL_DIR/scripts/sandbox_runtime.py" stage)"
export PATH="$RUNTIME_BIN:$PATH"
export CODEX_HOME="$HOME/.cache/gepa-optimize-anything-codex/codex"
export CODEX_ADAPTER_STATE_DIR="$(mktemp -d "$HOME/.cache/gepa-optimize-anything-codex/runs/run-XXXXXX")"
```

## Mental model (4 pieces)
1. **Candidate** — the text you optimize (a `str`). The seed is the start; `None` = seedless.
2. **`evaluate(candidate, example) -> (score, info)`** — `score` is a float (higher is better);
   `info` is a free-form dict of **feedback the backend's proposer reads** to make better candidates
   (the gepa backend's reflection LM, or the agentic backends' agent; the `best_of_n` baseline
   ignores feedback). When evals batch better than they stream (e.g. a provider batch API), pass
   `batch_evaluator=` instead — all pending `(candidate, example)` pairs in one call; see
   `references/api.md`.
3. **Engine (backend)** — `"gepa"` (default), `"autoresearch"`, `"meta_harness"`, the `"best_of_n"`
   baseline, or a constructed `Engine` instance.
4. **Budget** — `max_evals` (server-side eval-call cap, **default 100**) and, for in-process
   backends, `max_token_cost` (USD cap on proposer-LLM spend). This Codex adapter rejects
   `max_token_cost` for `autoresearch` and `meta_harness`; those agentic paths use the explicit
   bounded values below. For in-process runs, **size `max_evals` for many proposal rounds, not
   one** (see below) — this is the most common way agents misuse this API.

### Codex agentic caller configuration

Callers must explicitly set these recommended values. GEPA does not infer them from this skill:

```python
agentic_config = OptimizeAnythingConfig(
    engine=engine,
    max_evals=10,
    stop_at_score=None,  # replace with the known metric ceiling when one exists
    sandbox=True,
    engine_config=(
        {"max_iterations": 3, "max_candidates_per_iter": 3}
        if engine == "meta_harness"
        else {}
    ),
)
```

The adapter alone enforces a default of four atomic starts per unique state directory, leaving
room for one known-pre-submission retry. For a composed run, use
`optimize_adaptive_sequential(..., patience=2)` so two
non-improving slices rotate to a fresh engine. A single-engine run uses its hard iteration and eval
caps instead; upstream does not expose a per-engine plateau stopper.

## Sizing the valset and the budget (read this — the #1 mistake)
`max_evals` is the *main* control over how long an in-process optimizer runs. Leave it at the default (100)
with a large valset, or set it too low, and the run stops after a **single proposal**, then reports
a "best candidate" that looks fine but is barely optimized.

- **valset** — on the gepa backend the best candidate is selected by its scores on the valset, so
  make it a **representative subset of your data**. There is no fixed size: pick what represents the task (a
  handful for small/expensive tasks, more when data is cheap and plentiful — anywhere from a few to
  hundreds). Bigger valset = less noisy selection but more eval calls per candidate.
- **budget** — every proposed candidate is scored on the **whole selection set**, so size
  `max_evals` off that set, not off a single proposal. Which set that is depends on the mode:
  ```
  generalization (dataset + valset):  max_evals ≳ 15–20 × len(valset)    # scored & selected on valset
  multi-task     (dataset only):      max_evals ≳ 15–20 × len(dataset)   # scored & selected on dataset
  single-task    (no dataset/valset): max_evals ≳ 15–20                  # 1 eval per candidate, so this
                                                                         #   IS the number of proposals
  ```
The constant is the same everywhere for the in-process `gepa` and `best_of_n` backends — **let the backend propose AND evaluate ~15–20 candidates**
  (more if you can afford it). Anything much less and the run tries only a couple of candidates —
  i.e. it's barely optimizing. (This arithmetic is exact for the gepa backend — and the best_of_n
  baseline — which score every candidate on the full selection set; the agentic backends decide
  themselves how to spend eval calls, so treat it as a floor.)

After the run, check how many proposals actually happened (on the gepa backend,
`result.metadata["gepa_result"]` holds every candidate; with `engine.write_agent_state=True` the
`run_dir/iterations/` tree shows each one). **If it stopped after one proposal, the budget was too
low** — raise it and rerun.

### Give every run a real stop condition
`max_evals` bounds eval calls, but add explicit stops so runs end at the right moment:
- **`stop_at_score`** — set it whenever your metric has a known ceiling (e.g. `1.0` for a pass rate /
  accuracy). The backend stops the moment a candidate reaches it instead of burning the rest of the
  budget at the optimum.
- **`max_token_cost`** — a hard USD cap for supported in-process backends. Do not set it for this
  adapter's `autoresearch` or `meta_harness` path: the adapter fails closed because
  Codex cannot enforce GEPA's `--max-budget-usd` contract.
- **an optional wall-clock `timeout`** only when you need an emergency operational stop; do not use
  elapsed time as the default Codex work budget.
- If you **opt in** to evaluation caching (`engine_config={"engine": {"cache_evaluation": True}}` on
  the gepa backend — it is **off by default**), be aware `max_evals` then counts only cache *misses*:
  a converged search can keep proposing cache-hitting candidates without consuming eval budget, so
  `stop_at_score` and a compatible cost or wall-clock bound become mandatory, not optional.

## Minimal working example
The example optimizes a system prompt for concreteness, but the **shape is identical** for any
candidate — swap `SEED` for a code file / config / etc. and have `evaluate` compile/run/measure it.
```python
from gepa.optimize_anything import optimize_anything, OptimizeAnythingConfig

SEED = "You are an expert. Solve the task. Output only the final answer."


def evaluate(candidate: str, example) -> tuple[float, dict]:
    output = run_my_model(
        system_prompt=candidate, user_prompt=example["prompt"]
    )  # your call
    score = grade(output, example)  # float, higher=better
    return score, {  # everything here is shown to the proposer LLM
        "score": score,
        "output": output,
        "error": example.get(
            "error"
        ),  # concrete, actionable feedback drives good proposals
    }


result = optimize_anything(
    seed_candidate=SEED,
    evaluator=evaluate,
    dataset=trainset,  # optimize on these (multi-task/generalization mode)
    valset=valset,  # select the best candidate on these (generalization mode)
    test_set=testset,  # OPTIONAL, reporting-only: seed + final candidate scored here at the end
    objective="Produce a prompt that maximizes task accuracy.",
    background="Domain rules, constraints, output format the model must follow.",
    config=OptimizeAnythingConfig(
        engine="gepa",  # swap to "autoresearch" / "meta_harness" — same code
        #   ("best_of_n" runs the same way, as a comparison baseline)
        name="my_run",
        max_evals=300,  # ≳ 15-20 × len(valset): enough for ~15-20 proposals (see above)
        stop_at_score=1.0,  # stop at the optimum (set when your metric has a known ceiling)
        max_concurrency=16,
        run_dir="runs/my_run",  # engine workspace (gepa run dir / agent work dir)
        output_dir="outputs/my_run",  # eval server: per-eval JSON, progress_log.jsonl, summary.json
        engine_config={  # gepa backend: a GEPAConfig-shaped dict, validated strictly —
            "reflection": {  #   an unknown key raises TypeError immediately (fail fast)
                "reflection_lm": "openai/gpt-5.1",
                "reflection_lm_kwargs": {},
                "reflection_minibatch_size": 5,
            },
            "engine": {"max_workers": 32, "seed": 0},  # seed = reproducibility
        },
    ),
)
print(result.best_candidate, result.best_score)
# held-out (only present if you passed test_set): "test_score" = average, "test_scores" = per-example
print(
    "held-out:",
    result.metadata.get("test_score"),
    "seed held-out:",
    result.metadata.get("baseline_test_score"),
)
```

## Standard workflow
1. **Pick the mode** (single-task / multi-task / generalization) by which of `dataset`/`valset` you pass.
2. **Define the score deliberately.** The optimizer optimizes exactly what you measure — gate the
   score on what you actually care about (see `references/gotchas.md`, reward hacking).
3. **Write a feedback-rich `evaluate`.** The `info` dict is the proposer's signal — return errors,
   diffs, partial credit, not just a number (`oa.log()` and `capture_stdio` can route diagnostics in
   automatically). See `references/writing_evaluators.md`.
4. **Pick a proposer LLM** — a LiteLLM id (set the provider key) or a custom LM-protocol callable.
   Validate it with a 1-call test before a long run.
5. **Set a budget** (`max_evals` sized per above; add `max_token_cost` only for a backend that can
   enforce it) plus `stop_at_score` when the metric has a ceiling.
6. **Run `python "$SKILL_DIR/scripts/preflight.py" --engine <engine>`** to fail fast on missing creds / CLI before a long run.
7. **Launch**, watch the first 1-2 evals (the eval→model→score chain), then let it run.
8. **Read `result.best_candidate` and `run_dir/`** (and `result.metadata["test_score"]` if you
   passed a `test_set`).

## Critical gotchas (read before a real run)

- **Linux sandbox visibility.** Stage and probe the runtime before an agentic run. Keep Codex and adapter state beneath `~/.cache`.
These silently degrade *results* — skim before launching:
- **Reward hacking.** Every backend optimizes exactly what you score; a weak proxy gets gamed (e.g. a
  "correct"-only score → a do-nothing wrapper). Gate the score on the real goal. → `references/gotchas.md`.
- **Selection bias.** In generalization mode the best candidate is the max over many scored on
  `valset` — an optimistic estimate. Use enough `valset` examples (and N>1 for stochastic models), and
  report on a `test_set` for an unbiased number. → `references/gotchas.md`.
- **Stochastic models default to N=1 per eval** → noisy selection. Average N samples *inside*
  `evaluate`. → `references/writing_evaluators.md`.
- **Saturated signal → the gepa backend returns the seed unchanged.** If the seed already aces the
  training examples, every proposal looks "not better" and is rejected (many proposals, ~0 accepted).
  Reflection needs examples the seed gets *wrong* to learn from — ensure `dataset` has real
  failures. → `references/gotchas.md`.
- **`engine_config` is validated strictly per backend** — an unknown key (including a leftover key
  from a different backend after swapping `engine=`) raises `TypeError` at construction. Swapping
  `engine=` means swapping the `engine_config` block. → `references/api.md`.
- **Agentic backends need the staged Codex runtime.** Keep `sandbox=True` and run `python "$SKILL_DIR/scripts/preflight.py" --engine <engine>` before a real run. See `references/runtime.md`.

## Reference files (load as needed)
- `references/api.md` — `OptimizeAnythingConfig`, the backends and their typed `engine_config`
  options, the three modes, the LM protocol, budget/cost semantics, `Result` shape, and the
  composition/pipeline helpers.
- `references/writing_evaluators.md` — the `(score, info)` contract, `oa.log()`/`capture_stdio`,
  LLM-as-judge scoring, multi-objective via `info["scores"]`, N>1 averaging, feedback design.
- `references/tracking.md` — enabling wandb / mlflow experiment tracking and what gets logged.
- `references/gotchas.md` — reward hacking, selection bias, the three modes, backend prerequisites.
- `references/omni.md` — paper-informed early engine comparison, winner selection, fresh continuation,
  Codex budget adaptation, and certification receipts.
- `references/runtime.md` — Linux staging, authentication, Bubblewrap, run state, and Codex adapter limits.
- `scripts/preflight.py` — validate credentials, proposer LM, and the staged Codex runtime before launching.
