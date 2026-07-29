# GEPA Optimize Anything for Codex

This repository packages GEPA's `optimize_anything` workflow for Codex. It
keeps GEPA's candidate, evaluator, and engine logic intact. A narrow
compatibility command lets the `autoresearch` and `meta_harness` engines use
Codex in place of Claude Code.

The repository is a Codex marketplace containing one skills-only plugin.

The included command is named `claude` because that is the interface GEPA
currently invokes. It translates the supported flags into `codex exec`,
converts Codex JSON events into the result shape GEPA expects, and maps
upstream session IDs to resumable Codex threads.

## Install

Add the GitHub marketplace, then install the plugin:

```bash
codex plugin marketplace add sh-patterson/gepa-optimize-anything-codex
codex plugin add gepa-optimize-anything@gepa-optimize-anything-codex
```

Start a new Codex task after installation. Invoke
`$gepa-optimize-anything-codex` with the artifact and evaluator you want to
improve.

When invoked, the skill directs Codex to install pinned GEPA when needed and
launch it with the bundled adapter first on `PATH`. Agentic runs require Linux,
the Codex CLI, `jq`, and either `codex login`, `CODEX_API_KEY`, or
`OPENAI_API_KEY`.

The adapter translates GEPA's upstream `claude-sonnet-4-6` default to
`gpt-5.6-luna` with `high` reasoning. It also accepts the target model name
when a caller supplies it directly. Other source model names fail before
Codex starts. Its token-derived USD field is a conservative standard-tier
estimate that prices uncached input at the cache-write rate and applies the
long-context multiplier; it is not provider billing.

## Supported agentic configuration

The compatibility command is Linux-only and uses GEPA's default Bubblewrap
sandbox. Install Codex with
`npm install --prefix "$HOME/.local" @openai/codex`, prepend
`~/.local/node_modules/.bin` to `PATH`, set `CODEX_API_KEY`, and stage the
adapter with `sandbox_runtime.py stage`. Run preflight before starting an
agentic engine. Codex uses writable homes beneath `~/.cache`.

Do not set `max_token_cost` for `autoresearch` or `meta_harness`. GEPA sends it
to the compatibility command as `--max-budget-usd`; the adapter rejects that
flag before spawning Codex because it cannot enforce a USD ceiling. Use
`max_evals`, `stop_at_score`, and a host-level timeout. Set an external spend
limit in the OpenAI account before a paid run. That account-level control does
not satisfy GEPA's per-invocation hard-budget contract. The release gate remains
[externally blocked](HARD_BUDGET.md).

The adapter accepts pinned GEPA's exact
`--disallowedTools=WebFetch,WebSearch`, `--output-format json`, and
`--permission-mode bypassPermissions` arguments. It rejects other policy
values, unknown Claude CLI flags, and `--settings` before spawning Codex. It
maps the web-tool denial by disabling Codex's standalone web-search feature;
as with GEPA's unsandboxed Claude path, shell commands still have network
access.

## Verify from a source checkout

```bash
python -m pytest -q
```

Run the direct adapter smoke only when you intend to make one Codex model call:

```bash
RUN_CODEX_AGENT_SMOKE=1 python -m pytest -q \
  tests/test_adapter.py::test_live_codex_round_trip
```

The release dogfood runner makes paid calls. It runs one bounded engine, writes
a durable receipt, and exits nonzero when its evidence is incomplete. Run each
engine separately:

```bash
RUN_CODEX_AGENT_SMOKE=1 python scripts/release_dogfood.py --engine autoresearch
RUN_CODEX_AGENT_SMOKE=1 python scripts/release_dogfood.py --engine meta_harness
```

The matching pytest cases are also opt-in. They verify Luna with high reasoning,
the Bubblewrap sandbox, a deterministic `RED` to `BLUE` result, positive usage
and estimate, cost agreement, session mapping, and the persisted receipt:

```bash
RUN_CODEX_AGENT_SMOKE=1 python -m pytest -q tests/test_live_optimize_anything.py
```

Do not set `max_token_cost`. For release dogfood, use a dedicated project hard
limit and inspect each durable runner receipt before starting the next engine.
A submitted call and its accounting can arrive after that check. This is an
operational backstop, not per-invocation USD enforcement.

## Scope

This repository contains only the Codex skill port and compatibility command.
It does not include a benchmark, evaluator, domain prompt, article corpus, or
optimization result. Users supply the artifact and evaluator that GEPA runs.

The upstream skill is MIT licensed and pinned in
[`UPSTREAM.md`](UPSTREAM.md).
