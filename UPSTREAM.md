# Upstream

This repository adapts GEPA's `gepa-optimize-anything` skill for Codex.

- Original upstream repository: https://github.com/gepa-ai/gepa
- Upstream skill: `.claude/skills/gepa-optimize-anything`
- Pinned commit: `0310bb7b4952d4695718f9f557e450fd6781301e`
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
