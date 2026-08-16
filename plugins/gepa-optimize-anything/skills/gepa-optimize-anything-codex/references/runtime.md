# Codex-native runtime

The supported runtime uses the stable `openai-codex` Python SDK and local
Codex App Server. Direct `codex exec --json` is a narrow fallback behind the
same typed interface. Engine code never builds either transport protocol.

```text
GEPA / Best-of-N -> CodexLM ---------+
                                      +-> CodexRuntime -> App Server
AutoResearch / MetaHarness -> AgentRunner                  -> CLI fallback
```

`CodexRuntime` selects one provider during `prepare()` and freezes that choice.
Fallback is allowed only when App Server is unavailable before an invocation
starts. It never retries an ambiguous, usage-bearing, or completed turn on a
different provider.

## Desktop readiness

Install the repository's `live` extra, then run the no-model probe:

```powershell
python "$SKILL_DIR/scripts/native_preflight.py" `
  --evidence-dir "$RUN_DIR/runtime-evidence" `
  --codex-home "$HOME/.codex"
```

The report names the selected provider, runtime version, and sanitized auth
mode. `provider_call_made` remains false: account and version checks do not
start a model turn. A managed Codex task may need explicit read/write access to
the existing Codex home because App Server owns state there.

## Engine integration

- `gepa` and `best_of_n` receive `scripts/codex_lm.py`, a callable over the
  native runtime.
- `autoresearch` receives `scripts/codex_agent_runner.py` and maps its Ralph
  continuation identity to exactly one Codex thread.
- `meta_harness` receives the same runner with a fresh continuation identity
  per proposal iteration.

The pinned GEPA fork owns only the provider-neutral `AgentRunner` request and
result types. It does not import the plugin or know about App Server, the Codex
CLI, authentication, or receipt layout.

```python
from codex_agent_runner import CodexAgentRunner
from codex_runtime import create_runtime

runtime = create_runtime(
    evidence_dir=(run_dir / "runtime-evidence").resolve(),
    codex_home=(Path.home() / ".codex").resolve(),
)
runner = CodexAgentRunner(runtime)

config = OptimizeAnythingConfig(
    engine="autoresearch",
    max_evals=10,
    run_dir=str(run_dir / "work"),
    output_dir=run_dir / "evaluations",
    engine_config={
        "model": "gpt-5.6-luna",
        "effort": "high",
        "agent_runner": runner,
        "agent_timeout_seconds": 600,
    },
)
```

Use the same runner for MetaHarness, with `max_iterations=3` and
`max_candidates_per_iter=3`. Close `runtime` after the optimizer returns.

## Safety and custody

Every invocation explicitly uses `ApprovalMode.deny_all`. Supported sandboxes
are `read-only` and `workspace-write`; full access is rejected at the typed
boundary. Web search and workspace network access are disabled in the Codex
configuration. App Server and CLI backends emit the same immutable invocation
record, with one exclusive claim and one terminal receipt per invocation.

Timeouts interrupt App Server turns or terminate the CLI process group. A turn
that does not reach a terminal state after interruption is `ambiguous` and is
never retried. AutoResearch also stops continuation when an iteration makes no
evaluation progress.

Codex reports tokens but not provider USD billing for a Desktop-authenticated
turn. Receipts therefore set `cost_status="unknown"`; they do not invent a
zero or estimated bill. Do not set agentic `max_token_cost`. Bound work with
`max_evals`, `max_iterations`, `max_candidates_per_iter`, `stop_at_score`, and
the per-agent timeout.

## Legacy bridge

The old `claude` compatibility launcher remains temporarily for historical
release receipts and rollback. It is not the default native architecture.
Delete it only after clean-install Desktop and authorized live parity evidence
exists for all four engines and the native release certifier has replaced the
legacy adapter evidence schema.
