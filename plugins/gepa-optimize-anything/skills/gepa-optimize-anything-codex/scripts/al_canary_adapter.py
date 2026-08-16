"""Adapter for the frozen A|L political-ad cascade canary.

This module validates fixture custody and keeps sealed truth out of the runner.
It performs no provider call unless the caller explicitly supplies a live runner.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from codex_runtime import CodexRuntime, InvocationSpec

CASES_SHA256 = "44588f35af89527e97b3a080523df8b97f0bafc64051cb4b53114ed1e3fbc9f8"
EXPECTED_CASES = 48
EXPECTED_SPLITS = {"development": 28, "validation": 20}
EXPECTED_FAMILIES = 37


class RunCase(Protocol):
    def run_case(self, prompt: str, model_input: dict[str, Any]) -> dict[str, Any]: ...


class BatchScore(Protocol):
    score: float
    metrics: dict[str, Any]
    feedback: dict[str, Any]


class BatchEvaluator(Protocol):
    def __call__(
        self,
        cases: list[dict[str, Any]],
        predictions: list[dict[str, Any]],
        *,
        max_cost_per_case_usd: float,
    ) -> BatchScore: ...


@dataclass(frozen=True)
class AlFixture:
    root: Path
    cases_sha256: str
    cases: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AlCanaryResult:
    split: str
    score: float
    metrics: dict[str, Any]
    feedback: dict[str, Any]
    prompt_sha256: str
    cases_sha256: str


def load_fixture(root: Path, *, cases_sha256: str = CASES_SHA256) -> AlFixture:
    """Verify the frozen package and every referenced transcript/frame derivative."""
    root = root.resolve()
    cases_path = root / "cases.jsonl"
    raw_bytes = cases_path.read_bytes()
    actual_cases_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_cases_hash != cases_sha256.lower():
        raise ValueError("A|L cases.jsonl hash mismatch")
    cases = tuple(
        json.loads(line)
        for line in raw_bytes.decode("utf-8-sig").splitlines()
        if line.strip()
    )
    if len(cases) != EXPECTED_CASES:
        raise ValueError(f"A|L fixture must contain {EXPECTED_CASES} cases")

    split_counts: dict[str, int] = {}
    family_splits: dict[str, str] = {}
    seen_ids: set[str] = set()
    for case in cases:
        case_id = _string(case, "case_id")
        if case_id in seen_ids:
            raise ValueError(f"duplicate A|L case_id: {case_id}")
        seen_ids.add(case_id)
        split = _string(case, "split")
        split_counts[split] = split_counts.get(split, 0) + 1
        truth = case.get("sealed_truth")
        if not isinstance(truth, dict):
            raise ValueError(f"A|L case {case_id} lacks sealed truth")
        family = _string(truth, "creative_family_id")
        prior = family_splits.setdefault(family, split)
        if prior != split:
            raise ValueError(f"A|L family {family} crosses frozen splits")
        model_input = case.get("model_input")
        if not isinstance(model_input, dict):
            raise ValueError(f"A|L case {case_id} lacks model_input")
        sources = model_input.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"A|L case {case_id} has no sources")
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError(f"A|L case {case_id} has an invalid source")
            _verify_ref(case_id, source.get("transcript"), "transcript")
            _verify_ref(
                case_id,
                source.get("sparse_frame_derivative"),
                "sparse frame derivative",
            )
    if split_counts != EXPECTED_SPLITS:
        raise ValueError(f"A|L fixture split counts changed: {split_counts}")
    if len(family_splits) != EXPECTED_FAMILIES:
        raise ValueError("A|L fixture creative-family count changed")
    return AlFixture(root=root, cases_sha256=actual_cases_hash, cases=cases)


def run_canary(
    prompt: str,
    fixture: AlFixture,
    runner: RunCase,
    evaluator: BatchEvaluator,
    *,
    split: str,
    max_cost_per_case_usd: float,
) -> AlCanaryResult:
    """Run one split and disclose only aggregate evaluator feedback."""
    if split not in EXPECTED_SPLITS:
        raise ValueError("split must be development or validation")
    if not prompt.strip():
        raise ValueError("prompt must be non-empty")
    selected = [case for case in fixture.cases if case["split"] == split]
    predictions = [
        runner.run_case(prompt, copy.deepcopy(case["model_input"]))
        for case in selected
    ]
    result = evaluator(
        selected,
        predictions,
        max_cost_per_case_usd=max_cost_per_case_usd,
    )
    feedback = copy.deepcopy(result.feedback)
    if feedback.get("sealed_case_details_disclosed") is not False:
        raise ValueError("A|L evaluator must prove sealed case details were not disclosed")
    return AlCanaryResult(
        split=split,
        score=float(result.score),
        metrics=copy.deepcopy(result.metrics),
        feedback=feedback,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        cases_sha256=fixture.cases_sha256,
    )


class CodexRuntimeRunner:
    """Codex-native implementation of run_case(prompt, model_input)."""

    def __init__(
        self,
        runtime: CodexRuntime,
        *,
        cwd: Path,
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "high",
        timeout_seconds: float = 120.0,
    ) -> None:
        self.runtime = runtime
        self.cwd = cwd.resolve()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds

    def run_case(self, prompt: str, model_input: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        output_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "decision",
                "attribution",
                "evidence_refs",
                "unsupported_claims",
            ],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["political_ad", "not_political_ad", "abstain"],
                },
                "attribution": {"type": ["object", "null"]},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "unsupported_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }
        task = (
            f"{prompt.rstrip()}\n\n"
            "Classify only the supplied model input. Return JSON matching the schema.\n"
            f"MODEL_INPUT_JSON:\n{json.dumps(model_input, sort_keys=True)}"
        )
        result = self.runtime.invoke(
            InvocationSpec(
                invocation_id=f"al-{uuid.uuid4().hex}",
                prompt=task,
                cwd=self.cwd,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                sandbox="read-only",
                timeout_seconds=self.timeout_seconds,
                output_schema=output_schema,
            )
        )
        try:
            prediction = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ValueError("Codex returned invalid A|L prediction JSON") from exc
        if not isinstance(prediction, dict):
            raise ValueError("Codex A|L prediction must be an object")
        sources = model_input.get("sources", [])
        frames_seen = sum(
            int(isinstance(source, dict) and "sparse_frame_derivative" in source)
            for source in sources
        )
        prediction["receipt"] = {
            "calls": 1,
            "cost_usd": None,
            "cost_status": result.cost_status,
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "frames_seen": frames_seen,
            "provider": result.provider,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
        }
        return prediction


def _verify_ref(case_id: str, raw: object, role: str) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"A|L case {case_id} lacks {role} custody")
    path = Path(_string(raw, "path"))
    expected_hash = _string(raw, "sha256").lower()
    if not path.is_file():
        raise FileNotFoundError(f"A|L case {case_id} {role} is missing: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError(f"A|L case {case_id} {role} hash mismatch")


def _string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value
