from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
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
    authentication_mode,
    installed_skill_path,
    require_unique_state_dir,
)
from release_evidence import (  # noqa: E402
    hash_files,
    public_receipt,
    read_adapter_evidence,
    write_verified_receipt,
)


TARGET_MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "high"
HOST_TIMEOUT_SECONDS = 600
EXPECTED_RESULT = "ok"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_FILES = {
    ".codex-plugin/plugin.json": "plugins/gepa-optimize-anything/.codex-plugin/plugin.json",
    "skills/gepa-optimize-anything-codex/SKILL.md": "plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/SKILL.md",
    "skills/gepa-optimize-anything-codex/scripts/claude": "plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/scripts/claude",
    "skills/gepa-optimize-anything-codex/scripts/codex_claude_adapter.py": "plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/scripts/codex_claude_adapter.py",
    "skills/gepa-optimize-anything-codex/scripts/sandbox_runtime.py": "plugins/gepa-optimize-anything/skills/gepa-optimize-anything-codex/scripts/sandbox_runtime.py",
}


def _project_version() -> str:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("cannot resolve release runner commit")
    return completed.stdout.strip()


def _git_blob(commit: str, path: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "show", f"{commit}:{path}"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot read release blob: {path}")
    return completed.stdout


def installed_adapter_provenance(skill: Path, expected_commit: str) -> dict[str, Any]:
    if len(expected_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_commit
    ):
        raise ValueError("expected commit must be a lowercase SHA-1")
    if _git_head() != expected_commit:
        raise RuntimeError("release runner is not at the expected commit")
    plugin = skill.resolve().parents[1]
    try:
        plugin.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("adapter proof requires an installed plugin")
    manifest = plugin / ".codex-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("installed plugin manifest is invalid") from error
    if version != _project_version():
        raise RuntimeError("installed plugin version does not match the runner")
    hashes: dict[str, str] = {}
    for installed_name, repository_name in PROVENANCE_FILES.items():
        installed = plugin / installed_name
        if not installed.is_file():
            raise RuntimeError(f"installed plugin is missing {installed_name}")
        content = installed.read_bytes()
        if content != _git_blob(expected_commit, repository_name):
            raise RuntimeError(f"installed plugin does not match {repository_name}")
        hashes[installed_name] = hashlib.sha256(content).hexdigest()
    return {
        "installed_plugin": True,
        "plugin_version": version,
        "repository_commit": expected_commit,
        "skill_path": str(skill.resolve()),
        "installed_file_sha256": hashes,
    }


def _validate_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("adapter did not return JSON") from error
    if completed.returncode != 0:
        raise RuntimeError("adapter process failed")
    if not isinstance(payload, dict) or payload.get("subtype") != "success":
        raise RuntimeError("adapter did not return success")
    result = payload.get("result")
    if not isinstance(result, str) or result.strip() != EXPECTED_RESULT:
        raise RuntimeError("adapter did not return the expected result")
    return payload


def _raw_terminal(path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("Codex emitted malformed JSONL") from error
        if not isinstance(record, dict):
            raise RuntimeError("Codex emitted a non-object JSONL event")
        records.append(record)
    threads = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("type") == "thread.started"
    ]
    starts = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "turn.started"
    ]
    completions = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("type") == "turn.completed"
    ]
    messages = [
        (index, record.get("item"))
        for index, record in enumerate(records)
        if record.get("type") == "item.completed"
        and isinstance(record.get("item"), dict)
        and record["item"].get("type") == "agent_message"
    ]
    if not (
        len(threads) == 1
        and len(starts) == 1
        and len(completions) == 1
        and messages
        and threads[0][0] < starts[0]
        and starts[0] < messages[0][0] <= messages[-1][0] < completions[0][0]
    ):
        raise RuntimeError("Codex JSONL terminal sequence is incomplete")
    thread_id = threads[0][1].get("thread_id")
    message = messages[-1][1].get("text")
    usage = completions[0][1].get("usage")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("Codex JSONL lacks a thread ID")
    if not isinstance(message, str) or message.strip() != EXPECTED_RESULT:
        raise RuntimeError("Codex JSONL lacks the expected final message")
    if (
        not isinstance(usage, dict)
        or isinstance(usage.get("input_tokens"), bool)
        or not isinstance(usage.get("input_tokens"), int)
        or isinstance(usage.get("output_tokens"), bool)
        or not isinstance(usage.get("output_tokens"), int)
        or usage["input_tokens"] + usage["output_tokens"] <= 0
    ):
        raise RuntimeError("Codex JSONL lacks positive usage")
    return {"thread_id": thread_id, "message": EXPECTED_RESULT, "usage": usage}


def _capture_wrapper(real_codex: Path, raw_jsonl: Path, wrapper: Path) -> Path:
    wrapper.write_text(
        f"""#!{sys.executable}
import subprocess
import sys
from pathlib import Path

real_codex = {str(real_codex)!r}
raw_jsonl = Path({str(raw_jsonl)!r})
with raw_jsonl.open("w", encoding="utf-8") as output:
    completed = subprocess.run([real_codex, *sys.argv[1:]], stdout=output, check=False)
sys.stdout.write(raw_jsonl.read_text(encoding="utf-8"))
raise SystemExit(completed.returncode)
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def run_smoke(output_dir: Path, expected_commit: str) -> dict[str, Any]:
    if output_dir.exists():
        raise ValueError("adapter smoke output directory must be new")
    output_dir.mkdir(parents=True)
    skill = installed_skill_path()
    provenance = installed_adapter_provenance(skill, expected_commit)
    state_dir = (
        Path(os.environ["HOME"]).expanduser()
        / ".cache"
        / "gepa-optimize-anything-codex"
        / "runs"
        / f"adapter-smoke-{uuid.uuid4().hex}"
    )
    require_unique_state_dir(state_dir)
    runtime = _load_module(
        "release_adapter_smoke_runtime",
        skill / "scripts" / "sandbox_runtime.py",
    )
    paths = runtime.runtime_paths()
    state_dir = runtime.resolve_state_dir(paths, state_dir)
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    environment = runtime.runtime_environment(paths, state_dir)
    environment.update(
        {
            "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
            "CODEX_ADAPTER_MAX_INVOCATIONS": "1",
            "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
        }
    )
    if authentication_mode(environment) != "chatgpt_login" or any(
        environment.get(name) for name in API_KEY_NAMES
    ):
        raise RuntimeError("adapter smoke requires staged login without API keys")
    raw_jsonl = output_dir / "codex-terminal.jsonl"
    capture_wrapper = _capture_wrapper(
        paths.codex, raw_jsonl, output_dir / "codex-capture"
    )
    environment["CODEX_CLI"] = str(capture_wrapper)
    command = [
        str(skill / "scripts" / "claude"),
        "--print",
        "Reply with exactly the lowercase word: ok",
        "--session-id",
        f"adapter-smoke-{uuid.uuid4().hex}",
        "--output-format",
        "json",
        "--model",
        TARGET_MODEL,
        "--effort",
        REASONING_EFFORT,
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=output_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=HOST_TIMEOUT_SECONDS,
    )
    stdout_path = output_dir / "adapter-stdout.json"
    stderr_path = output_dir / "adapter-stderr.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    payload = _validate_payload(completed)
    raw_terminal = _raw_terminal(raw_jsonl)
    evidence = read_adapter_evidence(
        state_dir,
        target_model=TARGET_MODEL,
        reasoning_effort=REASONING_EFFORT,
        expected_invocations=1,
    )
    mapping = evidence.mappings[0]
    if mapping["resume"]:
        raise RuntimeError("adapter smoke unexpectedly resumed")
    if (
        raw_terminal["thread_id"] != mapping["codex_thread_id"]
        or raw_terminal["usage"] != evidence.usage
        or payload.get("codex_thread_id") != raw_terminal["thread_id"]
        or payload.get("usage") != raw_terminal["usage"]
        or payload.get("adapter_target_model") != TARGET_MODEL
    ):
        raise RuntimeError("adapter outputs do not conserve the raw Codex terminal")
    evidence_files = {
        "runner": Path(__file__).resolve(),
        "installed_manifest": skill.parents[1] / ".codex-plugin" / "plugin.json",
        "installed_launcher": skill / "scripts" / "claude",
        "installed_adapter": skill / "scripts" / "codex_claude_adapter.py",
        "installed_runtime": skill / "scripts" / "sandbox_runtime.py",
        "adapter_stdout": stdout_path,
        "adapter_stderr": stderr_path,
        "codex_capture": capture_wrapper,
        "codex_terminal_jsonl": raw_jsonl,
        "invocation_journal": evidence.invocation_paths[0],
        "session_mapping": evidence.session_paths[0],
    }
    receipt = {
        "schema_version": 1,
        "status": "success",
        "proof": "installed_adapter_smoke",
        "scope": "adapter_only",
        "excluded": [
            "gepa_optimizer",
            "optimize_anything",
            "evaluator",
            "judge",
            "research_bullet_harness",
        ],
        "policy": {
            "model": TARGET_MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "max_adapter_invocations": 1,
            "pre_submission_retries": 0,
            "host_timeout_seconds": HOST_TIMEOUT_SECONDS,
        },
        "provenance": provenance,
        "terminal": {
            "return_code": completed.returncode,
            "subtype": payload["subtype"],
            "result": EXPECTED_RESULT,
            "raw_jsonl_reconciled": True,
        },
        "usage": evidence.usage,
        "estimated_cost_usd": evidence.estimated_cost_usd,
        "session_mapping": mapping,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "hashes": hash_files(evidence_files),
    }
    receipt = public_receipt(receipt)
    write_verified_receipt(output_dir / "adapter-smoke-receipt.json", receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    args = parser.parse_args(argv)
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        print("adapter smoke requires RUN_CODEX_LIVE=1", file=sys.stderr)
        return 2
    try:
        receipt = run_smoke(args.output_dir, args.expected_commit)
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        if args.output_dir.is_dir():
            write_verified_receipt(
                args.output_dir / "adapter-smoke-receipt.json",
                {
                    "schema_version": 1,
                    "status": "error",
                    "proof": "installed_adapter_smoke",
                    "error_type": type(error).__name__,
                },
            )
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
