from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


ENGINES = frozenset({"gepa", "best_of_n"})
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "high"
MAX_ADAPTER_INVOCATIONS = 4
SEED = "Return RED."
TARGET = "Return BLUE."
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if completed.returncode == 0 and len(commit) == 40:
        return commit
    try:
        archived_commit = (path / "release" / "COMMIT").read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        raise RuntimeError("cannot resolve release runner commit") from exc
    if len(archived_commit) == 40 and all(
        character in "0123456789abcdef" for character in archived_commit
    ):
        return archived_commit
    raise RuntimeError("cannot resolve release runner commit")


def _installed_vcs_commit(package: str) -> str:
    raw = importlib.metadata.distribution(package).read_text("direct_url.json")
    try:
        payload = json.loads(raw or "")
        commit = payload["vcs_info"]["commit_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot resolve installed {package} commit") from exc
    if not isinstance(commit, str) or len(commit) != 40:
        raise RuntimeError(f"cannot resolve installed {package} commit")
    return commit


def _installed_skill() -> tuple[Path, Path]:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    if not configured:
        raise RuntimeError("in-process smoke requires GEPA_CODEX_SKILL_DIR")
    skill = Path(configured).expanduser().resolve()
    try:
        skill.relative_to(REPOSITORY_ROOT)
    except ValueError:
        pass
    else:
        raise RuntimeError("in-process smoke requires an installed plugin")
    if not (skill / "SKILL.md").is_file():
        raise RuntimeError("installed skill is missing SKILL.md")
    manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed plugin manifest is missing or invalid") from exc
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        expected_version = tomllib.load(source)["project"]["version"]
    if not isinstance(payload, dict) or payload.get("version") != expected_version:
        raise RuntimeError("installed plugin version does not match the runner")
    return skill, manifest


def _evaluate(candidate: str, _example: object = None) -> tuple[float, dict[str, str]]:
    passed = TARGET in candidate
    return float(passed), {"feedback": "Candidate must contain BLUE."}


def config_for(engine: str, root: Path) -> dict[str, Any]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine: {engine}")
    engine_config: dict[str, Any] = {
        "model": MODEL,
        "max_n": 3,
        "lm_kwargs": {},
    }
    if engine == "gepa":
        engine_config = {
            "reflection": {
                "reflection_lm": MODEL,
                "reflection_lm_kwargs": {},
            },
            "engine": {},
        }
    return {
        "engine": engine,
        "max_evals": 4,
        "stop_at_score": 1.0,
        "run_dir": str(root / "run"),
        "output_dir": str(root / "output"),
        "engine_config": engine_config,
    }


def _usage(metadata: object) -> dict[str, int]:
    if not isinstance(metadata, dict):
        raise RuntimeError("optimizer result metadata is missing")
    value = metadata.get("usage", metadata)
    if not isinstance(value, dict):
        raise RuntimeError("optimizer result usage is missing")
    usage: dict[str, int] = {}
    for name in ("input_tokens", "output_tokens"):
        token_count = value.get(name)
        if isinstance(token_count, bool) or not isinstance(token_count, int):
            raise RuntimeError(f"optimizer usage is missing {name}")
        usage[name] = token_count
    if sum(usage.values()) <= 0:
        raise RuntimeError("optimizer usage is empty")
    return usage


def _codex_usage(lms: list[object]) -> dict[str, int]:
    usage = {
        "input_tokens": sum(int(getattr(lm, "total_tokens_in", 0)) for lm in lms),
        "output_tokens": sum(int(getattr(lm, "total_tokens_out", 0)) for lm in lms),
    }
    if sum(usage.values()) <= 0:
        raise RuntimeError("optimizer usage is empty")
    return usage


def _result(result: object) -> dict[str, Any]:
    for name in ("best_candidate", "best_score", "total_evals", "metadata"):
        if not hasattr(result, name):
            raise RuntimeError(f"optimizer result is missing {name}")
    candidate = getattr(result, "best_candidate")
    score = getattr(result, "best_score")
    evals = getattr(result, "total_evals")
    if (
        candidate != TARGET
        or not isinstance(score, (int, float))
        or score != 1.0
        or isinstance(evals, bool)
        or not isinstance(evals, int)
        or evals <= 0
    ):
        raise RuntimeError("optimizer did not complete RED to BLUE")
    return {
        "improved": True,
        "best_score": float(score),
        "eval_count": int(evals),
        "best_candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
    }


def _atomic_receipt(path: Path, receipt: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(receipt, target, indent=2, sort_keys=True)
            target.write("\n")
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _load_script(path: Path, name: str) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load installed runtime module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _prepare_codex_runtime(skill: Path, work_dir: Path) -> dict[str, Any]:
    if any(os.environ.get(name) for name in ("CODEX_API_KEY", "OPENAI_API_KEY")):
        raise RuntimeError("staged-login proof cannot expose API keys")
    runtime = _load_script(
        skill / "scripts" / "sandbox_runtime.py",
        f"release_inprocess_runtime_{uuid4().hex}",
    )
    driver = _load_script(
        skill / "scripts" / "codex_lm.py",
        f"release_inprocess_driver_{uuid4().hex}",
    )
    state_dir = (
        Path.home()
        / ".cache"
        / "gepa-optimize-anything-codex"
        / "runs"
        / f"release-inprocess-{uuid4().hex}"
    )
    paths = runtime.stage_runtime(runtime.runtime_paths(state_dir=state_dir))
    probe = runtime.probe_runtime(paths)
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout or "Bubblewrap runtime probe failed").strip()
        )
    environment = runtime.runtime_environment(paths)
    environment.update(
        {
            "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
            "CODEX_ADAPTER_MAX_INVOCATIONS": str(MAX_ADAPTER_INVOCATIONS),
            "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
        }
    )
    return {
        "driver": driver.CodexLM,
        "launcher": paths.launcher,
        "adapter": paths.adapter_module,
        "environment": environment,
        "state_dir": state_dir,
        "work_dir": work_dir,
        "probe_success": True,
    }


def _journal_evidence(
    state_dir: Path, expected_invocations: int, expected_usage: dict[str, int]
) -> dict[str, Any]:
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state_dir / "invocations").glob("*.json"))
    ]
    if len(records) != expected_invocations or not records:
        raise RuntimeError("adapter journal invocation count is inconsistent")
    if any(
        record.get("terminal_status") != "completed"
        or record.get("return_code") != 0
        or record.get("target_model") != MODEL
        or record.get("reasoning_effort") != REASONING_EFFORT
        for record in records
    ):
        raise RuntimeError("adapter journal contains an incomplete invocation")
    journal_usage = {
        name: sum(int(record["usage"].get(name, 0)) for record in records)
        for name in ("input_tokens", "output_tokens")
    }
    if journal_usage != expected_usage:
        raise RuntimeError("adapter journal usage is inconsistent")
    estimated_cost = sum(float(record["estimated_cost_usd"]) for record in records)
    if estimated_cost <= 0:
        raise RuntimeError("adapter journal has no positive cost estimate")
    mappings = [
        {
            "upstream_session_id": record.get("upstream_session_id"),
            "codex_thread_id": record.get("codex_thread_id"),
        }
        for record in records
    ]
    if any(not item["upstream_session_id"] or not item["codex_thread_id"] for item in mappings):
        raise RuntimeError("adapter journal session mapping is incomplete")
    return {
        "invocation_count": len(records),
        "estimated_cost_usd": estimated_cost,
        "session_mapping": mappings,
    }


def _run_codex(
    engine: str, values: dict[str, Any], runtime: dict[str, Any]
) -> tuple[object, dict[str, int], dict[str, Any]]:
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    lms: list[object] = []

    def new_lm(model: str, **kwargs: Any) -> object:
        lm = runtime["driver"](
            model,
            launcher=runtime["launcher"],
            cwd=runtime["work_dir"],
            environment=runtime["environment"],
            reasoning_effort=REASONING_EFFORT,
            **kwargs,
        )
        lms.append(lm)
        return lm

    if engine == "gepa":
        lm = new_lm(MODEL)
        reflection = values["engine_config"]["reflection"]
        reflection["reflection_lm"] = lm
        reflection.pop("reflection_lm_kwargs")
        raw = optimize_anything(
            seed_candidate=SEED,
            evaluator=_evaluate,
            objective="Write a candidate containing BLUE.",
            config=OptimizeAnythingConfig(**values),
        )
    else:
        import gepa.oa.engines.best_of_n as best_of_n

        native_lm = best_of_n.LM
        best_of_n.LM = new_lm
        try:
            raw = optimize_anything(
                seed_candidate=SEED,
                evaluator=_evaluate,
                objective="Write a candidate containing BLUE.",
                config=OptimizeAnythingConfig(**values),
            )
        finally:
            best_of_n.LM = native_lm
    usage = _codex_usage(lms)
    expected_invocations = sum(int(getattr(lm, "invocation_count", 0)) for lm in lms)
    evidence = _journal_evidence(
        runtime["state_dir"], expected_invocations, usage
    )
    driver_cost = sum(float(getattr(lm, "total_cost", 0.0)) for lm in lms)
    if abs(driver_cost - evidence["estimated_cost_usd"]) > 1e-9:
        raise RuntimeError("adapter journal cost estimate is inconsistent")
    evidence["probe_success"] = runtime["probe_success"]
    return raw, usage, evidence


def run_smoke(
    engine: str,
    output_dir: Path,
    *,
    optimize: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    skill, manifest = _installed_skill()
    output_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    values = config_for(engine, output_dir)
    if optimize is None:
        runtime = _prepare_codex_runtime(skill, output_dir)
        raw, usage, runtime_evidence = _run_codex(engine, values, runtime)
    else:
        from gepa.optimize_anything import OptimizeAnythingConfig

        raw = optimize(
            seed_candidate=SEED,
            evaluator=_evaluate,
            objective="Write a candidate containing BLUE.",
            config=OptimizeAnythingConfig(**values),
        )
        usage = _usage(getattr(raw, "metadata", None))
        runtime = {
            "launcher": skill / "scripts" / "claude",
            "adapter": skill / "scripts" / "codex_claude_adapter.py",
        }
        runtime_evidence = {
            "invocation_count": 1,
            "estimated_cost_usd": 0.001,
            "session_mapping": [{"upstream_session_id": "test", "codex_thread_id": "test"}],
        }
    result = _result(raw)
    duration_ms = round((time.monotonic() - started) * 1000)
    source_files = {
        "skill": skill / "SKILL.md",
        "plugin_manifest": manifest,
        "runner": Path(__file__).resolve(),
        "codex_lm": skill / "scripts" / "codex_lm.py",
        "adapter": runtime["adapter"],
    }
    receipt = {
        "schema_version": 2,
        "status": "success",
        "engine": engine,
        "provider": "codex",
        "model": MODEL,
        "retry_count": 0,
        "policy": {
            "reasoning_effort": REASONING_EFFORT,
            "max_adapter_invocations": MAX_ADAPTER_INVOCATIONS,
            "pre_submission_retries": 0,
            "max_evals": 4,
            "max_n": 3 if engine == "best_of_n" else None,
        },
        "authentication": {
            "mode": "chatgpt_login",
            "child_api_keys_present": False,
        },
        "sandbox": {
            "codex_workspace_write": True,
            "bubblewrap_probe_success": runtime_evidence.get("probe_success", True),
        },
        "result": result,
        "usage": usage,
        "cost": {
            "estimated_usd": runtime_evidence["estimated_cost_usd"],
            "status": "standard_tier_upper_estimate_from_observed_usage",
        },
        "invocation_count": runtime_evidence["invocation_count"],
        "session_mapping": runtime_evidence["session_mapping"],
        "duration_ms": duration_ms,
        "provenance": {
            "installed_plugin": True,
            "skill_path": str(skill),
            "plugin_manifest": str(manifest),
            "plugin_version": json.loads(manifest.read_text(encoding="utf-8"))[
                "version"
            ],
            "repository_commit": _git_commit(REPOSITORY_ROOT),
            "gepa_commit": _installed_vcs_commit("gepa"),
        },
        "hashes": {name: _sha256(path) for name, path in source_files.items()},
    }
    receipt_path = output_dir / f"{engine}-receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    _atomic_receipt(receipt_path, receipt)
    if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("persisted receipt does not match the completed run")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("in-process smoke requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    print(json.dumps(run_smoke(args.engine, args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
