from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS))

import codex_agent_runner as agent  # noqa: E402
import codex_runtime as runtime  # noqa: E402


class FakeRuntime:
    def __init__(self) -> None:
        self.specs: list[object] = []

    def invoke(self, spec: object) -> object:
        self.specs.append(spec)
        return runtime.InvocationResult(
            invocation_id=spec.invocation_id,
            provider="app_server",
            text="done",
            status="completed",
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            sandbox=spec.sandbox,
            thread_id=spec.resume_thread_id or "thread-1",
            turn_id=f"turn-{len(self.specs)}",
            usage=runtime.TokenUsage(2, 1, 3, 1, 5),
            duration_ms=5,
            runtime_version="0.144.4",
            auth_mode="chatgpt",
        )


def _request(tmp_path: Path, **overrides: object) -> object:
    values = {
        "continuation_id": "continuation-1",
        "resume": False,
        "prompt": "work",
        "cwd": tmp_path.resolve(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "sandbox": "workspace-write",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runner_resumes_only_the_mapped_codex_thread(tmp_path: Path) -> None:
    native = FakeRuntime()
    runner = agent.CodexAgentRunner(native)

    first = runner.run(_request(tmp_path))
    second = runner.run(_request(tmp_path, resume=True, prompt="continue"))

    assert first.thread_id == second.thread_id == "thread-1"
    assert native.specs[0].resume_thread_id is None
    assert native.specs[1].resume_thread_id == "thread-1"
    assert second.cost_usd is None
    assert second.usage["total_tokens"] == 5


def test_runner_rejects_unknown_resume_and_duplicate_start(tmp_path: Path) -> None:
    runner = agent.CodexAgentRunner(FakeRuntime())

    with pytest.raises(ValueError, match="unknown"):
        runner.run(_request(tmp_path, resume=True))

    runner.run(_request(tmp_path))
    with pytest.raises(ValueError, match="already been started"):
        runner.run(_request(tmp_path))
