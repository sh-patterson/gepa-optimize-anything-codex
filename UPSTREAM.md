# Upstream

This repository adapts GEPA's `gepa-optimize-anything` skill for Codex.

- Original upstream repository: https://github.com/gepa-ai/gepa
- Pinned compatibility fork: https://github.com/sh-patterson/gepa
- Upstream skill: `.claude/skills/gepa-optimize-anything`
- Pinned commit: `736739860458ff420066736e67c9910afcade9d7`
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

## AutoResearch repair in the pinned commit

Pinned commit `736739860458ff420066736e67c9910afcade9d7` is the current safe
fork head for [gepa-ai/gepa#415](https://github.com/gepa-ai/gepa/pull/415),
which remains open. It descends from the ComBEE runtime at
`ba30ee24e8f63dfdb9e557ed8cfaaec7aa09a6df` and retains the evaluation-session drain barrier, late-request rejection, and derives the winner from completed evaluation receipts.
It also retains proposal N+1 ordering repair. The earlier `3a6f93c5dd0beb68825973b3b2f2cae23060bbbb`
commit is the historical adapter baseline, not the active dependency pin. The
exact pinned checkout's focused AutoResearch and evaluation-server suite must
remain green.

That upstream lifecycle proof is necessary but not sufficient for an installed
Codex release claim. The public phase certifier must also reconcile installed
adapter receipts for GEPA, AutoResearch, and MetaHarness, select the Phase 1
winner from one evaluator, and conserve its exact bytes into a fresh
AutoResearch continuation.
