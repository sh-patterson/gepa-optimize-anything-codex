from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
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
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_evidence import (  # noqa: E402
    hash_files,
    installed_provenance,
    public_receipt,
    read_adapter_evidence,
    write_verified_receipt,
)


def installed_skill_path() -> Path:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    if not configured:
        raise RuntimeError("in-process smoke requires GEPA_CODEX_SKILL_DIR")
    return Path(configured).expanduser().resolve()


def _project_version() -> str:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


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
    for name, token_count in value.items():
        if (
            isinstance(token_count, bool)
            or not isinstance(token_count, int)
            or token_count < 0
        ):
            raise RuntimeError(f"optimizer usage is invalid {name}")
        usage[name] = token_count
    if (
        not all(name in usage for name in ("input_tokens", "output_tokens"))
        or usage["input_tokens"] + usage["output_tokens"] <= 0
    ):
        raise RuntimeError("optimizer usage is empty")
    return usage


def _codex_usage(lms: list[object]) -> dict[str, int]:
    usage: dict[str, int] = {}
    for lm in lms:
        lm_usage = getattr(lm, "total_usage", None)
        if not isinstance(lm_usage, dict):
            raise RuntimeError("optimizer usage is missing")
        for name, value in _usage(lm_usage).items():
            usage[name] = usage.get(name, 0) + value
    return _usage(usage)


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
    paths = runtime.stage_runtime(runtime.runtime_paths())
    state_dir = runtime.resolve_state_dir(paths, state_dir)
    probe = runtime.probe_runtime(paths, state_dir)
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout or "Bubblewrap runtime probe failed").strip()
        )
    environment = runtime.runtime_environment(paths, state_dir)
    environment.update(
        {
            "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
            "CODEX_ADAPTER_MAX_INVOCATIONS": str(MAX_ADAPTER_INVOCATIONS),
            "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
        }
    )
    return {
        "driver": driver,
        "launcher": paths.launcher,
        "adapter": paths.adapter_module,
        "environment": environment,
        "state_dir": state_dir,
        "work_dir": work_dir,
        "probe_success": True,
    }


def _run_codex(
    engine: str, values: dict[str, Any], runtime: dict[str, Any]
) -> tuple[object, dict[str, int], list[object]]:
    from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

    lms: list[object] = []

    def new_lm(model: str, **kwargs: Any) -> object:
        if model != MODEL:
            raise ValueError(f"unexpected in-process model: {model}")
        state_dir = runtime["state_dir"] / f"{engine}-{uuid4().hex}"
        config = runtime["driver"].CodexLMConfig(
            executable=runtime["launcher"],
            model=MODEL,
            reasoning_effort=REASONING_EFFORT,
            sandbox_mode="workspace-write",
            state_dir=state_dir,
            session_dir=runtime["state_dir"] / f"{engine}-sessions-{uuid4().hex}",
            timeout_seconds=600,
            retry_ceiling=0,
        )
        environment = dict(runtime["environment"])
        environment["CODEX_ADAPTER_STATE_DIR"] = str(state_dir)
        lm = runtime["driver"].CodexLM(
            config,
            cwd=runtime["work_dir"],
            environment=environment,
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
    return raw, usage, lms


def run_smoke(
    engine: str,
    output_dir: Path,
    *,
    optimize: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    skill = installed_skill_path()
    provenance = installed_provenance(skill, REPOSITORY_ROOT, _project_version())
    manifest = Path(provenance["plugin_manifest"])
    output_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    values = config_for(engine, output_dir)
    if optimize is None:
        runtime = _prepare_codex_runtime(skill, output_dir)
        raw, usage, lms = _run_codex(engine, values, runtime)
        expected_invocations = sum(
            int(getattr(lm, "invocation_count", 0)) for lm in lms
        )
        driver_cost = sum(float(getattr(lm, "total_cost", 0.0)) for lm in lms)
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
            "state_dir": output_dir / "adapter-state",
            "probe_success": True,
        }
        expected_invocations = 1
        driver_cost = 0.001
    evidence = read_adapter_evidence(
        runtime["state_dir"],
        target_model=MODEL,
        reasoning_effort=REASONING_EFFORT,
        expected_invocations=expected_invocations,
    )
    if evidence.usage != usage:
        raise RuntimeError("adapter journal usage is inconsistent")
    if abs(driver_cost - evidence.estimated_cost_usd) > 1e-9:
        raise RuntimeError("adapter journal cost estimate is inconsistent")
    result = _result(raw)
    duration_ms = round((time.monotonic() - started) * 1000)
    source_files = {
        "skill": skill / "SKILL.md",
        "plugin_manifest": manifest,
        "runner": Path(__file__).resolve(),
        "codex_lm": skill / "scripts" / "codex_lm.py",
        "adapter": runtime["adapter"],
        **{
            f"invocation_{index}": path
            for index, path in enumerate(evidence.invocation_paths, start=1)
        },
        **{
            f"session_{index}": path
            for index, path in enumerate(evidence.session_paths, start=1)
        },
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
            "bubblewrap_probe_success": runtime["probe_success"],
        },
        "result": result,
        "usage": evidence.usage,
        "cost": {
            "estimated_usd": evidence.estimated_cost_usd,
            "status": "standard_tier_upper_estimate_from_observed_usage",
        },
        "invocation_count": len(evidence.invocation_paths),
        "session_mapping": [
            {
                "upstream_session_id": mapping["upstream_session_id"],
                "codex_thread_id": mapping["codex_thread_id"],
            }
            for mapping in evidence.mappings
        ],
        "duration_ms": duration_ms,
        "provenance": {
            **provenance,
        },
        "hashes": hash_files(source_files),
    }
    receipt_path = output_dir / f"{engine}-receipt.json"
    receipt["receipt_path"] = str(receipt_path)
    receipt = public_receipt(receipt)
    write_verified_receipt(receipt_path, receipt)
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
