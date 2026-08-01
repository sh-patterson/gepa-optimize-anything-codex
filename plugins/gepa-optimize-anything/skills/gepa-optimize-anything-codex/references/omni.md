# Paper-informed multi-engine optimization

Read this reference when the task is expensive or open-ended and there is no evidence that one
optimizer family is best.

## What the published result supports

The GEPA Omni experiment reported two findings on Frontier-CS under a matched $20 budget:

- No single optimizer dominated. GEPA, AutoResearch, and MetaHarness each won different problems.
- Optimizers made most gains early. A fresh optimizer often improved the first optimizer's best
  candidate after it plateaued.

Omni applied both findings. Phase 1 gave all three engines small parallel budget slices and selected
the best result. Phase 2 seeded a fresh optimizer from that winner for the remaining budget.

Source: https://gepa-ai.github.io/gepa/blog/2026/07/22/optimize-anything-omni/

Do not generalize the Frontier-CS result into a promise that Omni wins on another task. Treat it as
a search design to test against a matched standalone optimizer and best-of-N baseline.

## Run contract

1. Freeze one task contract. Every branch receives the exact same candidate bytes, evaluator,
   objective, background, dataset, validation set, test set, model settings, and score ceiling.
2. Preflight every required engine before spending the exploration budget. Give each Phase 1 engine
   an equal bounded slice. Use `optimize_best_of` when the engines' `best_score` values come from the
   same evaluator. Use `optimize_vote` when a common re-score is needed for a fair comparison.
3. Record each branch's candidate hash, score, evaluation receipts, model settings, usage, duration,
   and terminal status. A failed required branch fails the composition proof.
4. Select one Phase 1 winner. Preserve the winner text and SHA-256 digest.
5. Start Phase 2 with a new optimizer instance and fresh run, output, state, and session directories.
   Pass the exact winner bytes as its seed. Do not resume a Phase 1 process and call it fresh.
6. Compare the continuation against the original seed and a matched standalone or best-of-N run.
   Keep optimization evidence separate from held-out test evidence.

The minimum custody chain is:

```text
frozen seed and evaluator
  -> Phase 1 branch evaluation receipts
  -> selected winner bytes and SHA-256
  -> fresh continuation seed bytes and SHA-256
  -> continuation evaluation receipts
  -> final candidate and score
```

## Codex budget adaptation

The published experiment matched USD budgets. This Codex adapter cannot enforce GEPA's
`--max-budget-usd` contract for AutoResearch or MetaHarness. For Codex runs:

- Use equal explicit `max_evals` slices in Phase 1.
- Record observed token usage and estimated cost as metadata, not as a provider-enforced budget.
- Make no dollar-matched claim.
- Set `stop_at_score` when the evaluator has a known ceiling.
- Use zero ambiguous retries and a host timeout only as an emergency stop.
- Give every concurrent engine unique run, output, adapter-state, and Codex-session directories.

Equal evaluation counts are an operational adaptation. They are not equivalent to the paper's
matched-dollar experiment.

## Certification boundary

Do not call a run Omni unless all three required engines reached valid terminal states under one
shared evaluator, the Phase 1 winner was selected from comparable scores, and a fresh continuation
received the exact winner bytes.

As of v1.0.3, the installed Codex paths for GEPA and best-of-N have narrow probe evidence, and the
exact pinned GEPA checkout's tests verify AutoResearch's drain barrier, receipt-derived winner, and
feedback ordering. Installed AutoResearch, MetaHarness, and the complete three-engine continuation
remain pending until the public phase certifier passes. Until then, describe the implementation as
paper-informed and not as a certified reproduction of the published Omni method.
