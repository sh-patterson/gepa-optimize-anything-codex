from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_phase_certifier.py"
SPEC = importlib.util.spec_from_file_location("release_phase_certifier_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
certifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = certifier
SPEC.loader.exec_module(certifier)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _installed_skill(tmp_path: Path) -> Path:
    plugin = tmp_path / "installed" / "gepa-optimize-anything"
    skill = plugin / "skills" / "gepa-optimize-anything-codex"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# installed\n", encoding="utf-8")
    (scripts / "codex_lm.py").write_text("# public driver\n", encoding="utf-8")
    (scripts / "codex_claude_adapter.py").write_text(
        "# public adapter\n", encoding="utf-8"
    )
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text('{"version":"1.0.3"}\n', encoding="utf-8")
    return skill


def _provenance(skill: Path, *_args: object) -> dict[str, object]:
    return {
        "installed_plugin": True,
        "skill_path": str(skill),
        "plugin_manifest": str(skill.parents[1] / ".codex-plugin" / "plugin.json"),
        "plugin_version": "1.0.3",
        "repository_commit": "a" * 40,
        "gepa_commit": "b" * 40,
    }


def _fake_receipt(
    spec: object,
    candidate: bytes,
    seed_sha256: str,
    session: str,
) -> dict[str, object]:
    hashes = {
        "skill": "1" * 64,
        "plugin_manifest": "2" * 64,
        "codex_lm": "3" * 64,
        "adapter": "4" * 64,
        "contract": certifier.contract_sha256(),
    }
    policy = {
        "reasoning_effort": certifier.REASONING_EFFORT,
        "max_adapter_invocations": 1,
        "retry_count": 0,
        "max_evals": certifier.MAX_EVALS,
        "stop_at_score": certifier.STOP_AT_SCORE,
    }
    result = {
        "best_score": 1.0,
        "total_evals": 2,
        "best_candidate_sha256": _sha256(candidate),
    }
    receipt: dict[str, object] = {
        "schema_version": 2,
        "status": "success",
        "engine": spec.engine,
        "policy": policy,
        "provenance": {"installed_plugin": True},
        "contract_sha256": certifier.contract_sha256(),
        "seed_candidate_sha256": seed_sha256,
        "result": result,
        "session_mapping": [
            {
                "upstream_session_id": f"sha256:up-{session}",
                "codex_thread_id": f"sha256:thread-{session}",
            }
        ],
    }
    if spec.engine == "gepa":
        receipt["model"] = certifier.MODEL
        receipt["retry_count"] = 0
        receipt["hashes"] = hashes
        result["eval_count"] = result.pop("total_evals")
    else:
        policy["target_model"] = certifier.MODEL
        receipt["file_hashes"] = {
            **hashes,
            "plugin_json": hashes.pop("plugin_manifest"),
        }
    return receipt


def _fake_runner(
    barrier: threading.Barrier | None = None,
    reused_continuation_session: bool = False,
):
    calls: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    def run(spec: object, _skill: Path) -> None:
        if barrier is not None and spec.phase == "phase1":
            barrier.wait(timeout=2)
        seed = spec.seed_path.read_bytes()
        candidate = f"Return BLUE. {spec.phase}-{spec.engine}".encode()
        spec.root.mkdir(parents=True, exist_ok=False)
        certifier.write_verified_bytes(spec.candidate_path, candidate)
        session = (
            "phase1-gepa"
            if reused_continuation_session and spec.phase == "continuation"
            else f"{spec.phase}-{spec.engine}"
        )
        receipt = _fake_receipt(spec, candidate, _sha256(seed), session)
        certifier.write_verified_receipt(spec.receipt_path, receipt)
        with lock:
            calls.append((spec.phase, spec.engine, _sha256(seed)))

    return run, calls


def test_frozen_contract_preserves_exact_evaluator_seed_and_policy(tmp_path: Path) -> None:
    root = tmp_path / "cert"
    root.mkdir()

    seed_path, digest = certifier.freeze_contract(root)

    frozen = root / "frozen" / "release_omni_contract.py"
    manifest = json.loads((root / "frozen" / "manifest.json").read_text())
    assert frozen.read_bytes() == (ROOT / "scripts" / "release_omni_contract.py").read_bytes()
    assert digest == _sha256(frozen.read_bytes()) == certifier.contract_sha256()
    assert seed_path.read_bytes() == certifier.SEED_CANDIDATE.encode()
    assert manifest["phase1_engines"] == list(certifier.PHASE1_ENGINES)
    assert manifest["max_evals"] == 10
    assert manifest["stop_at_score"] is None
    assert manifest["model_call_ceiling"] == 4


def test_certifier_runs_concurrent_equal_phase1_and_exact_fresh_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(skill))
    monkeypatch.setattr(certifier, "installed_provenance", _provenance)
    runner, calls = _fake_runner(threading.Barrier(3))

    receipt = certifier.certify(tmp_path / "cert", branch_runner=runner)

    assert receipt["status"] == "success"
    assert receipt["verdict"] == "paper_informed_optimize_everything_certified"
    assert {(phase, engine) for phase, engine, _seed in calls} == {
        ("phase1", "gepa"),
        ("phase1", "autoresearch"),
        ("phase1", "meta_harness"),
        ("continuation", "autoresearch"),
    }
    selection = receipt["selection"]
    handoff = receipt["handoff"]
    assert selection["winner_engine"] == "gepa"
    assert handoff["winner_candidate_sha256"] == handoff["continuation_seed_sha256"]
    assert handoff["exact_bytes_conserved"] is True
    continuation_call = next(call for call in calls if call[0] == "continuation")
    assert continuation_call[2] == selection["winner_candidate_sha256"]
    assert len({spec[2] for spec in calls[:3]}) == 1


def test_branch_validation_fails_closed_on_candidate_contract_and_score_tamper(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed.txt"
    seed.write_text(certifier.SEED_CANDIDATE, encoding="utf-8")
    spec = certifier.BranchSpec("phase1", "gepa", tmp_path / "gepa", seed)
    runner, _calls = _fake_runner()
    runner(spec, tmp_path)
    expected_seed = _sha256(seed.read_bytes())

    assert certifier.validate_branch(
        spec, certifier.contract_sha256(), expected_seed
    ).score == 1.0
    spec.candidate_path.write_text("Return GREEN.", encoding="utf-8")
    with pytest.raises(RuntimeError, match="candidate bytes"):
        certifier.validate_branch(spec, certifier.contract_sha256(), expected_seed)

    runner_spec = certifier.BranchSpec(
        "phase1", "gepa", tmp_path / "gepa-two", seed
    )
    runner(runner_spec, tmp_path)
    receipt = json.loads(runner_spec.receipt_path.read_text())
    receipt["contract_sha256"] = "0" * 64
    runner_spec.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RuntimeError, match="contract hash"):
        certifier.validate_branch(
            runner_spec, certifier.contract_sha256(), expected_seed
        )


def test_selection_rejects_mixed_installed_artifacts_and_duplicate_sessions(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "seed.txt"
    seed.write_text(certifier.SEED_CANDIDATE, encoding="utf-8")
    runner, _calls = _fake_runner()
    results = []
    for engine in certifier.PHASE1_ENGINES:
        spec = certifier.BranchSpec("phase1", engine, tmp_path / engine, seed)
        runner(spec, tmp_path)
        results.append(
            certifier.validate_branch(
                spec, certifier.contract_sha256(), _sha256(seed.read_bytes())
            )
        )
    mixed = [
        results[0],
        results[1],
        certifier.BranchResult(
            **{
                **results[2].__dict__,
                "artifact_fingerprint": ("different",) * 5,
            }
        ),
    ]
    with pytest.raises(RuntimeError, match="one installed artifact"):
        certifier.select_winner(mixed)
    duplicate = [
        results[0],
        certifier.BranchResult(
            **{
                **results[1].__dict__,
                "session_fingerprint": results[0].session_fingerprint,
            }
        ),
        results[2],
    ]
    with pytest.raises(RuntimeError, match="reused an adapter session"):
        certifier.select_winner(duplicate)


def test_certifier_rejects_continuation_session_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _installed_skill(tmp_path)
    monkeypatch.setenv("GEPA_CODEX_SKILL_DIR", str(skill))
    monkeypatch.setattr(certifier, "installed_provenance", _provenance)
    runner, _calls = _fake_runner(reused_continuation_session=True)

    with pytest.raises(RuntimeError, match="continuation reused"):
        certifier.certify(tmp_path / "cert", branch_runner=runner)


def test_cli_requires_explicit_live_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("RUN_CODEX_LIVE", raising=False)

    assert certifier.main(["--output-dir", os.fspath(tmp_path / "cert")]) == 2
    assert "RUN_CODEX_LIVE=1" in capsys.readouterr().err
