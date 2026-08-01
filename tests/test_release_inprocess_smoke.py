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

FULL_USAGE = {
    "input_tokens": 12,
    "output_tokens": 8,
    "cached_input_tokens": 7,
    "cache_write_input_tokens": 0,
    "reasoning_output_tokens": 3,
}


def _installed_skill(tmp_path: Path) -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version":"1.0.1"}\n', encoding="utf-8")
    scripts = skill / "scripts"
    scripts.mkdir()
    (scripts / "codex_claude_adapter.py").write_text("# adapter\n", encoding="utf-8")
    (scripts / "codex_lm.py").write_text("# public adapter\n", encoding="utf-8")
    (scripts / "claude").write_text("# launcher\n", encoding="utf-8")
    return skill


class _Result:
    best_candidate = "Return BLUE."
    best_score = 1.0
    total_evals = 4
    metadata = {"usage": FULL_USAGE}


def _evidence() -> object:
    return sys.modules["release_evidence"].AdapterEvidence(
        invocation_paths=(),
        session_paths=(),
        usage=FULL_USAGE,
        estimated_cost_usd=0.001,
        mappings=(
            {
                "upstream_session_id": "test",
                "codex_thread_id": "test",
                "resume": False,
                "cost_status": "standard_tier_upper_estimate_from_observed_usage",
                "terminal_status": "completed",
                "return_code": 0,
            },
        ),
    )


def _provenance(skill: Path, *_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "installed_plugin": True,
        "skill_path": "installed-skill",
        "plugin_manifest": str(skill.parents[1] / ".codex-plugin" / "plugin.json"),
        "plugin_version": "1.0.1",
        "repository_commit": "a" * 40,
        "gepa_commit": "b" * 40,
    }


def test_config_is_bounded_and_uses_codex_model(tmp_path: Path) -> None:
    from gepa.oa.engines.best_of_n import BestOfNEngine
    from gepa.oa.engines.gepa import GepaEngine
    from gepa.optimize_anything import OptimizeAnythingConfig

    gepa_config = smoke.config_for("gepa", tmp_path)
    best_config = smoke.config_for("best_of_n", tmp_path)

    assert gepa_config["max_evals"] == 10
    assert gepa_config["stop_at_score"] is None
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
    lm = type("LM", (), {"total_usage": FULL_USAGE})()

    assert smoke._codex_usage([lm]) == FULL_USAGE
    with pytest.raises(RuntimeError, match="usage is empty"):
        smoke._codex_usage([])


@pytest.mark.parametrize("engine", ("gepa", "best_of_n"))
def test_runner_injects_codex_driver_into_inprocess_engines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    instances: list[object] = []

    class FakeCodexLM:
        def __init__(self, _config: object, **_kwargs: object) -> None:
            self.total_tokens_in = 10
            self.total_tokens_out = 2
            self.total_usage = {
                "input_tokens": 10,
                "output_tokens": 2,
                "cached_input_tokens": 4,
                "cache_write_input_tokens": 0,
                "reasoning_output_tokens": 1,
            }
            self.total_cost = 0.001
            self.invocation_count = 0
            instances.append(self)

        def __call__(self, prompt: str) -> str:
            assert prompt
            self.invocation_count += 1
            return "```\nReturn BLUE.\n```" if engine == "best_of_n" else "Return BLUE."

    runtime = {
        "driver": type("Driver", (), {"CodexLMConfig": staticmethod(lambda **values: values), "CodexLM": FakeCodexLM}),
        "launcher": tmp_path / "claude",
        "environment": {},
        "state_dir": tmp_path / "state",
        "work_dir": tmp_path,
        "probe_success": True,
    }

    result, usage, lms = smoke._run_codex(
        engine, smoke.config_for(engine, tmp_path), runtime, smoke.SEED
    )

    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 4,
        "cache_write_input_tokens": 0,
        "reasoning_output_tokens": 1,
    }
    assert lms == instances
    assert len(instances) == 1


def test_codex_runtime_rejects_api_keys_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")

    with pytest.raises(RuntimeError, match="cannot expose API keys"):
        smoke._prepare_codex_runtime(tmp_path / "skill", tmp_path / "work")


def test_codex_runtime_loads_installed_driver_and_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    loaded_paths: list[Path] = []
    installed_adapter = skill / "scripts" / "codex_claude_adapter.py"

    class Runtime:
        @staticmethod
        def runtime_paths() -> object:
            return object()

        @staticmethod
        def stage_runtime(paths: object) -> object:
            return paths

        @staticmethod
        def resolve_state_dir(_paths: object, state_dir: Path) -> Path:
            return state_dir

        @staticmethod
        def probe_runtime(_paths: object, _state_dir: Path) -> object:
            return type("Probe", (), {"returncode": 0})()

        @staticmethod
        def runtime_environment(_paths: object, _state_dir: Path) -> dict[str, str]:
            return {"SAFE": "1"}

    paths = type(
        "Paths",
        (),
        {"launcher": skill / "scripts" / "claude", "adapter_module": installed_adapter},
    )()
    Runtime.stage_runtime = staticmethod(lambda _paths: paths)

    def fake_load(path: Path, _name: str) -> object:
        loaded_paths.append(path)
        if path == skill / "scripts" / "sandbox_runtime.py":
            return Runtime()
        assert path == skill / "scripts" / "codex_lm.py"
        return type("Driver", (), {"CodexLM": object})()

    monkeypatch.setattr(smoke, "_load_script", fake_load)

    runtime = smoke._prepare_codex_runtime(skill, tmp_path / "work")

    assert loaded_paths == [
        skill / "scripts" / "sandbox_runtime.py",
        skill / "scripts" / "codex_lm.py",
    ]
    assert runtime["adapter"] == installed_adapter


def test_smoke_requires_installed_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEPA_CODEX_SKILL_DIR", raising=False)
    with pytest.raises(RuntimeError, match="GEPA_CODEX_SKILL_DIR"):
        smoke.run_smoke("gepa", tmp_path / "output")


def test_smoke_requires_the_installed_public_codex_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    (skill / "scripts" / "codex_lm.py").unlink()
    with pytest.raises(FileNotFoundError):
        smoke._load_script(skill / "scripts" / "codex_lm.py", "missing_public_driver")


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
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    monkeypatch.setattr(smoke, "read_adapter_evidence", lambda *_args, **_kwargs: _evidence())
    monkeypatch.setattr(smoke, "installed_provenance", _provenance)
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
    assert receipt["usage"] == FULL_USAGE
    assert receipt["duration_ms"] >= 0
    assert receipt["provenance"]["installed_plugin"] is True
    assert receipt["receipt_path"] == f"$HOME/output-{engine}/{engine}-receipt.json"
    assert all(
        mapping["upstream_session_id"].startswith("sha256:")
        and mapping["codex_thread_id"].startswith("sha256:")
        for mapping in receipt["session_mapping"]
    )
    assert set(receipt["hashes"]) == {
        "skill",
        "plugin_manifest",
        "runner",
        "codex_lm",
        "adapter",
        "contract",
    }
    adapter = (
        tmp_path
        / "installed"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
        / "scripts"
        / "codex_claude_adapter.py"
    )
    assert receipt["hashes"]["codex_lm"] == smoke.hash_files(
        {"codex_lm": adapter.parent / "codex_lm.py"}
    )["codex_lm"]
    assert receipt["hashes"]["adapter"] == smoke.hash_files({"adapter": adapter})[
        "adapter"
    ]
    assert "Return BLUE." not in json.dumps(receipt)
    assert str(tmp_path) not in json.dumps(receipt)
    assert seen["seed_candidate"] == "Return RED."
    config = seen["config"]
    assert config.max_evals == 10
    assert config.stop_at_score is None


def test_smoke_rejects_mismatched_returned_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    actual = _evidence()
    monkeypatch.setattr(
        smoke,
        "read_adapter_evidence",
        lambda *_args, **_kwargs: sys.modules["release_evidence"].AdapterEvidence(
            invocation_paths=actual.invocation_paths,
            session_paths=actual.session_paths,
            usage={**actual.usage, "input_tokens": 13},
            estimated_cost_usd=actual.estimated_cost_usd,
            mappings=actual.mappings,
        ),
    )
    monkeypatch.setattr(smoke, "installed_provenance", _provenance)

    with pytest.raises(RuntimeError, match="journal usage is inconsistent"):
        smoke.run_smoke("gepa", tmp_path / "output", optimize=lambda **_: _Result())


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

    def matching_evidence(*_args: object, **_kwargs: object) -> object:
        metadata = getattr(result, "metadata", {})
        usage = metadata.get("usage", {}) if isinstance(metadata, dict) else {}
        actual = _evidence()
        return sys.modules["release_evidence"].AdapterEvidence(
            invocation_paths=actual.invocation_paths,
            session_paths=actual.session_paths,
            usage=usage,
            estimated_cost_usd=actual.estimated_cost_usd,
            mappings=actual.mappings,
        )

    monkeypatch.setattr(smoke, "read_adapter_evidence", matching_evidence)
    monkeypatch.setattr(smoke, "installed_provenance", _provenance)
    with pytest.raises(RuntimeError, match=error):
        smoke.run_smoke("gepa", tmp_path / "output", optimize=lambda **_: result)
