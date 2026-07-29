from __future__ import annotations

from pathlib import Path

import gepa.oa.engines.best_of_n as best_of_n
from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything


def _evaluate(candidate: str, _example: object = None) -> tuple[float, dict[str, str]]:
    passed = "BLUE" in candidate
    return float(passed), {"feedback": "Candidate must contain BLUE."}


class _ReflectionLM:
    total_cost = 0.0
    total_tokens_in = 0
    total_tokens_out = 0

    def __init__(self) -> None:
        self.called = False

    def __call__(self, prompt: str) -> str:
        self.called = True
        assert "Candidate must contain BLUE." in prompt
        return "Return BLUE."


def test_gepa_inprocess_engine_accepts_a_no_network_lm(tmp_path: Path) -> None:
    lm = _ReflectionLM()

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=_evaluate,
        objective="Return a candidate containing BLUE.",
        config=OptimizeAnythingConfig(
            engine="gepa",
            max_evals=4,
            stop_at_score=1.0,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={
                "reflection": {"reflection_lm": lm},
                "engine": {"seed": 0},
            },
        ),
    )

    assert lm.called
    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert result.total_evals == 4
    assert result.metadata["adapter_cost"] == 0.0


def test_best_of_n_inprocess_engine_accepts_a_no_network_lm(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    candidates: list[str] = []

    class FakeLM:
        total_cost = 0.0

        def __init__(self, model: str, **_kwargs: object) -> None:
            calls.append(model)
            self.outputs = iter(("Return RED.", "Return GREEN.", "Return BLUE."))

        def __call__(self, prompt: str) -> str:
            calls.append(prompt)
            output = f"```\n{next(self.outputs)}\n```"
            candidates.append(output)
            return output

    monkeypatch.setattr(best_of_n, "LM", FakeLM)

    result = optimize_anything(
        seed_candidate="Return RED.",
        evaluator=_evaluate,
        objective="Return a candidate containing BLUE.",
        config=OptimizeAnythingConfig(
            engine="best_of_n",
            max_evals=3,
            stop_at_score=1.0,
            run_dir=str(tmp_path / "run"),
            output_dir=str(tmp_path / "output"),
            engine_config={"model": "fake/no-network", "max_n": 3},
        ),
    )

    assert calls[0] == "fake/no-network"
    assert len(calls) == 4
    assert result.best_candidate == "Return BLUE."
    assert result.best_score == 1.0
    assert result.total_evals == 3
    assert result.metadata["adapter_cost"] == 0.0
    assert result.metadata["n_samples"] == 3
    assert result.metadata["n_parse_failures"] == 0
    assert len(set(candidates)) == 3
