# Upstream

This repository adapts GEPA's `gepa-optimize-anything` skill for Codex.

- Original upstream repository: https://github.com/gepa-ai/gepa
- Pinned compatibility fork: https://github.com/sh-patterson/gepa
- Upstream skill: `.claude/skills/gepa-optimize-anything`
- Pinned commit: `3a6f93c5dd0beb68825973b3b2f2cae23060bbbb`
- Upstream license: MIT
- Upstream copyright: Copyright (c) 2025 Lakshya A Agrawal

The method documentation and references remain upstream work. This port
changes the agent execution boundary from Claude Code to Codex and adds tests
for that translation.

Within `plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/`,
`references/tracking.md` and `references/writing_evaluators.md` remain exact
copies of the pinned source after line-ending normalization. `SKILL.md`,
`references/api.md`, and `references/gotchas.md` contain only the Codex-specific
install, model, budget, sandbox, and compatibility notes needed by this port.
`tests/test_upstream_fidelity.py` pins the unchanged-file hashes and rejects the
known text-truncation defects repaired from the original release.

## AutoResearch pin update gate

Do not update the pinned commit for an AutoResearch claim until an upstream GEPA
repair is available and reviewed. The repair must provide an `EvalServer wait_for_idle()`
operation or an equivalent drain barrier. It must derive the
returned candidate and score from a completed evaluation receipt rather than
the mutable `best_candidate.txt` workspace file. It must also prove that
evaluation N+1 is not created until evaluation N feedback is complete.

When that upstream commit exists, update the pin in `pyproject.toml`, the
installation command in `SKILL.md`, and `tests/test_upstream_fidelity.py` in
one change. Add an integration test that holds evaluation N open, emits an
outer Codex `turn.completed`, and proves the repaired engine neither starts
evaluation N+1 nor returns a winner before the drain barrier completes. Do not
invent an upstream commit hash or merge this plugin branch as proof that the
upstream behavior changed.
