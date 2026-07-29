from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_ROUTES = {"billing", "support", "account_security", "technical", "general"}
SEED = '{"routes":[],"default":"general"}'
SCORE_TRACE: list[dict[str, Any]] = []


def candidate_hash(candidate: str) -> str:
    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()


def parse_policy(candidate: str) -> dict[str, Any]:
    policy = json.loads(candidate)
    if not isinstance(policy, dict) or set(policy) != {"routes", "default"}:
        raise ValueError("policy must contain only routes and default")
    if policy["default"] not in ALLOWED_ROUTES or not isinstance(policy["routes"], list):
        raise ValueError("invalid default or routes")
    for rule in policy["routes"]:
        if not isinstance(rule, dict) or set(rule) != {"keywords", "destination"}:
            raise ValueError("invalid rule")
        if rule["destination"] not in ALLOWED_ROUTES:
            raise ValueError("invalid destination")
        keywords = rule["keywords"]
        if (
            not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(word, str) or not word.strip() for word in keywords)
        ):
            raise ValueError("invalid keywords")
    return policy


def route(policy: dict[str, Any], message: str) -> str:
    normalized = message.casefold()
    for rule in policy["routes"]:
        if any(word.casefold() in normalized for word in rule["keywords"]):
            return str(rule["destination"])
    return str(policy["default"])


def score_candidate(
    candidate: str, cases: list[dict[str, str]]
) -> tuple[float, list[str], bool]:
    try:
        policy = parse_policy(candidate)
    except (ValueError, json.JSONDecodeError, TypeError):
        return 0.0, [case["id"] for case in cases], False
    failed = [
        case["id"]
        for case in cases
        if route(policy, case["message"]) != case["route"]
    ]
    return (len(cases) - len(failed)) / len(cases), failed, True


def evaluate(candidate: str, _example: object) -> tuple[float, dict[str, Any]]:
    fixture = json.loads(Path(os.environ["DOGFOOD_FIXTURE"]).read_text(encoding="utf-8"))
    cases = fixture["cases"]
    score, failed, valid = score_candidate(candidate, cases)
    SCORE_TRACE.append(
        {
            "candidate_sha256": candidate_hash(candidate),
            "score": score,
            "valid_json": valid,
        }
    )
    if not valid:
        feedback = "Candidate must be valid JSON matching the required routing-policy schema."
    elif failed:
        feedback = "Failed case IDs: " + ", ".join(failed)
    else:
        feedback = "All routing cases passed."
    return score, {"passed": not failed and valid, "feedback": feedback}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".tmp",
        delete=False,
    ) as target:
        temporary = Path(target.name)
        json.dump(payload, target, indent=2, sort_keys=True)
        target.write("\n")
    os.replace(temporary, path)


def main() -> None:
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    root = Path(os.environ["DOGFOOD_WORK_DIR"])
    run_dir = root / "meta-run"
    output_dir = root / "meta-output"
    result = optimize_anything(
        seed_candidate=SEED,
        evaluator=evaluate,
        dataset=[{"case": "routing-suite"}],
        objective=(
            "Produce a JSON routing policy with routes containing keyword lists and a "
            "destination plus a default route. Send charges and invoices to billing; "
            "refunds and cancellations to support; password and login-code problems to "
            "account_security; crashes and outages to technical; everything else to general."
        ),
        background=(
            "Return only the JSON policy. Valid destinations are billing, support, "
            "account_security, technical, and general."
        ),
        config=OptimizeAnythingConfig(
            engine="meta_harness",
            name="installed-skill-routing-dogfood",
            max_evals=3,
            stop_at_score=1.0,
            sandbox=True,
            run_dir=str(run_dir),
            output_dir=str(output_dir),
            engine_config={
                "model": "gpt-5.6-luna",
                "effort": "high",
                "max_iterations": 1,
                "max_candidates_per_iter": 2,
            },
        ),
    )
    fixture = json.loads(Path(os.environ["DOGFOOD_FIXTURE"]).read_text(encoding="utf-8"))
    cases = fixture["cases"]
    seed_score, _, _ = score_candidate(SEED, cases)
    best_score, _, best_valid = score_candidate(result.best_candidate, cases)
    proposed_hashes = sorted(
        {
            candidate_hash(path.read_text(encoding="utf-8"))
            for path in output_dir.glob("work/agents/iter*.txt")
            if path.is_file()
        }
    )
    atomic_json(
        Path(os.environ["DOGFOOD_RESULT_PATH"]),
        {
            "status": "success",
            "seed_score": seed_score,
            "best_score": best_score,
            "best_candidate_sha256": candidate_hash(result.best_candidate),
            "best_candidate_valid_json": best_valid,
            "total_evals": result.total_evals,
            "candidate_hashes": proposed_hashes,
            "score_trace": SCORE_TRACE,
        },
    )


if __name__ == "__main__":
    main()
