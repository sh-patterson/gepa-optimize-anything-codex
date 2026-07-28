# GEPA Optimize Anything for Codex

This repository ports GEPA's `gepa-optimize-anything` Claude Code skill to
Codex. It preserves GEPA's candidate, evaluator, engine, and budget contracts.
It changes only the agent command used by the `autoresearch` and
`meta_harness` engines.

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
```

Install GEPA and authenticate the Codex CLI:

```bash
pip install "gepa[full]"
codex --version
export OPENAI_API_KEY="..."
export CODEX_ADAPTER_STATE_DIR="$PWD/.codex-adapter-state"
export PATH="$PWD/.agents/skills/gepa-optimize-anything-codex/scripts:$PATH"
python .agents/skills/gepa-optimize-anything-codex/scripts/preflight.py \
  --engine autoresearch --no-sandbox
```

The first release supports `gpt-5.6-luna`, with `medium` reasoning by default.
The adapter's cost receipt uses that model's pricing snapshot. It rejects
other models rather than producing false cost accounting.

## Verify

```bash
python -m pytest -q
```

Set `RUN_CODEX_AGENT_SMOKE=1` only when you intend to make one paid Codex call:

```bash
RUN_CODEX_AGENT_SMOKE=1 python -m pytest -q -m live
```

## Linux sandbox limitation

The tested AutoResearch path uses `sandbox=False`. GEPA's bubblewrap jail mounts
system paths, `~/.local`, and the run directory. It cannot see a typical npm
Codex installation under `~/.npm-global`. `sandbox=True` works only when both
the `codex` executable and this adapter's `claude` command live in paths GEPA
mounts. Keep runs in a disposable work directory when using `sandbox=False`.

## Scope

This repository contains only the Codex skill port and compatibility command.
It does not include a benchmark, evaluator, domain prompt, article corpus, o
optimization result. Users supply the artifact and evaluator that GEPA runs.

The upstream skill is MIT licensed and pinned in
[`UPSTREAM.md`](UPSTREAM.md).
