from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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

import al_canary_adapter as al  # noqa: E402


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    transcript = tmp_path / "transcript.json"
    frame = tmp_path / "frame.jpg"
    transcript.write_text('{"text":"retained"}', encoding="utf-8")
    frame.write_bytes(b"frozen-frame")

    def custody(path: Path) -> dict[str, object]:
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    cases: list[dict[str, object]] = []
    for index in range(48):
        split = "development" if index < 28 else "validation"
        if split == "development":
            family_index = index % 21
        else:
            family_index = 21 + ((index - 28) % 16)
        cases.append(
            {
                "case_id": f"case-{index:02}",
                "split": split,
                "model_input": {
                    "allowed_decisions": [
                        "political_ad",
                        "not_political_ad",
                        "abstain",
                    ],
                    "sources": [
                        {
                            "transcript": custody(transcript),
                            "sparse_frame_derivative": custody(frame),
                        }
                    ],
                },
                "sealed_truth": {
                    "class": "political_ad" if index % 2 else "not_political_ad",
                    "creative_family_id": f"family-{family_index:02}",
                },
            }
        )
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8"
    )
    return tmp_path, hashlib.sha256(cases_path.read_bytes()).hexdigest()


def test_fixture_and_fake_runner_preserve_sealed_truth_boundary(tmp_path: Path) -> None:
    root, cases_hash = _fixture(tmp_path)
    fixture = al.load_fixture(root, cases_sha256=cases_hash)
    seen_inputs: list[dict[str, object]] = []

    class FakeRunner:
        def run_case(
            self, prompt: str, model_input: dict[str, object]
        ) -> dict[str, object]:
            assert prompt == "recognition-v2"
            assert "sealed_truth" not in model_input
            seen_inputs.append(model_input)
            return {
                "decision": "abstain",
                "receipt": {
                    "calls": 0,
                    "cost_usd": 0,
                    "latency_ms": 0,
                    "frames_seen": 0,
                },
            }

    def evaluator(
        cases: list[dict[str, object]],
        predictions: list[dict[str, object]],
        *,
        max_cost_per_case_usd: float,
    ) -> object:
        assert len(cases) == len(predictions) == 20
        assert max_cost_per_case_usd == 0.05
        return SimpleNamespace(
            score=0.25,
            metrics={"gates_pass": False},
            feedback={
                "aggregate_only": True,
                "sealed_case_details_disclosed": False,
            },
        )

    result = al.run_canary(
        "recognition-v2",
        fixture,
        FakeRunner(),
        evaluator,
        split="validation",
        max_cost_per_case_usd=0.05,
    )

    assert len(seen_inputs) == 20
    assert result.score == 0.25
    assert result.cases_sha256 == cases_hash


def test_fixture_rejects_changed_custody_artifact(tmp_path: Path) -> None:
    root, cases_hash = _fixture(tmp_path)
    (tmp_path / "frame.jpg").write_bytes(b"changed")

    with pytest.raises(ValueError, match="hash mismatch"):
        al.load_fixture(root, cases_sha256=cases_hash)


def test_canary_reloads_frozen_cases_before_run(tmp_path: Path) -> None:
    root, cases_hash = _fixture(tmp_path)
    fixture = al.load_fixture(root, cases_sha256=cases_hash)
    fixture.cases[28]["sealed_truth"]["class"] = "tampered"

    class Runner:
        def run_case(
            self, prompt: str, model_input: dict[str, object]
        ) -> dict[str, object]:
            del prompt, model_input
            return {"decision": "abstain"}

    def evaluator(
        cases: list[dict[str, object]],
        predictions: list[dict[str, object]],
        **kwargs: object,
    ) -> object:
        del predictions, kwargs
        assert cases[0]["sealed_truth"]["class"] != "tampered"
        return SimpleNamespace(
            score=0,
            metrics={"gates_pass": False},
            feedback={
                "aggregate_only": True,
                "sealed_case_details_disclosed": False,
            },
        )

    al.run_canary(
        "recognize",
        fixture,
        Runner(),
        evaluator,
        split="validation",
        max_cost_per_case_usd=0.05,
    )


def test_canary_rejects_sealed_truth_in_evaluator_output(tmp_path: Path) -> None:
    root, cases_hash = _fixture(tmp_path)
    fixture = al.load_fixture(root, cases_sha256=cases_hash)

    class Runner:
        def run_case(
            self, prompt: str, model_input: dict[str, object]
        ) -> dict[str, object]:
            del prompt, model_input
            return {"decision": "abstain"}

    def evaluator(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            score=0,
            metrics={"gates_pass": False, "family-01": "political_ad"},
            feedback={
                "aggregate_only": True,
                "sealed_case_details_disclosed": False,
            },
        )

    with pytest.raises(ValueError, match="non-aggregate keys"):
        al.run_canary(
            "recognize",
            fixture,
            Runner(),
            evaluator,
            split="validation",
            max_cost_per_case_usd=0.05,
        )


def test_canary_rejects_sealed_truth_in_allowed_scalar_field(tmp_path: Path) -> None:
    root, cases_hash = _fixture(tmp_path)
    fixture = al.load_fixture(root, cases_sha256=cases_hash)

    class Runner:
        def run_case(
            self, prompt: str, model_input: dict[str, object]
        ) -> dict[str, object]:
            del prompt, model_input
            return {"decision": "abstain"}

    def evaluator(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            score=0,
            metrics={"gates_pass": "family-01: political_ad"},
            feedback={
                "aggregate_only": True,
                "sealed_case_details_disclosed": False,
            },
        )

    with pytest.raises(ValueError, match="must be boolean"):
        al.run_canary(
            "recognize",
            fixture,
            Runner(),
            evaluator,
            split="validation",
            max_cost_per_case_usd=0.05,
        )


def test_codex_runner_uses_read_only_runtime_and_preserves_unknown_cost(
    tmp_path: Path,
) -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.spec = None

        def invoke(self, spec: object) -> object:
            self.spec = spec
            return SimpleNamespace(
                text=json.dumps(
                    {
                        "decision": "abstain",
                        "attribution": None,
                        "evidence_refs": [],
                        "unsupported_claims": [],
                    }
                ),
                cost_status="unknown",
                provider="app_server",
                status="completed",
                model="gpt-5.6-luna",
                reasoning_effort="high",
                runtime_version="0.144.4",
                auth_mode="chatgpt",
                usage=SimpleNamespace(
                    input_tokens=2,
                    cached_input_tokens=1,
                    output_tokens=3,
                    reasoning_output_tokens=1,
                    total_tokens=5,
                ),
                thread_id="thread-1",
                turn_id="turn-1",
            )

    runtime = FakeRuntime()
    runner = al.CodexRuntimeRunner(runtime, cwd=tmp_path)
    prediction = runner.run_case(
        "recognize",
        {"sources": [{"sparse_frame_derivative": {"path": "opaque"}}]},
    )

    assert runtime.spec.sandbox == "read-only"
    assert runtime.spec.model == "gpt-5.6-luna"
    assert runtime.spec.reasoning_effort == "high"
    attribution_schema = runtime.spec.output_schema["properties"]["attribution"]
    assert attribution_schema["anyOf"][0]["additionalProperties"] is False
    assert prediction["receipt"]["cost_usd"] is None
    assert prediction["receipt"]["cost_status"] == "unpriced_codex"
    assert prediction["receipt"]["observed_model"] == "gpt-5.6-luna"
    assert prediction["receipt"]["tokens"]["total"] == 5


def test_codex_runner_preserves_interrupted_terminal_status(tmp_path: Path) -> None:
    class InterruptedRuntime:
        def invoke(self, spec: object) -> object:
            del spec
            return SimpleNamespace(status="interrupted")

    runner = al.CodexRuntimeRunner(InterruptedRuntime(), cwd=tmp_path)

    with pytest.raises(RuntimeError, match="terminal status interrupted"):
        runner.run_case("recognize", {"sources": []})
