from __future__ import annotations

import sys
from pathlib import Path

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

import codex_lm  # noqa: E402
import codex_runtime  # noqa: E402


def _config(tmp_path: Path, **overrides: object) -> object:
    values = {
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "sandbox_mode": "workspace-write",
        "evidence_dir": (tmp_path / "evidence").resolve(),
        "timeout_seconds": 30,
        "allow_cli_fallback": False,
    }
    values.update(overrides)
    return codex_lm.CodexLMConfig(**values)


class FakeBackend:
    kind = "app_server"

    def probe(self) -> object:
        return codex_runtime.BackendProbe(self.kind, True, "0.144.4", "chatgpt")

    def invoke(self, spec: object) -> object:
        return codex_runtime.InvocationResult(
            invocation_id=spec.invocation_id,
            provider=self.kind,
            text="BLUE",
            status="completed",
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            sandbox=spec.sandbox,
            thread_id="thread-1",
            turn_id="turn-1",
            usage=codex_runtime.TokenUsage(2, 1, 3, 1, 5),
            duration_ms=5,
            runtime_version="0.144.4",
            auth_mode="chatgpt",
        )

    def close(self) -> None:
        pass


def _runtime(config: object) -> object:
    return codex_runtime.CodexRuntime(
        evidence=codex_runtime.EvidenceStore(config.evidence_dir),
        primary=FakeBackend(),
        fallback=None,
    )


def test_public_lm_returns_text_and_native_receipt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lm = codex_lm.CodexLM(
        config,
        cwd=tmp_path,
        environment={"OPENAI_API_KEY": "evaluator-only"},
        runtime=_runtime(config),
    )

    assert lm("prompt") == "BLUE"
    assert lm.total_usage == {
        "input_tokens": 2,
        "cached_input_tokens": 1,
        "output_tokens": 3,
        "reasoning_output_tokens": 1,
        "total_tokens": 5,
    }
    assert lm.total_cost == 0
    assert lm.cost_status == "unknown"
    assert lm.last_result is not None
    assert lm.last_result.provider == "app_server"
    assert lm.last_result.estimated_cost_usd is None
    assert lm.last_result.raw_receipt_path.is_file()
    with pytest.raises(TypeError):
        lm.last_result.usage["input_tokens"] = 9


def test_public_lm_rejects_reused_evidence_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    codex_lm.CodexLM(config, cwd=tmp_path, runtime=_runtime(config))

    with pytest.raises(RuntimeError, match="already been used"):
        codex_lm.CodexLM(config, cwd=tmp_path, runtime=_runtime(config))


def test_public_lm_rejects_litellm_only_options(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="sampling options"):
        codex_lm.CodexLM(
            config,
            cwd=tmp_path,
            runtime=_runtime(config),
            temperature=0.7,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "other"},
        {"reasoning_effort": "low"},
        {"timeout_seconds": 0},
        {"sandbox_mode": "full-access"},
        {"evidence_dir": Path("relative")},
    ],
)
def test_public_config_fails_closed(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _config(tmp_path, **overrides)
