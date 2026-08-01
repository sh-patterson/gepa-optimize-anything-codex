from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


SEED_CANDIDATE = "Return RED."
TARGET_CANDIDATE = "Return BLUE."
TARGET_TOKEN = "BLUE"
OBJECTIVE = "Write a candidate containing BLUE."
BACKGROUND = "The candidate must satisfy the deterministic evaluator."
DATASET = ({"case": "exact-token"},)
MAX_EVALS = 10
STOP_AT_SCORE = None
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "high"


def evaluate(
    candidate: str, _example: object = None
) -> tuple[float, dict[str, Any]]:
    passed = TARGET_TOKEN in candidate
    return float(passed), {
        "passed": passed,
        "feedback": "Passed." if passed else "Candidate must contain BLUE.",
    }


def contract_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
