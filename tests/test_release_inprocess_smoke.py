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
    manifest.write_text('{"version":"0.3.1"}\n', encoding="utf-8")
    return skill


class _Result:
    best_candidate = "Return BLUE."
    best_score = 1.0
    total_evals = 4
    metadata = {"usage": {"input_tokens": 12, "output_tokens": 8}}


def test_config_is_bounded_and_uses_exact_model(tmp_path: Path) -> None:
    from gepa.oa.engines.best_of_n import BestOfNEngine
    from gepa.oa.engines.gepa import GepaEngine
    from gepa.optimize_anything import OptimizeAnythingConfig

    gepa_config = smoke.config_for("gepa", tmp_path)
    best_config = smoke.config_for("best_of_n", tmp_path)

    assert gepa_config["max_evals"] == 4
    assert gepa_config["stop_at_score"] == 1.0
    assert gepa_config["engine_config"]["reflection"]["reflection_lm"] == smoke.MODEL
    assert gepa_config["engine_config"]["reflection"]["reflection_lm_kwargs"] == {
        "num_retries": 0
    }
    assert best_config["engine_config"] == {
        "model": smoke.MODEL,
        "max_n": 3,
        "lm_kwargs": {"num_retries": 0},
    }
    GepaEngine(OptimizeAnythingConfig(**gepa_config))
    BestOfNEngine(OptimizeAnythingConfig(**best_config))


def test_native_usage_requires_positive_model_tokens() -> None:
    lm = type("LM", (), {"total_tokens_in": 12, "total_tokens_out": 8})()

    assert smoke._native_usage([lm]) == {
        "input_tokens": 12,
        "output_tokens": 8,
    }
    with pytest.raises(RuntimeError, match="usage is empty"):
        smoke._native_usage([])


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


def test_smoke_requires_platform_key_and_installed_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        smoke.run_smoke("gepa", tmp_path / "output")

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    monkeypatch.setattr(smoke, "_installed_vcs_commit", lambda _package: "c" * 40)
    seen: dict[str, object] = {}

    def fake_optimize(**kwargs: object) -> _Result:
        seen.update(kwargs)
        return _Result()

    receipt = smoke.run_smoke(engine, tmp_path / f"output-{engine}", optimize=fake_optimize)
    assert receipt["status"] == "success"
    assert receipt["provider"] == "openai"
    assert receipt["model"] == "openai/gpt-5.1"
    assert receipt["retry_count"] == 0
    assert receipt["result"]["eval_count"] == 4
    assert receipt["usage"] == {"input_tokens": 12, "output_tokens": 8}
    assert receipt["duration_ms"] >= 0
    assert receipt["provenance"]["installed_plugin"] is True
    assert set(receipt["hashes"]) == {"skill", "plugin_manifest", "runner"}
    assert "Return BLUE." not in json.dumps(receipt)
    assert "test-key" not in json.dumps(receipt)
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
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(_installed_skill(tmp_path)))
    with pytest.raises(RuntimeError, match=error):
        smoke.run_smoke("gepa", tmp_path / "output", optimize=lambda **_: result)
