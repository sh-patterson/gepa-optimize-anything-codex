from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_inprocess_smoke.py"
SPEC = importlib.util.spec_from_file_location("release_inprocess_smoke_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def _installed_skill(tmp_path: Path) -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version":"0.3.2"}\n', encoding="utf-8")
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "codex_lm.py").write_text("# driver\n", encoding="utf-8")
    (scripts / "codex_claude_adapter.py").write_text("# adapter\n", encoding="utf-8")
    (scripts / "claude").write_text("# launcher\n", encoding="utf-8")
    return skill


class _Result:
    best_candidate = "Return BLUE."
    best_score = 1.0
    total_evals = 4
    metadata = {"usage": {"input_tokens": 12, "output_tokens": 8}}


def test_config_is_bounded_and_uses_codex_model(tmp_path: Path) -> None:
    from gepa.oa.engines.best_of_n import BestOfNEngine
    from gepa.oa.engines.gepa import GepaEngine
    from gepa.optimize_anything import OptimizeAnythingConfig

    gepa_config = smoke.config_for("gepa", tmp_path)
    best_config = smoke.config_for("best_of_n", tmp_path)

    assert gepa_config["max_evals"] == 4
    assert gepa_config["stop_at_score"] == 1.0
    assert gepa_config["engine_config"]["reflection"]["reflection_lm"] == smoke.MODEL
    assert gepa_config["engine_config"]["reflection"]["reflection_lm_kwargs"] == {}
    assert best_config["engine_config"] == {
        "model": smoke.MODEL,
        "max_n": 3,
        "lm_kwargs": {},
    }
    GepaEngine(OptimizeAnythingConfig(**gepa_config))
    BestOfNEngine(OptimizeAnythingConfig(**best_config))


def test_codex_usage_requires_positive_model_tokens() -> None:
    lm = type("LM", (), {"total_tokens_in": 12, "total_tokens_out": 8})()

    assert smoke._codex_usage([lm]) == {
        "input_tokens": 12,
        "output_tokens": 8,
    }
    with pytest.raises(RuntimeError, match="usage is empty"):
        smoke._codex_usage([])


def test_journal_evidence_hashes_invocation_and_session_records(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    invocations = state / "invocations"
    sessions = state / "sessions"
    invocations.mkdir(parents=True)
    sessions.mkdir()
    invocation = {
        "terminal_status": "completed",
        "return_code": 0,
        "target_model": smoke.MODEL,
        "reasoning_effort": smoke.REASONING_EFFORT,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "estimated_cost_usd": 0.001,
        "upstream_session_id": "session",
        "codex_thread_id": "thread",
    }
    invocation_path = invocations / "one.json"
    invocation_path.write_text(json.dumps(invocation), encoding="utf-8")
    session_path = sessions / "one.json"
    session_path.write_text(
        json.dumps({"upstream_session_id": "session", "thread_id": "thread"}),
        encoding="utf-8",
    )

    evidence = smoke._journal_evidence(
        state, 1, {"input_tokens": 10, "output_tokens": 2}
    )

    assert evidence["evidence_files"] == {
        "invocation_1": invocation_path,
        "session_1": session_path,
    }


@pytest.mark.parametrize("engine", ("gepa", "best_of_n"))
def test_runner_injects_codex_driver_into_inprocess_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    instances: list[object] = []

    class FakeCodexLM:
        def __init__(self, model: str, **_kwargs: object) -> None:
            assert model == smoke.MODEL
            self.total_tokens_in = 10
            self.total_tokens_out = 2
            self.total_cost = 0.001
            self.invocation_count = 0
            instances.append(self)

        def __call__(self, prompt: str) -> str:
            assert prompt
            self.invocation_count += 1
            return "```\nReturn BLUE.\n```" if engine == "best_of_n" else "Return BLUE."

    monkeypatch.setattr(
        smoke,
        "_journal_evidence",
        lambda _state, count, usage: {
            "invocation_count": count,
            "estimated_cost_usd": 0.001,
            "session_mapping": [
                {"upstream_session_id": "session", "codex_thread_id": "thread"}
            ],
        },
    )
    runtime = {
        "driver": FakeCodexLM,
        "launcher": tmp_path / "claude",
        "environment": {},
        "state_dir": tmp_path / "state",
        "work_dir": tmp_path,
        "probe_success": True,
    }

    result, usage, evidence = smoke._run_codex(
        engine, smoke.config_for(engine, tmp_path), runtime
    )

    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert usage == {"input_tokens": 10, "output_tokens": 2}
    assert evidence["invocation_count"] == 1
    assert evidence["probe_success"] is True
    assert len(instances) == 1


def test_codex_runtime_rejects_api_keys_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")

    with pytest.raises(RuntimeError, match="cannot expose API keys"):
        smoke._prepare_codex_runtime(tmp_path / "skill", tmp_path / "work")


def test_installed_gepa_commit_comes_from_direct_url_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = "b" * 40

    class Distribution:
        @staticmethod
        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"vcs_info": {"commit_id": commit}})

    monkeypatch.setattr(
        smoke.importlib.metadata,
        "distribution",
        lambda name: Distribution(),
    )

    assert smoke._installed_vcs_commit("gepa") == commit


def test_release_commit_uses_exported_archive_metadata(tmp_path: Path) -> None:
    commit = "a" * 40
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "COMMIT").write_text(commit, encoding="utf-8")

    assert smoke._git_commit(tmp_path) == commit


def test_smoke_requires_installed_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEPA_CODEX_SKILL_DIR", raising=False)
    with pytest.raises(RuntimeError, match="GEPA_CODEX_SKILL_DIR"):
        smoke.run_smoke("gepa", tmp_path / "output")


def test_cli_requires_explicit_live_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("RUN_CODEX_LIVE", raising=False)

    assert (
        smoke.main(
            ["--engine", "gepa", "--output-dir", os.fspath(tmp_path / "output")]
        )
        == 2
    )
    assert "RUN_CODEX_LIVE=1" in capsys.readouterr().err


@pytest.mark.parametrize("engine", ("gepa", "best_of_n"))
def test_smoke_writes_sanitized_installed_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    monkeypatch.setattr(smoke, "_installed_vcs_commit", lambda _package: "c" * 40)
    seen: dict[str, object] = {}

    def fake_optimize(**kwargs: object) -> _Result:
        seen.update(kwargs)
        return _Result()

    receipt = smoke.run_smoke(engine, tmp_path / f"output-{engine}", optimize=fake_optimize)
    assert receipt["status"] == "success"
    assert receipt["schema_version"] == 2
    assert receipt["provider"] == "codex"
    assert receipt["model"] == "gpt-5.6-luna"
    assert receipt["retry_count"] == 0
    assert receipt["policy"]["pre_submission_retries"] == 0
    assert receipt["authentication"]["mode"] == "chatgpt_login"
    assert receipt["authentication"]["child_api_keys_present"] is False
    assert receipt["result"]["eval_count"] == 4
    assert receipt["usage"] == {"input_tokens": 12, "output_tokens": 8}
    assert receipt["duration_ms"] >= 0
    assert receipt["provenance"]["installed_plugin"] is True
    assert set(receipt["hashes"]) == {
        "skill",
        "plugin_manifest",
        "runner",
        "codex_lm",
        "adapter",
    }
    assert "Return BLUE." not in json.dumps(receipt)
    assert seen["seed_candidate"] == "Return RED."
    config = seen["config"]
    assert config.max_evals == 4
    assert config.stop_at_score == 1.0


@pytest.mark.parametrize(
    "result, error",
    [
        (
            type(
                "MissingUsage",
                (),
                {
                    "best_candidate": "Return BLUE.",
                    "best_score": 1.0,
                    "total_evals": 1,
                    "metadata": {},
                },
            )(),
            "usage",
        ),
        (
            type(
                "Incomplete",
                (),
                {
                    "best_candidate": "Return RED.",
                    "best_score": 0.0,
                    "total_evals": 1,
                    "metadata": {"usage": {"input_tokens": 1, "output_tokens": 1}},
                },
            )(),
            "RED to BLUE",
        ),
    ],
)
def test_smoke_fails_closed_for_incomplete_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object, error: str
) -> None:
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    with pytest.raises(RuntimeError, match=error):
        smoke.run_smoke("gepa", tmp_path / "output", optimize=lambda **_: result)
