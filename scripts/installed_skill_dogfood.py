from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "installed_skill_release_dogfood", SCRIPT_DIR / "release_dogfood.py"
)
if _RELEASE_SPEC is None or _RELEASE_SPEC.loader is None:
    raise RuntimeError("cannot load release_dogfood.py")
release = importlib.util.module_from_spec(_RELEASE_SPEC)
sys.modules[_RELEASE_SPEC.name] = release
_RELEASE_SPEC.loader.exec_module(release)


HOST_TIMEOUT_SECONDS = 600
MINIMUM_SCORE = 0.75
MAX_EVALS = 3
MAX_CANDIDATES = 2
SEED = '{"routes":[],"default":"general"}'
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

ROUTING_CASES = [
    {"id": "billing_duplicate", "message": "I was charged twice", "route": "billing"},
    {"id": "billing_invoice", "message": "Where is my invoice?", "route": "billing"},
    {"id": "support_refund", "message": "I need a refund", "route": "support"},
    {
        "id": "support_cancel",
        "message": "Cancel and refund my plan",
        "route": "support",
    },
    {
        "id": "security_password",
        "message": "Reset my password",
        "route": "account_security",
    },
    {
        "id": "security_login",
        "message": "My login code never arrived",
        "route": "account_security",
    },
    {
        "id": "technical_crash",
        "message": "The app crashes on startup",
        "route": "technical",
    },
    {"id": "general_hours", "message": "What are your hours?", "route": "general"},
]

TASK_SOURCE = r"""
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


def score_candidate(candidate: str, cases: list[dict[str, str]]) -> tuple[float, list[str], bool]:
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
"""


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    release._atomic_json(path, payload)


def _write_fixture(root: Path) -> tuple[Path, Path]:
    fixture = root / "routing-cases.json"
    _atomic_json(fixture, {"cases": ROUTING_CASES})
    task = root / "routing_dogfood_task.py"
    task.write_text(textwrap.dedent(TASK_SOURCE).lstrip(), encoding="utf-8")
    return fixture, task


def _task_fixture(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("invalid routing evaluator fixture")
    return cases


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
        release.TARGET_MODEL,
        "-c",
        f'model_reasoning_effort="{release.REASONING_EFFORT}"',
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


def _inner_evidence(state_dir: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    records = release.invocation_records(state_dir)
    record = records[0]
    if record["resume"]:
        raise ValueError("dogfood inner invocation unexpectedly resumed")
    sessions = sorted((state_dir / "sessions").glob("*.json"))
    if len(sessions) != 1:
        raise ValueError("dogfood requires one inner session mapping")
    release._session_matches(sessions[0], record)
    invocation = next((state_dir / "invocations").glob("*.json"))
    return record, {"invocation": invocation, "session": sessions[0]}


def _source(skill: Path, staged: dict[str, Any]) -> dict[str, Any]:
    import importlib.metadata

    manifest = skill.parents[1] / ".codex-plugin" / "plugin.json"
    plugin = json.loads(manifest.read_text(encoding="utf-8"))
    return {
        "installed_plugin": True,
        "skill_path": str(skill),
        "plugin_manifest": str(manifest),
        "plugin_version": plugin["version"],
        "repository_commit": release._git_commit(REPOSITORY_ROOT),
        "gepa_version": importlib.metadata.version("gepa"),
        "gepa_commit": release._installed_vcs_commit("gepa"),
        **staged,
    }


def _receipt(
    *,
    root: Path,
    skill: Path,
    source: dict[str, Any],
    result: dict[str, Any],
    outer: dict[str, Any],
    inner: dict[str, Any],
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
            "target_model": release.TARGET_MODEL,
            "reasoning_effort": release.REASONING_EFFORT,
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
            "optimizer_codex": inner["usage"],
        },
        "cost": {
            "outer_estimated_usd": outer["estimated_cost_usd"],
            "optimizer_estimated_usd": inner["estimated_cost_usd"],
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
        "hashes": {name: release.sha256_file(path) for name, path in files.items()},
        "receipt_path": str(root / "installed-skill-dogfood-receipt.json"),
    }


def run_dogfood(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("dogfood output directory must be new")
    output_dir.mkdir(parents=True)
    skill = release.skill_dir()
    state_dir = (
        Path(os.environ["HOME"]).expanduser()
        / ".cache"
        / "gepa-optimize-anything-codex"
        / "runs"
        / f"installed-skill-{uuid.uuid4().hex}"
    )
    release.require_unique_state_dir(state_dir)
    environment, staged = release.stage_and_preflight(skill, "meta_harness", state_dir)
    if (
        environment.get("CODEX_ADAPTER_MAX_INVOCATIONS") != "1"
        or environment.get("CODEX_ADAPTER_PRE_SUBMISSION_RETRIES") != "0"
    ):
        raise ValueError("installed skill dogfood runtime policy is not fail-closed")
    if release.authentication_mode(environment) != "chatgpt_login":
        raise ValueError("installed skill dogfood requires staged ChatGPT login")
    if any(environment.get(name) for name in release.API_KEY_NAMES):
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
    adapter = release._load_module(
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
    _atomic_json(outer_terminal, outer)
    inner, evidence_files = _inner_evidence(state_dir)
    evidence_files["outer_terminal"] = outer_terminal
    result = _load_result(result_path)
    _validate_result(result)
    source = _source(skill, staged)
    receipt = _receipt(
        root=output_dir,
        skill=skill,
        source=source,
        result=result,
        outer=outer,
        inner=inner,
        evidence_files=evidence_files,
        fixture=fixture,
        task=task,
    )
    receipt["duration_ms"] = round((time.monotonic() - started) * 1000)
    receipt_path = Path(receipt["receipt_path"])
    _atomic_json(receipt_path, receipt)
    if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
        raise RuntimeError("persisted dogfood receipt does not match the run")
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
            _atomic_json(
                args.output_dir / "installed-skill-dogfood-receipt.json",
                failure,
            )
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
