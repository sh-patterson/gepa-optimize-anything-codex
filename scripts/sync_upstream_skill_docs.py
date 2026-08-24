#!/usr/bin/env python3
"""Merge gepa-ai/gepa skill docs with Codex adapter overlays."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
)
UPSTREAM_REF = "b265bf9ca77fd8e8d82039d9f74911b8780fe1ce"
UPSTREAM_REPO = "https://github.com/gepa-ai/gepa.git"
PINNED_GEPA_COMMIT = "2943746ebf77dc2c6b8d986dd9a6b074952525ef"
PINNED_GEPA_DEPENDENCY = (
    f"gepa[full] @ git+https://github.com/sh-patterson/gepa.git@{PINNED_GEPA_COMMIT}"
)


def upstream_text(relative: str) -> str:
    path = f".claude/skills/gepa-optimize-anything/{relative}"
    return subprocess.check_output(
        ["git", "show", f"{UPSTREAM_REF}:{path}"],
        cwd=_upstream_checkout(),
        text=True,
    ).replace("\r\n", "\n")


def _upstream_checkout() -> Path:
    checkout = Path("/tmp/gepa-upstream-sync")
    if not (checkout / ".git").exists():
        subprocess.check_call(
            ["git", "clone", "--depth", "1", UPSTREAM_REPO, str(checkout)],
            stdout=subprocess.DEVNULL,
        )
    subprocess.check_call(
        ["git", "fetch", "origin", UPSTREAM_REF],
        cwd=checkout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return checkout


def adapt_api(text: str) -> str:
    text = text.replace(
        "| `max_token_cost` | `None` | USD cap on the engine's **own** optimizer-LLM spend (reflection/agent). Enforced by the engine (gepa: `max_reflection_cost` stopper; agent engines: `--max-budget-usd`), not the eval server. |",
        "| `max_token_cost` | `None` | USD cap on the engine's **own** optimizer-LLM spend for backends that can enforce it (for example, `gepa` via `max_reflection_cost`). The Codex `autoresearch` and `meta_harness` adapters reject this field before launch because they cannot enforce a per-invocation USD ceiling. |",
    )
    text = text.replace(
        "| `sandbox` | **`True`** | OS-jail the subprocess engines' `claude` sessions: bwrap on Linux (needs the `bubblewrap` package — the run aborts at launch if `bwrap` is missing), Claude Code's Seatbelt sandbox on macOS. Also forces the work dir to a tempdir even when `run_dir` is set (artifacts are mirrored back). `False` prints a loud warning and runs the agent unsandboxed (`bypassPermissions`, full host access). |",
        "| `sandbox` | **`True`** | OS-jail the subprocess engines' compatibility sessions with Bubblewrap on Linux (needs the `bubblewrap` package — the run aborts at launch if `bwrap` is missing). This Codex adapter does not support macOS agentic execution. Also forces the work dir to a tempdir even when `run_dir` is set (artifacts are mirrored back). `False` prints a loud warning and runs the agent unsandboxed (`bypassPermissions`, full host access). |",
    )
    insert = """
### Codex adapter exceptions

For this repository's Linux-only Codex compatibility command, keep the default
`sandbox=True` and stage the runtime before launch.
Do not pass `max_token_cost` to `autoresearch` or `meta_harness`: the adapter
rejects the resulting `--max-budget-usd` flag before Codex starts because it
cannot enforce a USD cap. Callers must explicitly set `max_evals=10` for Codex
agentic runs and, for `meta_harness`, `max_iterations=3` plus
`max_candidates_per_iter=3`. The adapter alone enforces a default of four atomic starts per state directory. The adapter maps its supported Claude model names to
a pinned Codex target and reports token-derived USD as an estimate, not
provider billing. It accepts GEPA's `--disallowedTools=...` form and rejects
unknown flags plus `--settings` before Codex starts.

"""
    marker = "## The backends"
    text = text.replace(marker, insert + marker, 1)
    text = text.replace(
        "| `autoresearch` | one Claude Code subprocess iterates in a work dir (`program.md`, `candidate.txt`, `eval.sh` → HTTP eval server) | subprocess | `claude` on PATH + headless auth, `jq` |",
        "| `autoresearch` | one Codex subprocess iterates in a work dir (`program.md`, `candidate.txt`, `eval.sh` → HTTP eval server) | subprocess | staged Codex runtime + Codex auth, `jq` |",
    )
    text = text.replace(
        "| `meta_harness` | a Claude subprocess reads frontier/history and writes `pending_eval.json` candidates; the engine benchmarks each | subprocess | `claude` on PATH + headless auth |",
        "| `meta_harness` | a Codex subprocess reads frontier/history and writes `pending_eval.json` candidates; the engine benchmarks each | subprocess | staged Codex runtime + Codex auth |",
    )
    text = text.replace(
        "`scripts/preflight.py` checks a backend's prerequisites before a long run.",
        'Run `python "$SKILL_DIR/scripts/preflight.py" --engine <engine>` to check a\nbackend\'s prerequisites before a long run.',
    )
    text = text.replace(
        'OptimizeAnythingConfig(engine="autoresearch", max_evals=100, max_token_cost=5.0),',
        'OptimizeAnythingConfig(engine="autoresearch", max_evals=10, sandbox=True),',
    )
    if "For Codex agentic compositions" not in text:
        text = text.replace(
            "  `max_switches`.",
            "  `max_switches`.\n\nFor Codex agentic compositions, use `patience=2`. Individual `autoresearch` and `meta_harness`\nruns do not expose a common plateau setting, so their iteration, invocation, and evaluation caps\nremain the stop boundary.",
        )
    return text


def adapt_gotchas(text: str) -> str:
    text = text.replace(
        "## 6. Agentic backends have launch-time prerequisites\n"
        "`autoresearch` / `meta_harness` `subprocess.Popen([\"claude\", ...])`. A missing `claude` CLI — or, on\n"
        "Linux, a missing `bwrap` (bubblewrap) while the default `sandbox=True` is in effect — aborts the run\n"
        "at launch with a boxed message and install instructions (`npm install -g @anthropic-ai/claude-code`;\n"
        "`sudo apt/dnf install bubblewrap`). An *unauthenticated* CLI or a missing `jq` (used by\n"
        "autoresearch's generated `eval.sh`) still surfaces only mid-run, and `sandbox=False` runs the agent\n"
        "unconfined (loud warning) — so run `scripts/preflight.py` first either way.",
        "## 6. Agentic backends have launch-time prerequisites\n"
        "`autoresearch` and `meta_harness` need the staged Codex runtime. A missing\n"
        "runtime, Codex CLI, `bwrap`, `jq`, or sandbox authentication fails preflight\n"
        "before the optimizer starts. An explicit `--no-sandbox` run is unconfined\n"
        "(loud warning). Run\n"
        '`python "$SKILL_DIR/scripts/preflight.py" --engine <engine>` first either way.\n'
        "See `runtime.md` for the internal process contract.",
    )
    text = text.replace(
        "## 7. Give runs a real stop condition (`stop_at_score` / `max_token_cost`)",
        "## 7. Give runs a real stop condition (`stop_at_score` / bounded work)\n\n"
        "Sandboxed agentic runs require either `CODEX_API_KEY` or a ChatGPT login created\n"
        "with `sandbox_runtime.py login` in the isolated runtime home. A normal\n"
        "`~/.codex` login remains available only to explicit `--no-sandbox` runs.\n"
        "`OPENAI_API_KEY` is reserved for GEPA's in-process models and is not translated\n"
        "into Codex authentication.",
    )
    text = text.replace(
        "  times out, still spending proposer-LLM tokens. With caching on, `stop_at_score` and/or\n"
        "  `max_token_cost` (plus a wall-clock `timeout` on the launched process) are mandatory.\n"
        "  A distinct `valset` is a separate cache namespace from the trainset (list-position ids would\n"
        "  otherwise collide). `valset=None` still shares cached rollouts with minibatches. Resuming a\n"
        "  `run_dir` whose cache predates that namespacing drops the old entries.\n"
        "Agentic backends also spend LLM tokens between evals — cap them with `max_token_cost`\n"
        "(enforced as `--max-budget-usd`).",
        "  times out, still spending proposer-LLM tokens. With caching on, `stop_at_score` and/or a compatible\n"
        "  cost or wall-clock bound are mandatory.\n"
        "  A distinct `valset` is a separate cache namespace from the trainset (list-position ids would\n"
        "  otherwise collide). `valset=None` still shares cached rollouts with minibatches. Resuming a\n"
        "  `run_dir` whose cache predates that namespacing drops the old entries.\n"
        "This Codex adapter rejects agentic `max_token_cost` because it cannot enforce GEPA's\n"
        "`--max-budget-usd` contract. Callers must explicitly set `max_evals=10` for Codex agentic runs;\n"
        "for `meta_harness`, also set `max_iterations=3` and `max_candidates_per_iter=3`. The adapter alone\n"
        "enforces a default of four atomic starts per state directory and retries once only when Codex is\n"
        "known not to have started. The retry consumes a start; ambiguous or usage-bearing calls are never\n"
        "retried. Use an account spend\n"
        "limit as a secondary backstop. A host timeout is optional rather than the default work budget.",
    )
    if "## 11. An outer AutoResearch turn" not in text:
        text = text.replace(
            "## Quick pre-flight checklist",
            "## 11. An outer AutoResearch turn is not an evaluation receipt\n\n"
            "The pinned GEPA commit provides an evaluation-session drain barrier,\n"
            "receipt-derived winner selection, and an ordering test that blocks proposal N+1\n"
            "until evaluation N feedback completes. Those tests repair the lifecycle defect;\n"
            "they do not make an outer `turn.completed` record sufficient release evidence.\n\n"
            "For an installed claim, reconcile the adapter journal, evaluation receipt,\n"
            "candidate hash, and session mapping. For the paper-informed multi-engine claim,\n"
            "also require all three Phase 1 branches and the exact-winner fresh continuation\n"
            "receipt.\n\n"
            "## Quick pre-flight checklist",
        )
    text = text.replace(
        "- [ ] `stop_at_score` set when the metric has a ceiling; `max_token_cost` for agentic backends",
        "- [ ] `stop_at_score` set when the metric has a ceiling; `max_token_cost` only where enforceable",
    )
    text = text.replace(
        "- [ ] for agentic backends: `claude` CLI on PATH + authed, `jq` installed, and on Linux `bwrap`\n"
        "      (bubblewrap) for the default `sandbox=True` (`scripts/preflight.py`)",
        "- [ ] for agentic backends: staged Codex runtime + Codex auth, `jq` installed, and\n"
        "      a passing Bubblewrap preflight with staged ChatGPT login or `CODEX_API_KEY`\n"
        "- [ ] for Codex agentic backends: explicitly set `max_evals=10`; for `meta_harness`, set\n"
        "      `max_iterations=3` and `max_candidates_per_iter=3`; use unique adapter state so its\n"
        "      four-start cap is meaningful",
    )
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


def main() -> int:
    (SKILL / "references").mkdir(parents=True, exist_ok=True)

    for relative in ("references/tracking.md", "references/writing_evaluators.md"):
        content = upstream_text(relative)
        (SKILL / relative).write_text(content, encoding="utf-8")

    api = adapt_api(upstream_text("references/api.md"))
    (SKILL / "references/api.md").write_text(api, encoding="utf-8")

    gotchas = adapt_gotchas(upstream_text("references/gotchas.md"))
    (SKILL / "references/gotchas.md").write_text(gotchas, encoding="utf-8")

    hashes = {
        rel: sha256_text((SKILL / rel).read_text(encoding="utf-8"))
        for rel in (
            "SKILL.md",
            "references/api.md",
            "references/gotchas.md",
            "references/tracking.md",
            "references/writing_evaluators.md",
        )
    }
    print("Synced api.md, gotchas.md, tracking.md, writing_evaluators.md")
    print("PINNED_GEPA_COMMIT =", PINNED_GEPA_COMMIT)
    print("UPSTREAM_REF =", UPSTREAM_REF)
    for rel, digest in hashes.items():
        print(f"  {rel}: {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
