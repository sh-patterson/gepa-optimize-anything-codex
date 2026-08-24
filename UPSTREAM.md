# Upstream

This repository adapts GEPA's `gepa-optimize-anything` skill for Codex.

- Original upstream repository: https://github.com/gepa-ai/gepa
- Upstream skill baseline: `.claude/skills/gepa-optimize-anything` at `b265bf9ca77fd8e8d82039d9f74911b8780fe1ce`
- Pinned compatibility fork: https://github.com/sh-patterson/gepa
- Pinned commit: `2943746ebf77dc2c6b8d986dd9a6b074952525ef`
- Upstream license: MIT
- Upstream copyright: Copyright (c) 2025 Lakshya A Agrawal

The method documentation and references remain upstream work. This port
changes the agent execution boundary from Claude Code to Codex and adds tests
for that translation.

Within `plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/`,
`references/tracking.md` and `references/writing_evaluators.md` remain exact
copies of the upstream baseline after line-ending normalization. `SKILL.md`,
`references/api.md`, and `references/gotchas.md` contain only the Codex-specific
install, model, budget, sandbox, and compatibility notes needed by this port.
`scripts/sync_upstream_skill_docs.py` refreshes the copied references from the
upstream baseline; `tests/test_upstream_fidelity.py` pins the unchanged-file
hashes and rejects the known text-truncation defects repaired from the original
release.

## Fork-only AutoResearch repair

Pinned commit `2943746ebf77dc2c6b8d986dd9a6b074952525ef` tracks `gepa-ai/gepa`
main and reapplies the fork-only evaluation-session drain barrier locally. It
rejects late requests after close, derives the winner from completed evaluation receipts, and prevents proposal N+1 from starting before evaluation N feedback
completes. This repair is maintained on `sh-patterson/gepa` only; it is not
submitted upstream.

That lifecycle proof is necessary but not sufficient for an installed Codex
release claim. The public phase certifier must also reconcile installed
adapter receipts for GEPA, AutoResearch, and MetaHarness, select the Phase 1
winner from one evaluator, and conserve its exact bytes into a fresh
AutoResearch continuation.

## Dependency drift gate

The `live` extra is the source-of-truth GEPA pin. Release provenance now reads
that declared commit and fails if the installed VCS commit differs, so a stale
environment cannot produce a valid receipt. When upstream `gepa-ai/gepa` moves,
rebase the lifecycle fix onto the current upstream head, run the upstream
fidelity and lifecycle suites, and update this file and `pyproject.toml` in the
same change. Do not repin directly to upstream or adopt a native proposer until
the Codex model, budget, sandbox-custody, timeout, and live-parity gates are
separately demonstrated.
