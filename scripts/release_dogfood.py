from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable


ENGINES = frozenset({"autoresearch", "meta_harness"})
HOST_TIMEOUT_SECONDS = 600
SEED_CANDIDATE = "Return RED."
TARGET_CANDIDATE = "Return BLUE."
TARGET_MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "high"
REQUIRED_INVOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "upstream_session_id",
        "codex_thread_id",
        "resume",
        "source_model",
        "target_model",
        "reasoning_effort",
        "return_code",
        "terminal_status",
        "usage",
        "estimated_cost_usd",
        "cost_status",
        "duration_ms",
    }
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
        "retry_count": 0,
        "proposer_iterations": 1,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {name}")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"invalid {name}")
    return numeric


def _read_invocation(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid invocation JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid invocation record: {path}")
    missing = REQUIRED_INVOCATION_FIELDS - set(payload)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"missing required field in invocation record: {names}")
    if payload["schema_version"] != 1:
        raise ValueError("unsupported invocation schema_version")
    if payload["terminal_status"] != "completed":
        raise ValueError("invocation terminal status is not completed")
    if payload["return_code"] != 0:
        raise ValueError("invocation return code is not zero")
    if payload["target_model"] != TARGET_MODEL:
        raise ValueError("invocation target model does not match release policy")
    if payload["reasoning_effort"] != REASONING_EFFORT:
        raise ValueError("invocation reasoning effort does not match release policy")
    if (
        not isinstance(payload["upstream_session_id"], str)
        or not payload["upstream_session_id"]
    ):
        raise ValueError("invalid upstream_session_id")
    if (
        not isinstance(payload["codex_thread_id"], str)
        or not payload["codex_thread_id"]
    ):
        raise ValueError("invalid codex_thread_id")
    if not isinstance(payload["resume"], bool):
        raise ValueError("invalid resume")
    if not isinstance(payload["usage"], dict):
        raise ValueError("invalid usage")
    for name in ("input_tokens", "output_tokens"):
        if name not in payload["usage"]:
            raise ValueError(f"missing required usage field: {name}")
    for name, value in payload["usage"].items():
        _nonnegative_integer(value, f"usage.{name}")
    if payload["usage"]["input_tokens"] + payload["usage"]["output_tokens"] <= 0:
        raise ValueError("invocation usage is empty")
    if _nonnegative_number(payload["estimated_cost_usd"], "estimated_cost_usd") <= 0:
        raise ValueError("invocation estimated_cost_usd is empty")
    if not isinstance(payload["cost_status"], str) or not payload["cost_status"]:
        raise ValueError("invalid cost_status")
    _nonnegative_integer(payload["duration_ms"], "duration_ms")
    return payload


def invocation_records(state_dir: Path) -> list[dict[str, Any]]:
    invocation_dir = state_dir / "invocations"
    if not invocation_dir.is_dir():
        raise ValueError("missing invocation evidence directory")
    records = [_read_invocation(path) for path in sorted(invocation_dir.glob("*.json"))]
    if len(records) != 1:
        raise ValueError("release evidence must contain exactly one invocation")
    return records


def require_unique_state_dir(state_dir: Path) -> None:
    invocation_dir = state_dir / "invocations"
    if invocation_dir.exists() and any(invocation_dir.iterdir()):
        raise ValueError("CODEX_ADAPTER_STATE_DIR must be unique and unused")
    state_dir.mkdir(parents=True, exist_ok=True)


def _returned_cost(metadata: object, name: str) -> float:
    if not isinstance(metadata, dict) or name not in metadata:
        raise ValueError(f"GEPA result is missing {name}")
    return _nonnegative_number(metadata[name], name)


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


def _required_hashes(paths: dict[str, Path]) -> dict[str, str]:
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise ValueError(f"missing required custody files: {', '.join(missing)}")
    return {name: sha256_file(path) for name, path in paths.items()}


def build_receipt(
    *,
    engine: str,
    state_dir: Path,
    manifest_path: Path,
    source: dict[str, Any],
    result: dict[str, Any],
    summary: dict[str, Any],
    file_paths: dict[str, Path],
    commits: dict[str, str],
    version: str,
) -> dict[str, Any]:
    records = invocation_records(state_dir)
    usage: dict[str, int] = {}
    for record in records:
        for name, value in record["usage"].items():
            usage[name] = usage.get(name, 0) + _nonnegative_integer(
                value, f"usage.{name}"
            )
    estimated_cost = sum(
        _nonnegative_number(record["estimated_cost_usd"], "estimated_cost_usd")
        for record in records
    )
    metadata = result.get("metadata")
    adapter_cost = _returned_cost(metadata, "adapter_cost")
    returned_total = _returned_cost(metadata, "total_cost")
    summary_adapter = _nonnegative_number(
        summary.get("adapter_cost_usd"), "summary.adapter_cost_usd"
    )
    summary_eval = _nonnegative_number(
        summary.get("eval_cost_usd"), "summary.eval_cost_usd"
    )
    summary_total_usd = _nonnegative_number(
        summary.get("total_cost_usd"), "summary.total_cost_usd"
    )
    summary_total = _nonnegative_number(summary.get("total_cost"), "summary.total_cost")
    if (
        not isinstance(summary.get("adapter_cost_status"), str)
        or not summary["adapter_cost_status"]
    ):
        raise ValueError("summary is missing adapter_cost_status")
    if summary["adapter_cost_status"] != metadata.get("adapter_cost_status"):
        raise ValueError("returned and persisted adapter cost status do not agree")
    if records[0]["cost_status"] != metadata.get("adapter_cost_status"):
        raise ValueError("journal and returned adapter cost status do not agree")
    agreement = all(
        math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12)
        for left, right in (
            (estimated_cost, adapter_cost),
            (adapter_cost, summary_adapter),
            (returned_total, summary_total_usd),
            (returned_total, summary_total),
            (summary_total_usd, summary_eval + summary_adapter),
        )
    )
    if not agreement:
        raise ValueError("journal, returned, and persisted costs do not agree")
    return {
        "schema_version": 1,
        "status": "success",
        "engine": engine,
        "policy": release_policy(engine),
        "commits": commits,
        "version": version,
        "source": source,
        "result": {key: value for key, value in result.items() if key != "metadata"},
        "usage": usage,
        "cost": {
            "estimated_usd": estimated_cost,
            "adapter_reported_usd": adapter_cost,
            "eval_usd": summary_eval,
            "total_usd": returned_total,
            "cost_status": records[0]["cost_status"],
            "agreement": agreement,
        },
        "sandbox": {"enabled": True, "runtime": "bubblewrap"},
        "session_mapping": [
            {
                "upstream_session_id": record["upstream_session_id"],
                "codex_thread_id": record["codex_thread_id"],
                "resume": record["resume"],
            }
            for record in records
        ],
        "file_hashes": _required_hashes(file_paths),
    }


def persist_receipt(output_dir: Path, receipt: dict[str, Any]) -> Path:
    path = output_dir / "release_receipt.json"
    _atomic_json(path, receipt)
    return path


def _git_commit(path: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if completed.returncode != 0 or len(commit) != 40:
        raise RuntimeError(f"cannot resolve git commit for {path}")
    return commit


def _optional_git_commit(path: Path) -> str | None:
    try:
        return _git_commit(_repository_root(path))
    except RuntimeError:
        return None


def _repository_root(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError(f"no repository root for {path}")


def skill_dir() -> Path:
    configured = os.environ.get("GEPA_CODEX_SKILL_DIR")
    fallback = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "gepa-optimize-anything"
        / "skills"
        / "gepa-optimize-anything-codex"
    )
    selected = Path(configured).expanduser().resolve() if configured else fallback
    if not (selected / "SKILL.md").is_file():
        raise RuntimeError("GEPA Codex skill directory is missing SKILL.md")
    return selected


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
    paths = runtime.stage_runtime(runtime.runtime_paths(state_dir=state_dir))
    probe = runtime.probe_runtime(paths)
    if probe.returncode != 0:
        raise RuntimeError(
            (probe.stderr or probe.stdout or "sandbox probe failed").strip()
        )
    environment = runtime.runtime_environment(paths)
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
        "state_dir": str(paths.state_dir),
    }
    return environment, evidence


def _evaluate(candidate: str, _example: object) -> tuple[float, dict[str, Any]]:
    passed = TARGET_CANDIDATE in candidate
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


def _session_matches(path: Path, record: dict[str, Any]) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid session mapping record") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("upstream_session_id") != record["upstream_session_id"]
        or payload.get("thread_id") != record["codex_thread_id"]
    ):
        raise ValueError("session mapping does not match invocation evidence")


def _valid_result(result: dict[str, Any]) -> None:
    if TARGET_CANDIDATE not in result["best_candidate"]:
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
        "schema_version": 1,
        "status": status,
        "engine": engine,
        "policy": release_policy(engine),
        "error_code": code,
    }
    if source is not None:
        receipt["source"] = source
        receipt["sandbox"] = {"enabled": True, "runtime": source.get("sandbox_runtime")}
    path = persist_receipt(root / "output", receipt)
    receipt["receipt_path"] = str(path)
    _atomic_json(path, receipt)
    return receipt, path


def run_release(
    engine: str, base_dir: Path | None = None
) -> tuple[dict[str, Any], Path]:
    root, state_dir = _new_paths(base_dir)
    root.mkdir(parents=True, exist_ok=False)
    source: dict[str, Any] | None = None
    try:
        require_unique_state_dir(state_dir)
        skill = skill_dir()
        environment, staged = stage_and_preflight(skill, engine, state_dir)
        from gepa import __version__ as gepa_version
        import gepa

        plugin = skill.parents[1] / ".codex-plugin" / "plugin.json"
        plugin_data = json.loads(plugin.read_text(encoding="utf-8"))
        skill_version = plugin_data.get("version")
        if not isinstance(skill_version, str) or not skill_version:
            raise ValueError("selected plugin version is missing")
        gepa_module = Path(gepa.__file__).resolve()
        source = {
            "gepa_module": str(gepa_module),
            "gepa_version": gepa_version,
            "gepa_commit": _git_commit(_repository_root(gepa_module)),
            "skill_path": str(skill),
            "skill_version": skill_version,
            "skill_commit": _optional_git_commit(skill),
            "skill_source": "environment"
            if "GEPA_CODEX_SKILL_DIR" in os.environ
            else "repo_fallback",
            **staged,
        }
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
        summary = _summary(summary_path)
        records = invocation_records(state_dir)
        sessions = sorted((state_dir / "sessions").glob("*.json"))
        if len(sessions) != 1:
            raise ValueError(
                "release evidence must contain exactly one session mapping"
            )
        _session_matches(sessions[0], records[0])
        files = {
            "plugin_json": plugin,
            "skill": skill / "SKILL.md",
            "launcher": Path(staged["launcher"]),
            "adapter": Path(staged["adapter_module"]),
            "summary": summary_path,
            "invocation": next((state_dir / "invocations").glob("*.json")),
            "session": sessions[0],
            "manifest": manifest,
        }
        receipt = build_receipt(
            engine=engine,
            state_dir=state_dir,
            manifest_path=manifest,
            source=source,
            result=result,
            summary=summary,
            file_paths=files,
            commits={
                "runner": _git_commit(_repository_root(Path(__file__))),
                "gepa": source["gepa_commit"],
                "selected_skill": source["skill_commit"],
            },
            version=str(source["skill_version"]),
        )
        path = persist_receipt(root / "output", receipt)
        receipt["receipt_path"] = str(path)
        _atomic_json(path, receipt)
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
    if os.environ.get("RUN_CODEX_AGENT_SMOKE") != "1":
        print("release dogfood requires RUN_CODEX_AGENT_SMOKE=1", file=sys.stderr)
        return 2
    receipt, _receipt_path = run_release(args.engine)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
