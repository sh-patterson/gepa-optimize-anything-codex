from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "gepa-optimize-anything" / "skills" / "gepa-optimize-anything-codex" / "scripts" / "codex_lm.py"
SPEC = importlib.util.spec_from_file_location("public_codex_lm_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
codex_lm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_lm
SPEC.loader.exec_module(codex_lm)


def _config(tmp_path: Path) -> object:
    return codex_lm.CodexLMConfig(
        executable=tmp_path / "claude",
        model="gpt-5.6-luna",
        reasoning_effort="high",
        sandbox_mode="workspace-write",
        state_dir=tmp_path / "state",
        session_dir=tmp_path / "sessions",
        timeout_seconds=30,
    )


def test_public_lm_returns_text_and_writes_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        session_id = command[command.index("--session-id") + 1]
        payload = {"is_error": False, "session_id": session_id,
                   "adapter_target_model": "gpt-5.6-luna", "result": "BLUE",
                   "codex_thread_id": "thread-1", "usage": {"input_tokens": 2, "output_tokens": 3}}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(codex_lm.subprocess, "run", fake_run)
    lm = codex_lm.CodexLM(_config(tmp_path), cwd=tmp_path, environment={"SAFE": "1"})

    assert lm("prompt") == "BLUE"
    assert seen[seen.index("--model") + 1] == "gpt-5.6-luna"
    assert seen[seen.index("--effort") + 1] == "high"
    assert lm.total_usage == {"input_tokens": 2, "output_tokens": 3}
    assert lm.last_result is not None
    assert lm.last_result.raw_receipt_path.is_file()
    assert lm.last_result.session_mapping_path.is_file()
    with pytest.raises(TypeError):
        lm.last_result.usage["input_tokens"] = 9


def test_public_lm_rejects_reused_state_and_invalid_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    lm = codex_lm.CodexLM(config, cwd=tmp_path, environment={})
    with pytest.raises(RuntimeError, match="already been used"):
        codex_lm.CodexLM(config, cwd=tmp_path, environment={})
    monkeypatch.setattr(codex_lm.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "{}", ""))
    with pytest.raises(RuntimeError, match="invocation failed"):
        lm("prompt")


@pytest.mark.parametrize("kwargs", [
    {"model": "other"},
    {"reasoning_effort": "low"},
    {"timeout_seconds": 0},
    {"retry_ceiling": -1},
])
def test_public_config_fails_closed(tmp_path: Path, kwargs: dict[str, object]) -> None:
    values = {"executable": tmp_path / "claude", "model": "gpt-5.6-luna", "reasoning_effort": "high", "sandbox_mode": "workspace-write", "state_dir": tmp_path / "state", "session_dir": tmp_path / "sessions", "timeout_seconds": 1}
    values.update(kwargs)
    with pytest.raises(ValueError):
        codex_lm.CodexLMConfig(**values)
