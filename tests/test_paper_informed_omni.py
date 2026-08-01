from __future__ import annotations

import hashlib
from pathlib import Path

from gepa.optimize_anything import (
    OptimizeAnythingConfig,
    Result,
    optimize_anything,
    optimize_best_of,
    register_engine,
)


class _BranchA:
    name = "paper_contract_branch_a"

    def __init__(self, _config: OptimizeAnythingConfig) -> None:
        pass

    def run(self, task, server) -> Result:
        candidate = f"{task.seed_candidate}|branch-a"
        score, _ = server.evaluate(candidate)
        return Result(candidate, score, total_evals=1)

    def process_result(self, _result: Result, _output_dir: Path | None) -> None:
        pass


class _BranchB:
    name = "paper_contract_branch_b"

    def __init__(self, _config: OptimizeAnythingConfig) -> None:
        pass

    def run(self, task, server) -> Result:
        candidate = f"{task.seed_candidate}|branch-b"
        score, _ = server.evaluate(candidate)
        return Result(candidate, score, total_evals=1)

    def process_result(self, _result: Result, _output_dir: Path | None) -> None:
        pass


class _FreshContinuation:
    name = "paper_contract_fresh_continuation"
    seen_seeds: list[str] = []

    def __init__(self, _config: OptimizeAnythingConfig) -> None:
        pass

    def run(self, task, server) -> Result:
        self.seen_seeds.append(task.seed_candidate)
        candidate = f"{task.seed_candidate}|fresh"
        score, _ = server.evaluate(candidate)
        return Result(candidate, score, total_evals=1)

    def process_result(self, _result: Result, _output_dir: Path | None) -> None:
        pass


register_engine(_BranchA.name, _BranchA)
register_engine(_BranchB.name, _BranchB)
register_engine(_FreshContinuation.name, _FreshContinuation)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_pinned_composition_selects_best_then_starts_fresh(tmp_path: Path) -> None:
    scores = {
        "seed|branch-a": 0.4,
        "seed|branch-b": 0.8,
        "seed|branch-b|fresh": 0.9,
    }

    def evaluate(candidate: str) -> tuple[float, dict[str, str]]:
        return scores[candidate], {"candidate": candidate}

    explore = optimize_best_of(
        "seed",
        evaluator=evaluate,
        objective="exercise one shared evaluator",
        configs=[
            OptimizeAnythingConfig(
                engine=_BranchA.name,
                max_evals=1,
                output_dir=tmp_path / "phase1-a",
                run_dir=str(tmp_path / "state-a"),
            ),
            OptimizeAnythingConfig(
                engine=_BranchB.name,
                max_evals=1,
                output_dir=tmp_path / "phase1-b",
                run_dir=str(tmp_path / "state-b"),
            ),
        ],
    )

    winner = explore.best_candidate
    winner_sha256 = _sha(winner)
    assert winner == "seed|branch-b"
    assert explore.best_score == 0.8

    _FreshContinuation.seen_seeds.clear()
    continued = optimize_anything(
        winner,
        evaluator=evaluate,
        objective="exercise one shared evaluator",
        config=OptimizeAnythingConfig(
            engine=_FreshContinuation.name,
            max_evals=1,
            output_dir=tmp_path / "phase2",
            run_dir=str(tmp_path / "state-fresh"),
        ),
    )

    assert _FreshContinuation.seen_seeds == [winner]
    assert _sha(_FreshContinuation.seen_seeds[0]) == winner_sha256
    assert continued.best_candidate == "seed|branch-b|fresh"
    assert continued.best_score == 0.9
    for directory in ("phase1-a", "phase1-b", "phase2"):
        assert (tmp_path / directory / "summary.json").is_file()
