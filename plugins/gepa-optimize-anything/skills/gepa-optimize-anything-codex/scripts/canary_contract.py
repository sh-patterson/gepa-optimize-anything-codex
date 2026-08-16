"""Frozen external-fixture contract for optimizer canaries.

Loading a suite verifies custody and split isolation before any runner is called.
The contract deliberately has no provider or optimizer dependency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

Split = Literal["development", "validation"]


@dataclass(frozen=True)
class ArtifactRef:
    path: Path
    sha256: str
    role: str


@dataclass(frozen=True)
class CanaryCase:
    case_id: str
    family_id: str
    split: Split
    expected_label: str
    artifacts: tuple[ArtifactRef, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class CanarySuite:
    suite_id: str
    positive_label: str
    cases: tuple[CanaryCase, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class CaseInput:
    """Runner-visible case data; deliberately excludes labels and family identity."""

    case_id: str
    artifacts: tuple[ArtifactRef, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class CaseObservation:
    case_id: str
    predicted_label: str
    provider_cost_usd: float | None = None
    human_review_units: int = 0
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.case_id or not self.predicted_label:
            raise ValueError("observation identifiers and label must be non-empty")
        if self.provider_cost_usd is not None and self.provider_cost_usd < 0:
            raise ValueError("provider_cost_usd cannot be negative")
        if self.human_review_units < 0:
            raise ValueError("human_review_units cannot be negative")


@dataclass(frozen=True)
class CaseScore:
    score: float
    feedback: str

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 1:
            raise ValueError("case score must be between 0 and 1")


@dataclass(frozen=True)
class SuiteResult:
    suite_id: str
    candidate: str
    mean_score: float
    false_positives: int
    false_negatives: int
    known_provider_cost_usd: float
    unknown_provider_cost_cases: int
    human_review_units: int
    case_scores: tuple[CaseScore, ...]


class CaseRunner(Protocol):
    def run_case(self, candidate: str, case: CaseInput) -> CaseObservation: ...


class CaseEvaluator(Protocol):
    def evaluate(
        self, candidate: str, case: CanaryCase, observation: CaseObservation
    ) -> CaseScore: ...


def load_suite(manifest_path: Path) -> CanarySuite:
    """Load and fully verify a suite before returning executable cases."""
    manifest_path = manifest_path.resolve()
    raw_bytes = manifest_path.read_bytes()
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid canary manifest JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("canary manifest schema_version must be 1")

    suite_id = _required_string(raw, "suite_id")
    positive_label = _required_string(raw, "positive_label")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("canary manifest cases must be a non-empty list")

    cases: list[CanaryCase] = []
    seen_case_ids: set[str] = set()
    family_splits: dict[str, Split] = {}
    root = manifest_path.parent
    for index, raw_case in enumerate(raw_cases):
        if not isinstance(raw_case, dict):
            raise ValueError(f"case {index} must be an object")
        case_id = _required_string(raw_case, "case_id")
        if case_id in seen_case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)
        family_id = _required_string(raw_case, "family_id")
        split = raw_case.get("split")
        if split not in ("development", "validation"):
            raise ValueError(f"case {case_id} has an invalid split")
        prior_split = family_splits.setdefault(family_id, split)
        if prior_split != split:
            raise ValueError(
                f"family {family_id} crosses development and validation splits"
            )
        expected_label = _required_string(raw_case, "expected_label")
        artifacts = _load_artifacts(root, case_id, raw_case.get("artifacts"))
        payload = raw_case.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError(f"case {case_id} payload must be an object")
        cases.append(
            CanaryCase(
                case_id=case_id,
                family_id=family_id,
                split=split,
                expected_label=expected_label,
                artifacts=artifacts,
                payload=payload,
            )
        )
    if {case.split for case in cases} != {"development", "validation"}:
        raise ValueError("suite must contain development and validation cases")
    return CanarySuite(
        suite_id=suite_id,
        positive_label=positive_label,
        cases=tuple(cases),
        manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def run_suite(
    candidate: str,
    suite: CanarySuite,
    runner: CaseRunner,
    evaluator: CaseEvaluator,
    *,
    split: Split,
) -> SuiteResult:
    """Run one frozen split through injected runner and evaluator seams."""
    if not candidate.strip():
        raise ValueError("candidate must be non-empty")
    selected = tuple(case for case in suite.cases if case.split == split)
    if not selected:
        raise ValueError(f"suite has no {split} cases")

    scores: list[CaseScore] = []
    false_positives = 0
    false_negatives = 0
    known_cost = 0.0
    unknown_cost_cases = 0
    review_units = 0
    for case in selected:
        runner_input = CaseInput(
            case_id=case.case_id,
            artifacts=case.artifacts,
            payload=case.payload,
        )
        observation = runner.run_case(candidate, runner_input)
        if observation.case_id != case.case_id:
            raise ValueError(
                f"runner returned {observation.case_id} for case {case.case_id}"
            )
        score = evaluator.evaluate(candidate, case, observation)
        scores.append(score)
        expected_positive = case.expected_label == suite.positive_label
        predicted_positive = observation.predicted_label == suite.positive_label
        false_positives += int(predicted_positive and not expected_positive)
        false_negatives += int(expected_positive and not predicted_positive)
        if observation.provider_cost_usd is None:
            unknown_cost_cases += 1
        else:
            known_cost += observation.provider_cost_usd
        review_units += observation.human_review_units
    return SuiteResult(
        suite_id=suite.suite_id,
        candidate=candidate,
        mean_score=sum(score.score for score in scores) / len(scores),
        false_positives=false_positives,
        false_negatives=false_negatives,
        known_provider_cost_usd=known_cost,
        unknown_provider_cost_cases=unknown_cost_cases,
        human_review_units=review_units,
        case_scores=tuple(scores),
    )


def _load_artifacts(
    root: Path, case_id: str, raw_artifacts: object
) -> tuple[ArtifactRef, ...]:
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ValueError(f"case {case_id} artifacts must be a non-empty list")
    artifacts: list[ArtifactRef] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise ValueError(f"case {case_id} artifact must be an object")
        relative = Path(_required_string(raw, "path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"case {case_id} artifact path must stay under manifest root")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"case {case_id} artifact escapes manifest root")
        expected_hash = _required_string(raw, "sha256").lower()
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise ValueError(f"case {case_id} artifact has invalid sha256")
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"case {case_id} artifact hash mismatch: {relative}")
        artifacts.append(
            ArtifactRef(
                path=path,
                sha256=expected_hash,
                role=_required_string(raw, "role"),
            )
        )
    return tuple(artifacts)


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value
