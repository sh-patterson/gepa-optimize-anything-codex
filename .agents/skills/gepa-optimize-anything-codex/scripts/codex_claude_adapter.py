from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALUE_FLAGS = {
    "--allowedTools",
    "--disallowedTools",
    "--effort",
    "--max-budget-usd",
    "--model",
    "--output-format",
    "--permission-mode",
    "--session-id",
    "--settings",
}


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    model: str
    reasoning_effort: str
    upstream_session_id: str
    resume: bool
    requested_budget_usd: float | None
    cwd: Path


@dataclass(frozen=True)
class CodexRun:
    returncode: int
    thread_id: str | None
    final_message: str
    usage: dict[str, int]
    stderr: str
    duration_ms: int


def parse_agent_request(argv: list[str], cwd: Path) -> AgentRequest:
    model = "gpt-5.6-luna"
    effort = "medium"
    session_id: str | None = None
    requested_budget: float | None = None
    resume = False
    prompts: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--resume":
            if index + 1 >= len(argv):
                raise ValueError("missing value for --resume")
            resume = True
            session_id = argv[index + 1]
            index += 2
            continue
        if item in VALUE_FLAGS:
            if index + 1 >= len(argv):
                raise ValueError(f"missing value for {item}")
            value = argv[index + 1]
            if item == "--model":
                model = value
            elif item == "--effort":
                effort = value
            elif item == "--session-id":
                session_id = value
            elif item == "--max-budget-usd":
                requested_budget = float(value)
            index += 2
            continue
        if item.startswith("--"):
            index += 1
            continue
        prompts.append(item)
        index += 1
    if not prompts:
        raise ValueError("missing agent prompt")
    if session_id is None:
        raise ValueError("missing --session-id or --resume session id")
    return AgentRequest(
        prompt="\n\n".join(prompts),
        model=model,
        reasoning_effort=effort,
        upstream_session_id=session_id,
        resume=resume,
        requested_budget_usd=requested_budget,
        cwd=cwd,
    )


def scrubbed_env() -> dict[str, str]:
    blocked_suffixes = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD")
    blocked_names = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}
    return {
        name: value
        for name, value in os.environ.items()
        if name not in blocked_names and not name.endswith(blocked_suffixes)
    }


def parse_codex_output(raw: str) -> tuple[str | None, str, dict[str, int]]:
    thread_id: str | None = None
    messages: list[str] = []
    usage: dict[str, int] = {}
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or "") or None
        item = event.get("item") or {}
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            messages.append(str(item.get("text") or ""))
        if event.get("type") == "turn.completed":
            usage = {
                name: int(value or 0)
                for name, value in (event.get("usage") or {}).items()
            }
    return thread_id, (messages[-1] if messages else ""), usage


def _state_dir() -> Path:
    configured = os.environ.get("CODEX_ADAPTER_STATE_DIR")
    if not configured:
        raise RuntimeError("CODEX_ADAPTER_STATE_DIR is required")
    state_dir = Path(configured)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _load_mapping(state_dir: Path) -> dict[str, str]:
    path = state_dir / "sessions.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise RuntimeError("invalid Codex session mapping")
    return payload


def _save_mapping(state_dir: Path, mapping: dict[str, str]) -> None:
    path = state_dir / "sessions.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(mapping, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _codex_command(
    request: AgentRequest, codex: str, mapping: dict[str, str]
) -> list[str]:
    common = [
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "-m",
        request.model,
        "-c",
        f'model_reasoning_effort="{request.reasoning_effort}"',
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=true",
    ]
    if request.resume:
        thread_id = mapping.get(request.upstream_session_id)
        if thread_id is None:
            raise RuntimeError(
                "no Codex thread mapped for resume session "
                f"{request.upstream_session_id}"
            )
        return [
            codex,
            "exec",
            "resume",
            *common,
            thread_id,
            request.prompt,
        ]
    return [
        codex,
        "exec",
        *common,
        "--sandbox",
        "workspace-write",
        "-C",
        str(request.cwd),
        request.prompt,
    ]


def invoke_codex(request: AgentRequest) -> CodexRun:
    codex = os.environ.get("CODEX_CLI") or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex CLI was not found")
    if request.model != "gpt-5.6-luna":
        raise ValueError("adapter cost accounting is pinned to gpt-5.6-luna")
    state_dir = _state_dir()
    mapping = _load_mapping(state_dir)
    command = _codex_command(request, codex, mapping)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for Codex automation")
    child_env = scrubbed_env()
    child_env["CODEX_API_KEY"] = api_key
    codex_home = state_dir / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    child_env["CODEX_HOME"] = str(codex_home)
    started = time.monotonic()
    proc = subprocess.run(
        command,
        cwd=request.cwd,
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
    )
    thread_id, message, usage = parse_codex_output(proc.stdout)
    duration_ms = round((time.monotonic() - started) * 1000)
    if proc.returncode == 0 and not request.resume:
        if thread_id is None:
            raise RuntimeError("Codex completed without a thread.started event")
        mapping[request.upstream_session_id] = thread_id
        _save_mapping(state_dir, mapping)
    return CodexRun(
        returncode=proc.returncode,
        thread_id=thread_id
        or (mapping.get(request.upstream_session_id) if request.resume else None),
        final_message=message,
        usage=usage,
        stderr=proc.stderr,
        duration_ms=duration_ms,
    )


def estimated_luna_cost(usage: dict[str, int]) -> float:
    input_tokens = usage.get("input_tokens", 0)
    cached_tokens = min(input_tokens, usage.get("cached_input_tokens", 0))
    output_tokens = usage.get("output_tokens", 0)
    return (
        (input_tokens - cached_tokens) * 1.0 + cached_tokens * 0.1 + output_tokens * 6.0
    ) / 1_000_000


def result_payload(request: AgentRequest, run: CodexRun) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success" if run.returncode == 0 else "error",
        "is_error": run.returncode != 0,
        "session_id": request.upstream_session_id,
        "total_cost_usd": estimated_luna_cost(run.usage),
        "duration_ms": run.duration_ms,
        "num_turns": 1,
        "usage": run.usage,
        "result": run.final_message,
    }


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    try:
        request = parse_agent_request(
            list(sys.argv[1:] if argv is None else argv), Path.cwd()
        )
        run = invoke_codex(request)
        print(json.dumps(result_payload(request, run), ensure_ascii=False))
        if run.stderr:
            print(run.stderr, file=sys.stderr, end="")
        return run.returncode
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        payload = {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "session_id": None,
            "total_cost_usd": 0.0,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "num_turns": 0,
            "usage": {},
            "result": str(exc),
        }
        print(json.dumps(payload))
        print(f"Codex adapter error: {exc}", file=sys.stderr)
        return 2
