from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

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

import canary_contract as canary  # noqa: E402


def _manifest(tmp_path: Path) -> Path:
    cases = tmp_path / "cases"
    cases.mkdir()
    development = cases / "development.json"
    validation = cases / "validation.json"
    development.write_text('{"frozen":true}', encoding="utf-8")
    validation.write_text('{"frozen":true}', encoding="utf-8")

    def artifact(path: Path) -> dict[str, str]:
        return {
            "role": "admission_input",
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    manifest = tmp_path / "suite.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suite_id": "al-frozen-48",
                "positive_label": "candidate",
                "cases": [
                    {
                        "case_id": "development-1",
                        "family_id": "family-a",
                        "split": "development",
                        "expected_label": "candidate",
                        "artifacts": [artifact(development)],
                        "payload": {"retained": True},
                    },
                    {
                        "case_id": "validation-1",
                        "family_id": "family-b",
                        "split": "validation",
                        "expected_label": "abstain",
                        "artifacts": [artifact(validation)],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_fake_runner_exercises_contract_without_provider(tmp_path: Path) -> None:
    suite = canary.load_suite(_manifest(tmp_path))

    class FakeRunner:
        def run_case(
            self, candidate: str, case: canary.CaseInput
        ) -> canary.CaseObservation:
            assert candidate == "prompt-v2"
            assert not hasattr(case, "expected_label")
            assert not hasattr(case, "family_id")
            return canary.CaseObservation(
                case_id=case.case_id,
                predicted_label="candidate",
                provider_cost_usd=None,
                human_review_units=1,
            )

    class ExactEvaluator:
        def evaluate(
            self,
            candidate: str,
            case: canary.CanaryCase,
            observation: canary.CaseObservation,
        ) -> canary.CaseScore:
            del candidate
            exact = observation.predicted_label == case.expected_label
            return canary.CaseScore(float(exact), "exact" if exact else "wrong")

    result = canary.run_suite(
        "prompt-v2", suite, FakeRunner(), ExactEvaluator(), split="validation"
    )

    assert result.mean_score == 0
    assert result.false_positives == 1
    assert result.false_negatives == 0
    assert result.known_provider_cost_usd == 0
    assert result.unknown_provider_cost_cases == 1
    assert result.human_review_units == 1


def test_loader_rejects_artifact_mutation(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / "cases" / "development.json").write_text(
        '{"frozen":false}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="artifact hash mismatch"):
        canary.load_suite(manifest)


def test_loader_rejects_family_leakage_across_splits(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["cases"][1]["family_id"] = "family-a"
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="crosses development and validation"):
        canary.load_suite(manifest)


def test_runner_cannot_substitute_case_identity(tmp_path: Path) -> None:
    suite = canary.load_suite(_manifest(tmp_path))

    class WrongRunner:
        def run_case(
            self, candidate: str, case: canary.CaseInput
        ) -> canary.CaseObservation:
            del candidate, case
            return canary.CaseObservation("other-case", "abstain")

    class UnusedEvaluator:
        def evaluate(self, *args: object) -> canary.CaseScore:
            raise AssertionError("evaluator must not receive substituted evidence")

    with pytest.raises(ValueError, match="runner returned other-case"):
        canary.run_suite(
            "prompt-v2", suite, WrongRunner(), UnusedEvaluator(), split="validation"
        )


@pytest.mark.parametrize("target", ["manifest", "artifact"])
def test_run_rechecks_custody_after_loading(tmp_path: Path, target: str) -> None:
    manifest = _manifest(tmp_path)
    suite = canary.load_suite(manifest)
    if target == "manifest":
        manifest.write_text("{}", encoding="utf-8")
    else:
        (tmp_path / "cases" / "validation.json").write_text(
            '{"frozen":false}', encoding="utf-8"
        )

    with pytest.raises(ValueError, match="changed|hash mismatch"):
        canary.run_suite("prompt-v2", suite, object(), object(), split="validation")


def test_runner_receives_payload_copy(tmp_path: Path) -> None:
    suite = canary.load_suite(_manifest(tmp_path))

    class MutatingRunner:
        def run_case(
            self, candidate: str, case: canary.CaseInput
        ) -> canary.CaseObservation:
            del candidate
            case.payload["injected"] = True
            return canary.CaseObservation(case.case_id, "abstain")

    class Evaluator:
        def evaluate(
            self,
            candidate: str,
            case: canary.CanaryCase,
            observation: canary.CaseObservation,
        ) -> canary.CaseScore:
            del candidate, observation
            assert "injected" not in case.payload
            return canary.CaseScore(1, "sealed")

    canary.run_suite(
        "prompt-v2", suite, MutatingRunner(), Evaluator(), split="validation"
    )
