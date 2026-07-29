from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any, Callable


ENGINES = frozenset({"autoresearch", "meta_harness"})
HOST_TIMEOUT_SECONDS = 600
SEED_CANDIDATE = "Return RED."
TARGET_TOKEN = "BLUE"
TARGET_MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "high"
MAX_ADAPTER_INVOCATIONS = 1
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
API_KEY_NAMES = ("CODEX_API_KEY", "OPENAI_API_KEY")
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_evidence import (  # noqa: E402
    hash_files,
    installed_provenance,
    read_adapter_evidence,
    write_verified_receipt,
)


def release_config(engine: str, root: Path) -> dict[str, Any]:
    if engine not in ENGINES:
        raise ValueError(f"unsupported engine: {engine}")
    engine_config: dict[str, Any] = {
        "model": TARGET_MODEL,
        "effort": REASONING_EFFORT,
    }
    if engine == "autoresearch":
        engine_config["ralph"] = False
        engine_config["max_no_eval_seconds"] = HOST_TIMEOUT_SECONDS
    else:
        engine_config["max_iterations"] = 1
        engine_config["max_candidates_per_iter"] = 1
    return {
        "engine": engine,
        "name": f"release-dogfood-{engine}-{root.name}",
        "max_evals": 3,
        "stop_at_score": 1.0,
        "sandbox": True,
        "run_dir": str(root / "run"),
        "output_dir": str(root / "output"),
        "engine_config": engine_config,
    }


def release_policy(engine: str) -> dict[str, Any]:
    return {
        "engine": engine,
        "target_model": TARGET_MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "sandbox": True,
        "max_evals": 3,
        "stop_at_score": 1.0,
        "host_timeout_seconds": HOST_TIMEOUT_SECONDS,
        "max_adapter_invocations": MAX_ADAPTER_INVOCATIONS,
        "retry_count": 0,
        "proposer_iterations": 1,
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def require_unique_state_dir(state_dir: Path) -> None:
    invocation_dir = state_dir / "invocations"
    if invocation_dir.exists() and any(invocation_dir.iterdir()):
        raise ValueError("CODEX_ADAPTER_STATE_DIR must be unique and unused")
    state_dir.mkdir(parents=True, exist_ok=True)


def _result_summary(result: object) -> dict[str, Any]:
    for name in ("best_candidate", "best_score", "total_evals", "metadata"):
        if not hasattr(result, name):
            raise ValueError(f"GEPA result is missing {name}")
    best_candidate = getattr(result, "best_candidate")
    best_score = getattr(result, "best_score")
    total_evals = getattr(result, "total_evals")
    if not isinstance(best_candidate, str) or not best_candidate:
        raise ValueError("GEPA result has no best candidate")
    if not isinstance(best_score, (int, float)) or not math.isfinite(best_score):
        raise ValueError("GEPA result has invalid best score")
    _nonnegative_integer(total_evals, "total_evals")
    return {
        "seed_candidate": SEED_CANDIDATE,
        "best_candidate": best_candidate,
        "best_score": float(best_score),
        "total_evals": total_evals,
        "metadata": getattr(result, "metadata"),
    }


def _project_version() -> str:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def installed_skill_path() -> Path:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    if not configured:
        raise RuntimeError("release dogfood requires GEPA_CODEX_SKILL_DIR")
    return Path(configured).expanduser().resolve()


def authentication_mode(environment: dict[str, str]) -> str:
    if environment.get("CODEX_API_KEY"):
        return "codex_api_key"
    return "chatgpt_login"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def stage_and_preflight(
    skill: Path, engine: str, state_dir: Path
) -> tuple[dict[str, str], dict[str, Any]]:
    runtime = _load_module(
        "release_dogfood_runtime", skill / "scripts" / "sandbox_runtime.py"
    )
    paths = runtime.stage_runtime(runtime.runtime_paths())
    state_dir = runtime.resolve_state_dir(paths, state_dir)
    probe = runtime.probe_runtime(paths, state_dir)
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout or "sandbox probe failed").strip()
        )
    environment = runtime.runtime_environment(paths, state_dir)
    environment["CODEX_ADAPTER_MAX_INVOCATIONS"] = str(MAX_ADAPTER_INVOCATIONS)
    environment["CODEX_ADAPTER_PRE_SUBMISSION_RETRIES"] = "0"
    mode = authentication_mode(environment)
    if mode == "chatgpt_login":
        present = [name for name in API_KEY_NAMES if environment.get(name)]
        if present:
            raise RuntimeError("staged-login proof cannot expose API keys")
        environment["CODEX_ADAPTER_AUTH_MODE"] = mode
    preflight = subprocess.run(
        [sys.executable, str(skill / "scripts" / "preflight.py"), "--engine", engine],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if preflight.returncode != 0:
        raise RuntimeError(
            (preflight.stderr or preflight.stdout or "preflight failed").strip()
        )
    evidence = {
        "sandbox_runtime": "bubblewrap",
        "probe_success": True,
        "stage_bin": str(paths.stage_bin),
        "launcher": str(paths.launcher),
        "adapter_module": str(paths.adapter_module),
        "codex": str(paths.codex),
        "codex_home": str(paths.codex_home),
        "state_dir": str(state_dir),
    }
    return environment, evidence


def _evaluate(candidate: str, _example: object) -> tuple[float, dict[str, Any]]:
    passed = TARGET_TOKEN in candidate
    return float(passed), {
        "passed": passed,
        "feedback": "Candidate must contain BLUE." if not passed else "Passed.",
    }


def _optimization_child(
    config_values: dict[str, Any], result_path: str, environment: dict[str, str]
) -> None:
    os.setsid()
    os.environ.update(environment)
    try:
        from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

        raw = optimize_anything(
            seed_candidate=SEED_CANDIDATE,
            evaluator=_evaluate,
            dataset=[{"case": "exact-token"}],
            objective="Write a candidate containing BLUE.",
            background="The candidate must satisfy the deterministic evaluator.",
            config=OptimizeAnythingConfig(**config_values),
        )
        _atomic_json(
            Path(result_path), {"status": "success", "result": _result_summary(raw)}
        )
    except BaseException as exc:
        _atomic_json(
            Path(result_path),
            {"status": "error", "error_type": type(exc).__name__},
        )
        raise


def supervise(
    target: Callable[..., None], args: tuple[Any, ...], timeout: int
) -> dict[str, Any]:
    if os.name != "posix":
        raise RuntimeError("release dogfood requires POSIX process groups")
    process = multiprocessing.get_context("fork").Process(target=target, args=args)
    process.start()
    process.join(timeout)
    if process.is_alive():
        os.killpg(process.pid, signal.SIGTERM)
        process.join(2)
        if process.is_alive():
            os.killpg(process.pid, signal.SIGKILL)
            process.join()
        return {"status": "timeout", "pid": process.pid}
    return {"status": "exited", "pid": process.pid, "returncode": process.exitcode}


def _new_paths(base_dir: Path | None) -> tuple[Path, Path]:
    base = base_dir or (
        Path.home() / ".cache" / "gepa-optimize-anything-codex" / "runs"
    )
    root = base / f"release-dogfood-{uuid.uuid4().hex}"
    return root, root / "adapter-state"


def _summary(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("missing or invalid output summary.json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid output summary.json")
    return payload


def _valid_result(result: dict[str, Any]) -> None:
    if TARGET_TOKEN not in result["best_candidate"]:
        raise ValueError("best candidate does not contain BLUE")
    if result["best_score"] != 1.0:
        raise ValueError("best score is not 1.0")
    if not 2 <= result["total_evals"] <= 3:
        raise ValueError("total evals are outside release bounds")


def _failure_receipt(
    engine: str,
    root: Path,
    code: str,
    status: str = "error",
    source: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    receipt = {
        "schema_version": 2,
        "status": status,
        "engine": engine,
        "policy": release_policy(engine),
        "error_code": code,
    }
    if source is not None:
        receipt["source"] = source
        receipt["sandbox"] = {"enabled": True, "runtime": source.get("sandbox_runtime")}
    path = root / "output" / "release_receipt.json"
    receipt["receipt_path"] = str(path)
    write_verified_receipt(path, receipt)
    return receipt, path


def run_release(
    engine: str, base_dir: Path | None = None
) -> tuple[dict[str, Any], Path]:
    root, state_dir = _new_paths(base_dir)
    root.mkdir(parents=True, exist_ok=False)
    source: dict[str, Any] | None = None
    try:
        skill = installed_skill_path()
        skill_version = _project_version()
        provenance = installed_provenance(skill, REPOSITORY_ROOT, skill_version)
        require_unique_state_dir(state_dir)
        environment, staged = stage_and_preflight(skill, engine, state_dir)
        import gepa

        plugin = skill.parents[1] / ".codex-plugin" / "plugin.json"
        gepa_module = Path(gepa.__file__).resolve()
        release_commit = provenance["repository_commit"]
        source = {
            "gepa_module": str(gepa_module),
            "gepa_version": importlib.metadata.version("gepa"),
            "gepa_commit": provenance["gepa_commit"],
            "skill_path": provenance["skill_path"],
            "skill_version": skill_version,
            "plugin_commit": release_commit,
            "skill_source": "installed_plugin",
            "plugin_manifest": provenance["plugin_manifest"],
            **staged,
        }
        auth_mode = authentication_mode(environment)
        if auth_mode != "chatgpt_login":
            raise ValueError("release proof requires staged ChatGPT login")
        manifest = root / "run_manifest.json"
        _atomic_json(manifest, {"policy": release_policy(engine), "source": source})
        result_path = root / "child_result.json"
        outcome = supervise(
            _optimization_child,
            (release_config(engine, root), str(result_path), environment),
            HOST_TIMEOUT_SECONDS,
        )
        if outcome["status"] == "timeout":
            return _failure_receipt(engine, root, "host_timeout", "timeout", source)
        child = _summary(result_path)
        if outcome.get("returncode") != 0 or child.get("status") != "success":
            return _failure_receipt(
                engine, root, "optimizer_child_failure", source=source
            )
        result = child["result"]
        _valid_result(result)
        summary_path = root / "output" / "summary.json"
        _summary(summary_path)
        evidence = read_adapter_evidence(
            state_dir,
            target_model=TARGET_MODEL,
            reasoning_effort=REASONING_EFFORT,
            expected_invocations=1,
        )
        files = {
            "plugin_json": plugin,
            "skill": skill / "SKILL.md",
            "launcher": Path(staged["launcher"]),
            "adapter": Path(staged["adapter_module"]),
            "summary": summary_path,
            "invocation": evidence.invocation_paths[0],
            "session": evidence.session_paths[0],
            "manifest": manifest,
        }
        if auth_mode not in {"chatgpt_login", "codex_api_key"}:
            raise ValueError("invalid authentication mode")
        receipt = {
            "schema_version": 2,
            "status": "success",
            "engine": engine,
            "policy": release_policy(engine),
            "commits": {
                "runner": release_commit,
                "gepa": source["gepa_commit"],
                "plugin": source["plugin_commit"],
            },
            "versions": {"plugin": source["skill_version"], "gepa": source["gepa_version"]},
            "provenance": {
                "installed_plugin": True,
                "skill_path": source["skill_path"],
                "plugin_manifest": source["plugin_manifest"],
            },
            "authentication": {
                "mode": auth_mode,
                "child_api_keys_present": False,
                "credentials_recorded": False,
            },
            "source": source,
            "result": {
                "improved": result["best_score"] == 1.0,
                "best_score": result["best_score"],
                "total_evals": result["total_evals"],
            },
            "usage": evidence.usage,
            "cost": {
                "estimated_usd": evidence.estimated_cost_usd,
                "cost_status": evidence.mappings[0]["cost_status"],
                "source": "codex_adapter_invocation_journal",
            },
            "sandbox": {"enabled": True, "runtime": "bubblewrap"},
            "terminal": {
                "status": evidence.mappings[0]["terminal_status"],
                "return_code": evidence.mappings[0]["return_code"],
                "ambiguous_retry": False,
            },
            "session_mapping": [
                {
                    "upstream_session_id": mapping["upstream_session_id"],
                    "codex_thread_id": mapping["codex_thread_id"],
                    "resume": mapping["resume"],
                }
                for mapping in evidence.mappings
            ],
            "file_hashes": hash_files(files),
        }
        path = root / "output" / "release_receipt.json"
        receipt["receipt_path"] = str(path)
        write_verified_receipt(path, receipt)
        return receipt, path
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return _failure_receipt(
            engine, root, "release_validation_failure", source=source
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True, choices=sorted(ENGINES))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("release dogfood requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    receipt, _receipt_path = run_release(args.engine)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
