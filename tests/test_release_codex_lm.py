from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_codex_lm.py"
SPEC = importlib.util.spec_from_file_location("release_codex_lm_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
codex_lm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_lm
SPEC.loader.exec_module(codex_lm)


def test_codex_lm_records_adapter_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> object:
        session_id = command[command.index("--session-id") + 1]
        assert command[-1] == "improve this"
        assert kwargs["env"] == {"SAFE": "1"}
        return type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "is_error": False,
                        "session_id": session_id,
                        "adapter_target_model": codex_lm.TARGET_MODEL,
                        "result": "improved",
                        "usage": {"input_tokens": 12, "output_tokens": 8},
                        "total_cost_usd": 0.001,
                    }
                ),
            },
        )()

    monkeypatch.setattr(codex_lm.subprocess, "run", fake_run)
    lm = codex_lm.CodexLM(
        codex_lm.TARGET_MODEL,
        launcher=tmp_path / "claude",
        cwd=tmp_path,
        environment={"SAFE": "1"},
    )

    assert lm("improve this") == "improved"
    assert lm.invocation_count == 1
    assert lm.total_tokens_in == 12
    assert lm.total_tokens_out == 8
    assert lm.total_cost == pytest.approx(0.001)


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"is_error": True},
        {
            "is_error": False,
            "session_id": "wrong",
            "adapter_target_model": codex_lm.TARGET_MODEL,
            "result": "improved",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.001,
        },
    ),
)
def test_codex_lm_fails_closed_on_invalid_adapter_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(
        codex_lm.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(payload)},
        )(),
    )
    lm = codex_lm.CodexLM(
        codex_lm.TARGET_MODEL,
        launcher=tmp_path / "claude",
        cwd=tmp_path,
        environment={},
    )

    with pytest.raises(RuntimeError):
        lm("improve this")


def test_codex_lm_rejects_litellm_only_options(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sampling options"):
        codex_lm.CodexLM(
            codex_lm.TARGET_MODEL,
            launcher=tmp_path / "claude",
            cwd=tmp_path,
            environment={},
            temperature=0.7,
        )
