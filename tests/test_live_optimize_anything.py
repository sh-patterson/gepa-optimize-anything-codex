from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_AGENT_SMOKE") != "1",
    reason="requires paid GEPA and Codex calls",
)
def test_autoresearch_improves_a_deterministic_text_candidate(tmp_path: Path):
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    def evaluate(candidate: str, _example: object) -> tuple[float, dict]:
        passed = "BLUE" in candidate
        return float(passed), {
            "passed": passed,
            "feedback": (
                "The candidate must contain the exact uppercase token BLUE."
                if not passed
                else "The candidate contains BLUE."
            ),
        }

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=evaluate,
        dataset=[{"case": "exact-token"}],
        objective="Write a candidate that passes the deterministic evaluator.",
        background="The evaluator feedback states the exact missing requirement.",
        config=OptimizeAnythingConfig(
            engine="autoresearch",
            name="codex-adapter-live-smoke",
            max_evals=10,
            max_token_cost=0.20,
            stop_at_score=1.0,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={
                "model": "gpt-5.6-luna",
                "ralph": False,
            },
        ),
    )

    assert result.best_score == 1.0
    assert "BLUE" in result.best_candidate
    assert result.total_evals >= 2
    assert result.metadata["adapter_cost"] > 0
