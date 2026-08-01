from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_evidence import (  # noqa: E402
    hash_files,
    installed_provenance,
    write_verified_bytes,
    write_verified_receipt,
)
from release_omni_contract import (  # noqa: E402
    DATASET,
    MAX_EVALS,
    MODEL,
    REASONING_EFFORT,
    SEED_CANDIDATE,
    STOP_AT_SCORE,
    contract_sha256,
    evaluate,
)


PHASE1_ENGINES = ("gepa", "autoresearch", "meta_harness")
ENGINE_ORDER = {engine: index for index, engine in enumerate(PHASE1_ENGINES)}
HOST_TIMEOUT_SECONDS = 660


@dataclass(frozen=True)
class BranchSpec:
    phase: str
    engine: str
    root: Path
    seed_path: Path

    @property
    def candidate_path(self) -> Path:
        return self.root / "candidate.txt"

    @property
    def receipt_path(self) -> Path:
        if self.engine == "gepa":
            return self.root / "gepa-receipt.json"
        return self.root / "output" / "release_receipt.json"


@dataclass(frozen=True)
class BranchResult:
    spec: BranchSpec
    candidate: bytes
    candidate_sha256: str
    score: float
    total_evals: int
    receipt: dict[str, object]
    receipt_sha256: str
    artifact_fingerprint: tuple[str, ...]
    session_fingerprint: tuple[tuple[str, str], ...]


BranchRunner = Callable[[BranchSpec, Path], None]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"missing or invalid receipt: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"receipt is not an object: {path}")
    return value


def _project_version() -> str:
    import tomllib

    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def installed_skill_path() -> Path:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    if not configured:
        raise RuntimeError("phase certifier requires GEPA_CODEX_SKILL_DIR")
    return Path(configured).expanduser().resolve()


def freeze_contract(output_root: Path) -> tuple[Path, str]:
    frozen = output_root / "frozen"
    frozen.mkdir(parents=True)
    source = SCRIPT_DIR / "release_omni_contract.py"
    content = source.read_bytes()
    frozen_contract = frozen / "release_omni_contract.py"
    write_verified_bytes(frozen_contract, content)
    digest = _sha256(content)
    if digest != contract_sha256():
        raise RuntimeError("frozen contract does not match the active evaluator")
    seed_path = frozen / "seed.txt"
    write_verified_bytes(seed_path, SEED_CANDIDATE.encode("utf-8"))
    manifest = {
        "schema_version": 1,
        "contract_sha256": digest,
        "seed_sha256": _sha256(seed_path.read_bytes()),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "max_evals": MAX_EVALS,
        "stop_at_score": STOP_AT_SCORE,
        "phase1_engines": list(PHASE1_ENGINES),
        "continuation_engine": "autoresearch",
        "model_call_ceiling": 4,
        "retries": 0,
    }
    write_verified_receipt(frozen / "manifest.json", manifest)
    return seed_path, digest


def _command(spec: BranchSpec) -> list[str]:
    common = [
        "--output-dir",
        os.fspath(spec.root),
        "--seed-file",
        os.fspath(spec.seed_path),
        "--candidate-out",
        os.fspath(spec.candidate_path),
    ]
    if spec.engine == "gepa":
        return [
            sys.executable,
            os.fspath(SCRIPT_DIR / "release_inprocess_smoke.py"),
            "--engine",
            "gepa",
            *common,
        ]
    return [
        sys.executable,
        os.fspath(SCRIPT_DIR / "release_dogfood.py"),
        "--engine",
        spec.engine,
        *common,
    ]


def run_live_branch(spec: BranchSpec, skill: Path) -> None:
    environment = dict(os.environ)
    environment["RUN_CODEX_LIVE"] = "1"
    environment["GEPA_CODEX_SKILL_DIR"] = os.fspath(skill)
    completed = subprocess.run(
        _command(spec),
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=HOST_TIMEOUT_SECONDS,
    )
    spec.root.mkdir(parents=True, exist_ok=True)
    write_verified_bytes(
        spec.root / "certifier-stdout.txt", completed.stdout.encode("utf-8")
    )
    write_verified_bytes(
        spec.root / "certifier-stderr.txt", completed.stderr.encode("utf-8")
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{spec.phase} {spec.engine} failed with exit {completed.returncode}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"branch receipt has invalid {label}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"branch receipt has invalid {label}")
    return value


def _artifact_fingerprint(receipt: Mapping[str, object]) -> tuple[str, ...]:
    hashes = receipt.get("hashes", receipt.get("file_hashes"))
    values = _mapping(hashes, "artifact hashes")
    plugin_key = "plugin_manifest" if "plugin_manifest" in values else "plugin_json"
    return tuple(
        _string(values.get(key), f"hash {key}")
        for key in ("skill", plugin_key, "codex_lm", "adapter", "contract")
    )


def _sessions(receipt: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw = receipt.get("session_mapping")
    if not isinstance(raw, list) or len(raw) != 1:
        raise RuntimeError("branch must have exactly one adapter invocation")
    sessions: list[tuple[str, str]] = []
    for item in raw:
        mapping = _mapping(item, "session mapping")
        sessions.append(
            (
                _string(mapping.get("upstream_session_id"), "upstream session"),
                _string(mapping.get("codex_thread_id"), "Codex thread"),
            )
        )
    return tuple(sessions)


def validate_branch(
    spec: BranchSpec, expected_contract: str, expected_seed: str
) -> BranchResult:
    receipt = _read_json(spec.receipt_path)
    if receipt.get("status") != "success" or receipt.get("engine") != spec.engine:
        raise RuntimeError(f"{spec.phase} {spec.engine} is not terminal-success")
    if receipt.get("contract_sha256") != expected_contract:
        raise RuntimeError("branch contract hash differs from the frozen evaluator")
    if receipt.get("seed_candidate_sha256") != expected_seed:
        raise RuntimeError("branch seed hash differs from its assigned seed")
    policy = _mapping(receipt.get("policy"), "policy")
    if policy.get("max_evals") != MAX_EVALS or policy.get("stop_at_score") is not None:
        raise RuntimeError("branch evaluation policy differs from the shared slice")
    model = receipt.get("model", policy.get("target_model"))
    effort = policy.get("reasoning_effort")
    if model != MODEL or effort != REASONING_EFFORT:
        raise RuntimeError("branch model or reasoning effort differs")
    if receipt.get("retry_count", policy.get("retry_count")) != 0:
        raise RuntimeError("branch recorded a retry")
    if policy.get("max_adapter_invocations") != 1:
        raise RuntimeError("branch adapter ceiling is not one")
    provenance = _mapping(receipt.get("provenance"), "provenance")
    if provenance.get("installed_plugin") is not True:
        raise RuntimeError("branch did not use the installed plugin")
    result = _mapping(receipt.get("result"), "result")
    score = result.get("best_score")
    total_evals = result.get("eval_count", result.get("total_evals"))
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise RuntimeError("branch score is invalid")
    if isinstance(total_evals, bool) or not isinstance(total_evals, int):
        raise RuntimeError("branch evaluation count is invalid")
    if not 1 <= total_evals <= MAX_EVALS:
        raise RuntimeError("branch exceeded the shared evaluation slice")
    candidate = spec.candidate_path.read_bytes()
    candidate_sha256 = _sha256(candidate)
    if result.get("best_candidate_sha256") != candidate_sha256:
        raise RuntimeError("branch candidate bytes do not match its receipt")
    try:
        candidate_text = candidate.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("branch candidate is not strict UTF-8") from exc
    local_score, _feedback = evaluate(candidate_text, DATASET[0])
    if float(score) != local_score:
        raise RuntimeError("branch score does not match the frozen evaluator")
    return BranchResult(
        spec=spec,
        candidate=candidate,
        candidate_sha256=candidate_sha256,
        score=float(score),
        total_evals=total_evals,
        receipt=receipt,
        receipt_sha256=_sha256(spec.receipt_path.read_bytes()),
        artifact_fingerprint=_artifact_fingerprint(receipt),
        session_fingerprint=_sessions(receipt),
    )


def select_winner(results: Sequence[BranchResult]) -> BranchResult:
    if {result.spec.engine for result in results} != set(PHASE1_ENGINES):
        raise RuntimeError("Phase 1 did not produce all three engines")
    fingerprints = {result.artifact_fingerprint for result in results}
    if len(fingerprints) != 1:
        raise RuntimeError("Phase 1 engines did not use one installed artifact")
    sessions = [session for result in results for session in result.session_fingerprint]
    if len(sessions) != len(set(sessions)):
        raise RuntimeError("Phase 1 reused an adapter session")
    return min(results, key=lambda item: (-item.score, ENGINE_ORDER[item.spec.engine]))


def _branch_summary(result: BranchResult) -> dict[str, object]:
    return {
        "engine": result.spec.engine,
        "phase": result.spec.phase,
        "candidate_sha256": result.candidate_sha256,
        "score": result.score,
        "total_evals": result.total_evals,
        "receipt_sha256": result.receipt_sha256,
        "session_mapping": [list(pair) for pair in result.session_fingerprint],
    }


def certify(
    output_root: Path,
    *,
    branch_runner: BranchRunner = run_live_branch,
) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=False)
    seed_path, frozen_contract = freeze_contract(output_root)
    skill = installed_skill_path()
    provenance = installed_provenance(skill, REPOSITORY_ROOT, _project_version())
    installed_hashes = hash_files(
        {
            "skill": skill / "SKILL.md",
            "plugin_manifest": Path(provenance["plugin_manifest"]),
            "codex_lm": skill / "scripts" / "codex_lm.py",
            "adapter": skill / "scripts" / "codex_claude_adapter.py",
        }
    )
    phase1_specs = [
        BranchSpec("phase1", engine, output_root / "phase1" / engine, seed_path)
        for engine in PHASE1_ENGINES
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(branch_runner, spec, skill): spec for spec in phase1_specs
        }
        failures: list[str] = []
        for future, spec in futures.items():
            try:
                future.result()
            except Exception as exc:
                failures.append(f"{spec.engine}: {exc}")
    if failures:
        raise RuntimeError("Phase 1 failed: " + "; ".join(failures))
    seed_sha256 = _sha256(seed_path.read_bytes())
    phase1 = [
        validate_branch(spec, frozen_contract, seed_sha256) for spec in phase1_specs
    ]
    winner = select_winner(phase1)
    winner_path = output_root / "winner" / "candidate.txt"
    write_verified_bytes(winner_path, winner.candidate)
    if _sha256(winner_path.read_bytes()) != winner.candidate_sha256:
        raise RuntimeError("winner bytes changed during selection")
    continuation_spec = BranchSpec(
        "continuation",
        "autoresearch",
        output_root / "phase2" / "autoresearch",
        winner_path,
    )
    branch_runner(continuation_spec, skill)
    continuation = validate_branch(
        continuation_spec, frozen_contract, winner.candidate_sha256
    )
    prior_sessions = {
        session for result in phase1 for session in result.session_fingerprint
    }
    if any(session in prior_sessions for session in continuation.session_fingerprint):
        raise RuntimeError("continuation reused a Phase 1 adapter session")
    if continuation.artifact_fingerprint != winner.artifact_fingerprint:
        raise RuntimeError("continuation did not use the Phase 1 installed artifact")
    receipt = {
        "schema_version": 1,
        "status": "success",
        "verdict": "paper_informed_optimize_everything_certified",
        "claim": (
            "One installed artifact ran equal early slices across GEPA, "
            "AutoResearch, and MetaHarness, selected the best shared score, "
            "and seeded a fresh AutoResearch continuation with exact winner bytes."
        ),
        "limitations": [
            "Public deterministic task; no semantic-quality claim.",
            "Equal max_evals, not dollar-matched budgets.",
        ],
        "policy": {
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "max_evals_per_stage": MAX_EVALS,
            "stop_at_score": STOP_AT_SCORE,
            "model_call_ceiling": 4,
            "retries": 0,
        },
        "contract_sha256": frozen_contract,
        "installed_provenance": provenance,
        "installed_hashes": installed_hashes,
        "phase1": [_branch_summary(result) for result in phase1],
        "selection": {
            "rule": "highest_score_then_fixed_engine_order",
            "winner_engine": winner.spec.engine,
            "winner_candidate_sha256": winner.candidate_sha256,
            "winner_receipt_sha256": winner.receipt_sha256,
        },
        "continuation": _branch_summary(continuation),
        "handoff": {
            "winner_candidate_sha256": winner.candidate_sha256,
            "continuation_seed_sha256": continuation.receipt[
                "seed_candidate_sha256"
            ],
            "exact_bytes_conserved": True,
            "fresh_session": True,
        },
    }
    receipt_path = output_root / "phase-certification-receipt.json"
    write_verified_receipt(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("phase certifier requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    try:
        receipt = certify(args.output_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
