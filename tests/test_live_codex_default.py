from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from gepa.optimize_anything import OptimizeAnythingConfig


ROOT = Path(__file__).parents[1]


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_LIVE") != "1", reason="requires one live Codex call"
)
def test_installed_default_reflection_improves_from_evaluator_feedback() -> None:
    skill = Path(os.environ["GEPA_CODEX_SKILL_DIR"]).resolve()
    assert ROOT.resolve() not in skill.parents
    scripts = skill / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("codex_gepa_live", scripts / "codex_gepa.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    def evaluate(candidate: str) -> tuple[float, dict[str, str]]:
        return float("BLUE" in candidate), {
            "feedback": "Replace RED with BLUE. A passing candidate must contain BLUE."
        }

    run = module.optimize_with_codex(
        "Return RED.",
        evaluator=evaluate,
        objective="Write a candidate that contains BLUE.",
        config=OptimizeAnythingConfig(
            engine="gepa",
            max_evals=4,
            stop_at_score=1.0,
            engine_config={"engine": {"max_candidate_proposals": 1}},
        ),
        max_reflection_calls=1,
    )
    assert run.result.best_score == 1.0
    assert "BLUE" in run.result.best_candidate
    assert run.reflection_lm.invocation_count == 1
    assert run.reflection_lm.last_result is not None
    assert run.reflection_lm.last_result.status == "completed"
    assert run.reflection_lm.last_result.usage["input_tokens"] > 0
    assert run.reflection_lm.last_result.session_mapping_path.is_file()
    assert run.state_root.is_dir()
