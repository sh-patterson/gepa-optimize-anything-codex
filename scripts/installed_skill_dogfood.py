from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from release_dogfood import (  # noqa: E402
    API_KEY_NAMES,
    REASONING_EFFORT,
    TARGET_MODEL,
    authentication_mode,
    installed_skill_path,
    require_unique_state_dir,
    stage_and_preflight,
)
from release_evidence import (  # noqa: E402
    hash_files,
    installed_provenance,
    public_receipt,
    read_adapter_evidence,
    write_verified_receipt,
)


HOST_TIMEOUT_SECONDS = 600
MINIMUM_SCORE = 0.75
MAX_EVALS = 3
MAX_CANDIDATES = 2
SEED = '{"routes":[],"default":"general"}'
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPOSITORY_ROOT / "release" / "fixtures"
ROUTING_CASES_FIXTURE = FIXTURES_DIR / "support_routing_cases.json"
TASK_FIXTURE = FIXTURES_DIR / "support_routing_task.py"


def _write_fixture(root: Path) -> tuple[Path, Path]:
    fixture = root / ROUTING_CASES_FIXTURE.name
    task = root / TASK_FIXTURE.name
    shutil.copyfile(ROUTING_CASES_FIXTURE, fixture)
    shutil.copyfile(TASK_FIXTURE, task)
    return fixture, task


def _task_fixture(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("invalid routing evaluator fixture")
    return cases


def _project_version() -> str:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _load_module(name: str, path: Path) -> object:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _outer_command(
    codex: str, work_dir: Path, task: Path, runtime_cache: Path
) -> list[str]:
    prompt = (
        "Use $gepa-optimize-anything:gepa-optimize-anything-codex to complete "
        "the installed-plugin dogfood in this directory. Invoke the installed skill, "
        f"then run exactly one command: {sys.executable} {task}. Do not create, edit, "
        "or repair any file yourself; the provided program is the sole artifact writer. "
        "Report its terminal outcome without substituting your own result."
    )
    return [
        codex,
        "exec",
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "-m",
        TARGET_MODEL,
        "-c",
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        "sandbox_workspace_write.writable_roots="
        + json.dumps([str(runtime_cache)]),
        "-c",
        "features.standalone_web_search=false",
        "--sandbox",
        "workspace-write",
        "-C",
        str(work_dir),
        prompt,
    ]


def _run_outer_codex(
    command: list[str], environment: dict[str, str], adapter: object
) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(timeout=HOST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise TimeoutError("installed skill dogfood exceeded host timeout") from exc
    terminal = adapter.parse_codex_output(stdout)
    if process.returncode != 0 or terminal.status != "completed":
        raise RuntimeError("outer Codex invocation did not complete terminally")
    if not terminal.thread_id:
        raise RuntimeError("outer Codex invocation has no thread mapping")
    usage = terminal.usage
    if (
        not isinstance(usage, dict)
        or usage.get("input_tokens", 0) + usage.get("output_tokens", 0) <= 0
    ):
        raise RuntimeError("outer Codex invocation has no usage")
    return {
        "thread_id": terminal.thread_id,
        "terminal_status": terminal.status,
        "return_code": process.returncode,
        "usage": usage,
        "estimated_cost_usd": adapter.estimated_luna_cost(usage),
    }


def _load_result(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("missing or invalid dogfood result") from exc
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise ValueError("dogfood result is not terminally successful")
    return payload


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_result(result: dict[str, Any]) -> None:
    seed_score = result.get("seed_score")
    best_score = result.get("best_score")
    total_evals = result.get("total_evals")
    if (
        isinstance(seed_score, bool)
        or not isinstance(seed_score, (int, float))
        or not math.isfinite(seed_score)
        or seed_score != 0.125
    ):
        raise ValueError("dogfood seed score is invalid")
    if (
        isinstance(best_score, bool)
        or not isinstance(best_score, (int, float))
        or not math.isfinite(best_score)
        or best_score < MINIMUM_SCORE
        or best_score <= seed_score
    ):
        raise ValueError("dogfood did not improve to the required score")
    if (
        isinstance(total_evals, bool)
        or not isinstance(total_evals, int)
        or not 2 <= total_evals <= MAX_EVALS
    ):
        raise ValueError("dogfood evaluation count is outside policy")
    if result.get("best_candidate_valid_json") is not True:
        raise ValueError("dogfood winner is not valid JSON")
    if not _valid_hash(result.get("best_candidate_sha256")):
        raise ValueError("dogfood winner hash is invalid")
    hashes = result.get("candidate_hashes")
    if (
        not isinstance(hashes, list)
        or len(hashes) < MAX_CANDIDATES
        or len(set(hashes)) < MAX_CANDIDATES
        or any(not _valid_hash(value) for value in hashes)
    ):
        raise ValueError("dogfood did not produce two distinct candidates")
    trace = result.get("score_trace")
    if not isinstance(trace, list) or len(trace) < 2:
        raise ValueError("dogfood score trace is incomplete")
    for item in trace:
        if (
            not isinstance(item, dict)
            or set(item) != {"candidate_sha256", "score", "valid_json"}
            or not _valid_hash(item["candidate_sha256"])
            or isinstance(item["score"], bool)
            or not isinstance(item["score"], (int, float))
            or not 0 <= item["score"] <= 1
            or not isinstance(item["valid_json"], bool)
        ):
            raise ValueError("dogfood score trace is invalid")


def _receipt(
    *,
    root: Path,
    skill: Path,
    source: dict[str, Any],
    result: dict[str, Any],
    outer: dict[str, Any],
    inner: dict[str, Any],
    inner_usage: dict[str, int],
    inner_cost_usd: float,
    evidence_files: dict[str, Path],
    fixture: Path,
    task: Path,
) -> dict[str, Any]:
    manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
    files = {
        "plugin_manifest": manifest,
        "skill": skill / "SKILL.md",
        "runner": Path(__file__).resolve(),
        "task_runner": task,
        "evaluator_fixture": fixture,
        **evidence_files,
    }
    return {
        "schema_version": 2,
        "status": "success",
        "proof": "installed_skill_dogfood",
        "engine": "meta_harness",
        "policy": {
            "target_model": TARGET_MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "sandbox": True,
            "max_evals": MAX_EVALS,
            "stop_at_score": 1.0,
            "max_iterations": 1,
            "max_candidates_per_iteration": MAX_CANDIDATES,
            "max_adapter_invocations": 1,
            "pre_submission_retries": 0,
            "host_timeout_seconds": HOST_TIMEOUT_SECONDS,
        },
        "authentication": {
            "mode": "chatgpt_login",
            "child_api_keys_present": False,
            "credentials_recorded": False,
        },
        "provenance": source,
        "result": {
            "seed_score": result["seed_score"],
            "best_score": result["best_score"],
            "total_evals": result["total_evals"],
            "best_candidate_valid_json": result["best_candidate_valid_json"],
            "best_candidate_sha256": result["best_candidate_sha256"],
            "candidate_hashes": result["candidate_hashes"],
            "score_trace": result["score_trace"],
        },
        "usage": {
            "outer_codex": outer["usage"],
            "optimizer_codex": inner_usage,
        },
        "cost": {
            "outer_estimated_usd": outer["estimated_cost_usd"],
            "optimizer_estimated_usd": inner_cost_usd,
            "status": inner["cost_status"],
            "source": "codex_terminal_usage_and_adapter_invocation_journal",
        },
        "sandbox": {
            "outer": "codex_workspace_write",
            "optimizer": "bubblewrap",
            "probe_success": source["probe_success"],
        },
        "terminal": {
            "outer": {
                "status": outer["terminal_status"],
                "return_code": outer["return_code"],
                "thread_id": outer["thread_id"],
            },
            "optimizer": {
                "status": inner["terminal_status"],
                "return_code": inner["return_code"],
                "ambiguous_retry": False,
            },
        },
        "session_mapping": {
            "upstream_session_id": inner["upstream_session_id"],
            "codex_thread_id": inner["codex_thread_id"],
            "resume": inner["resume"],
        },
        "hashes": hash_files(files),
        "receipt_path": str(root / "installed-skill-dogfood-receipt.json"),
    }


def run_dogfood(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("dogfood output directory must be new")
    output_dir.mkdir(parents=True)
    skill = installed_skill_path()
    provenance = installed_provenance(skill, REPOSITORY_ROOT, _project_version())
    state_dir = (
        Path(os.environ["HOME"]).expanduser()
        / ".cache"
        / "gepa-optimize-anything-codex"
        / "runs"
        / f"installed-skill-{uuid.uuid4().hex}"
    )
    require_unique_state_dir(state_dir)
    environment, staged = stage_and_preflight(skill, "meta_harness", state_dir)
    if (
        environment.get("CODEX_ADAPTER_MAX_INVOCATIONS") != "1"
        or environment.get("CODEX_ADAPTER_PRE_SUBMISSION_RETRIES") != "0"
    ):
        raise ValueError("installed skill dogfood runtime policy is not fail-closed")
    if authentication_mode(environment) != "chatgpt_login":
        raise ValueError("installed skill dogfood requires staged ChatGPT login")
    if any(environment.get(name) for name in API_KEY_NAMES):
        raise ValueError("installed skill dogfood cannot expose API keys")
    fixture, task = _write_fixture(output_dir)
    _task_fixture(fixture)
    result_path = output_dir / "dogfood-result.json"
    environment.update(
        {
            "DOGFOOD_FIXTURE": str(fixture),
            "DOGFOOD_RESULT_PATH": str(result_path),
            "DOGFOOD_WORK_DIR": str(output_dir),
        }
    )
    adapter = _load_module(
        "installed_skill_dogfood_adapter",
        skill / "scripts" / "codex_claude_adapter.py",
    )
    started = time.monotonic()
    outer = _run_outer_codex(
        _outer_command(
            environment["CODEX_CLI"],
            output_dir,
            task,
            state_dir.parents[1],
        ),
        environment,
        adapter,
    )
    outer_terminal = output_dir / "outer-terminal.json"
    write_verified_receipt(outer_terminal, outer)
    evidence = read_adapter_evidence(
        state_dir,
        target_model=TARGET_MODEL,
        reasoning_effort=REASONING_EFFORT,
        expected_invocations=1,
    )
    inner = evidence.mappings[0]
    if inner["resume"]:
        raise ValueError("dogfood inner invocation unexpectedly resumed")
    evidence_files = {
        "invocation": evidence.invocation_paths[0],
        "session": evidence.session_paths[0],
    }
    evidence_files["outer_terminal"] = outer_terminal
    result = _load_result(result_path)
    _validate_result(result)
    source = {
        **provenance,
        "gepa_version": importlib.metadata.version("gepa"),
        **staged,
    }
    receipt = _receipt(
        root=output_dir,
        skill=skill,
        source=source,
        result=result,
        outer=outer,
        inner=inner,
        inner_usage=evidence.usage,
        inner_cost_usd=evidence.estimated_cost_usd,
        evidence_files=evidence_files,
        fixture=fixture,
        task=task,
    )
    receipt["duration_ms"] = round((time.monotonic() - started) * 1000)
    receipt_path = output_dir / "installed-skill-dogfood-receipt.json"
    receipt = public_receipt(receipt)
    write_verified_receipt(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("installed skill dogfood requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    try:
        receipt = run_dogfood(args.output_dir)
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if args.output_dir.is_dir():
            failure = {
                "schema_version": 2,
                "status": "error",
                "proof": "installed_skill_dogfood",
                "error_type": type(exc).__name__,
            }
            write_verified_receipt(
                args.output_dir / "installed-skill-dogfood-receipt.json",
                failure,
            )
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
