# GEPA Optimize Anything for Codex

This repository ports GEPA's `gepa-optimize-anything` Claude Code skill to
Codex. It changes only the agent command used by the `autoresearch` and
`meta_harness` engines.

It ships a standalone repository-local Codex skill under `.agents/skills`,
not a packaged Codex plugin.

The included command is named `claude` because that is the interface GEPA
currently invokes. It translates the supported flags into `codex exec`,
converts Codex JSON events into the result shape GEPA expects, and maps
upstream session IDs to resumable Codex threads.

## Install

Copy the skill into a project's `.agents/skills` directory:

```bash
git clone https://github.com/sh-patterson/gepa-optimize-anything-codex.git
mkdir -p your-project/.agents/skills
cp -R gepa-optimize-anything-codex/.agents/skills/gepa-optimize-anything-codex \
  your-project/.agents/skills/
cd your-project
```

Install GEPA and authenticate the Codex CLI. An existing `codex login` is
supported, but no-call preflight can verify only that login is configured, not
that its token is fresh. For noninteractive automation, set `CODEX_API_KEY` or
`OPENAI_API_KEY`.

```bash
pip install "gepa[full] @ git+https://github.com/gepa-ai/gepa.git@f919db0a622e2e9f9204779b81fe00cc1b2d808f"
codex --version
codex login
# Or, for noninteractive automation:
# export CODEX_API_KEY="..."
export CODEX_ADAPTER_STATE_DIR="$PWD/.codex-adapter-state"
export PATH="$PWD/.agents/skills/gepa-optimize-anything-codex/scripts:$PATH"
python .agents/skills/gepa-optimize-anything-codex/scripts/preflight.py \
  --engine autoresearch --no-sandbox
```

The adapter translates GEPA's upstream `claude-sonnet-4-6` default to
`gpt-5.6-luna` with `medium` reasoning. It also accepts the target model name
when a caller supplies it directly. Other source model names fail before
Codex starts. Its token-derived USD field is a conservative standard-tier
estimate that prices uncached input at the cache-write rate and applies the
long-context multiplier; it is not provider billing.

## Agentic-engine limits

The compatibility command is Linux-only. Set `sandbox=False` and pass
`--no-sandbox` to preflight. The default GEPA bubblewrap sandbox does not mount
every Codex installation and auth location the adapter needs. Codex still runs
the translated command in its own `workspace-write` sandbox. The process
inherits non-Anthropic host credentials such as repository or cloud tokens,
matching GEPA's unsandboxed environment behavior; run from a disposable,
least-privilege environment and unset secrets the task does not need.

Do not set `max_token_cost` for `autoresearch` or `meta_harness`. GEPA sends it
to the compatibility command as `--max-budget-usd`; the adapter rejects that
flag before spawning Codex because it cannot enforce a USD ceiling. Use
`max_evals`, `stop_at_score`, and a host-level timeout. Set an external spend
limit in the OpenAI account before a paid run.

The adapter accepts pinned GEPA's exact
`--disallowedTools=WebFetch,WebSearch`, `--output-format json`, and
`--permission-mode bypassPermissions` arguments. It rejects other policy
values, unknown Claude CLI flags, and `--settings` before spawning Codex. It
maps the web-tool denial by disabling Codex's standalone web-search feature;
as with GEPA's unsandboxed Claude path, shell commands still have network
access.

## Verify

```bash
python -m pytest -q
```

Run the direct live smoke only when you intend to make one Codex model call:

```bash
RUN_CODEX_AGENT_SMOKE=1 python -m pytest -q \
  tests/test_adapter.py::test_live_codex_round_trip
```

The GEPA live smoke is separate and may invoke the agent more than once:

```bash
RUN_CODEX_AGENT_SMOKE=1 python -m pytest -q \
  tests/test_live_optimize_anything.py::test_autoresearch_improves_a_deterministic_text_candidate
```

## Scope

This repository contains only the Codex skill port and compatibility command.
It does not include a benchmark, evaluator, domain prompt, article corpus, or
optimization result. Users supply the artifact and evaluator that GEPA runs.

The upstream skill is MIT licensed and pinned in
[`UPSTREAM.md`](UPSTREAM.md).
