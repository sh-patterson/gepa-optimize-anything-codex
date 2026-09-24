from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from gepa.optimize_anything import OptimizeAnythingConfig


SCRIPTS = (
    Path(__file__).parents[1]
    / "plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/scripts"
)
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("codex_gepa", SCRIPTS / "codex_gepa.py")
assert SPEC is not None and SPEC.loader is not None
codex_gepa = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_gepa
SPEC.loader.exec_module(codex_gepa)


def test_default_gepa_uses_codex_reflection_and_preserves_feedback(monkeypatch, tmp_path):
    prompts: list[str] = []

    class FakeCodexLM:
        total_cost = 0.0

        def __call__(self, prompt: str) -> str:
            prompts.append(prompt)
            assert "Candidate must contain BLUE" in prompt
            return "Return BLUE."

    lm = FakeCodexLM()
    monkeypatch.setattr(codex_gepa, "_new_codex_lm", lambda *_: (lm, tmp_path))

    def run_gepa(seed, *, evaluator, config, **_kwargs):
        assert seed == "Return RED."
        assert config.engine == "gepa"
        assert config.engine_config["reflection"]["reflection_lm"] is lm
        _, feedback = evaluator(seed)
        improved = lm(f"Candidate: {seed}\n{feedback['feedback']}")
        return SimpleNamespace(best_candidate=improved, best_score=evaluator(improved)[0])

    monkeypatch.setattr(codex_gepa, "optimize_anything", run_gepa)
    result = codex_gepa.optimize_with_codex(
        "Return RED.",
        evaluator=lambda candidate: (
            float("BLUE" in candidate),
            {"feedback": "Candidate must contain BLUE"},
        ),
    )
    assert result.result.best_candidate == "Return BLUE."
    assert result.result.best_score == 1.0
    assert result.reflection_lm is lm
    assert result.state_root == tmp_path
    assert len(prompts) == 1


@pytest.mark.parametrize(
    "config,match",
    [
        (OptimizeAnythingConfig(engine="best_of_n"), "engine='gepa'"),
        (OptimizeAnythingConfig(engine="gepa", max_evals=None), "positive max_evals"),
        (OptimizeAnythingConfig(engine="gepa", max_token_cost=1.0), "enforceable USD"),
        (
            OptimizeAnythingConfig(
                engine="gepa",
                engine_config={"reflection": {"reflection_lm": "openai/gpt-5.1"}},
            ),
            "another reflection_lm",
        ),
        (
            OptimizeAnythingConfig(
                engine="gepa",
                engine_config={"reflection": {"reflection_strategy": object()}},
            ),
            "reflection_strategy",
        ),
    ],
)
def test_provider_or_unbounded_configuration_fails_before_codex_starts(
    monkeypatch, config, match
):
    monkeypatch.setattr(
        codex_gepa,
        "_new_codex_lm",
        lambda *_: pytest.fail("Codex started before validation"),
    )
    with pytest.raises(ValueError, match=match):
        codex_gepa.optimize_with_codex("seed", evaluator=lambda _: 0.0, config=config)


def test_reflection_call_limit_must_be_a_positive_integer(monkeypatch):
    monkeypatch.setattr(
        codex_gepa,
        "_new_codex_lm",
        lambda *_: pytest.fail("Codex started with an invalid limit"),
    )
    for limit in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="positive integer"):
            codex_gepa.optimize_with_codex(
                "seed", evaluator=lambda _: 0.0, max_reflection_calls=limit
            )
