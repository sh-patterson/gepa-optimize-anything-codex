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
    assert prediction["receipt"]["cost_usd"] is None
    assert prediction["receipt"]["cost_status"] == "unknown"
