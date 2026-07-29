from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

VALUE_FLAGS = {
    "--effort",
    "--max-budget-usd",
    "--model",
    "--output-format",
    "--permission-mode",
    "--session-id",
    "--settings",
}

TARGET_MODEL = "gpt-5.6-luna"
SUPPORTED_SOURCE_MODELS = frozenset({"claude-sonnet-4-6", TARGET_MODEL})
NO_VALUE_FLAGS = {"--print"}
EXPECTED_DISALLOWED_TOOLS = "--disallowedTools=WebFetch,WebSearch"
TERMINATION_GRACE_SECONDS = 2.0
INVOCATION_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AgentRequest:
    prompt: str
    source_model: str
    target_model: str
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
    terminal_status: Literal["completed", "failed", "ambiguous"]


@dataclass(frozen=True)
class CodexTerminalState:
    thread_id: str | None
    final_message: str
    usage: dict[str, int]
    status: Literal["completed", "failed", "missing"]
    error: str | None


def parse_agent_request(argv: list[str], cwd: Path) -> AgentRequest:
    source_model = "claude-sonnet-4-6"
    effort = "high"
    session_id: str | None = None
    requested_budget: float | None = None
    resume = False
    saw_session_id = False
    prompts: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--resume":
            if index + 1 >= len(argv):
                raise ValueError("missing value for --resume")
            if resume or saw_session_id:
                raise ValueError("use exactly one of --session-id or --resume")
            resume = True
            session_id = argv[index + 1]
            index += 2
            continue
        if item == "--settings":
            if index + 1 >= len(argv):
                raise ValueError("missing value for --settings")
            raise ValueError("--settings is unsupported by the Codex adapter")
        if item.startswith("--disallowedTools="):
            if item != EXPECTED_DISALLOWED_TOOLS:
                raise ValueError(
                    "unsupported --disallowedTools policy; expected "
                    "WebFetch,WebSearch"
                )
            index += 1
            continue
        if item in VALUE_FLAGS:
            if index + 1 >= len(argv):
                raise ValueError(f"missing value for {item}")
            value = argv[index + 1]
            if item == "--model":
                source_model = value
            elif item == "--effort":
                effort = value
            elif item == "--session-id":
                if resume or saw_session_id:
                    raise ValueError("use exactly one of --session-id or --resume")
                saw_session_id = True
                session_id = value
            elif item == "--max-budget-usd":
                requested_budget = float(value)
            elif item == "--output-format" and value != "json":
                raise ValueError("--output-format must be json")
            elif item == "--permission-mode" and value != "bypassPermissions":
                raise ValueError("--permission-mode must be bypassPermissions")
            index += 2
            continue
        if item in NO_VALUE_FLAGS:
            index += 1
            continue
        if item.startswith("--"):
            raise ValueError(f"unsupported Claude CLI flag: {item}")
        prompts.append(item)
        index += 1
    if not prompts:
        raise ValueError("missing agent prompt")
    if session_id is None:
        raise ValueError("missing --session-id or --resume session id")
    if source_model not in SUPPORTED_SOURCE_MODELS:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_MODELS))
        raise ValueError(
            f"unsupported source model {source_model!r}; supported models: {supported}"
        )
    return AgentRequest(
        prompt="\n\n".join(prompts),
        source_model=source_model,
        target_model=TARGET_MODEL,
        reasoning_effort=effort,
        upstream_session_id=session_id,
        resume=resume,
        requested_budget_usd=requested_budget,
        cwd=cwd,
    )


def scrubbed_env() -> dict[str, str]:
    blocked_names = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    return {
        name: value
        for name, value in os.environ.items()
        if name not in blocked_names
    }


def _error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "Codex turn failed")
    return str(error or event.get("message") or "Codex turn failed")


def parse_codex_output(raw: str) -> CodexTerminalState:
    thread_id: str | None = None
    messages: list[str] = []
    usage: dict[str, int] = {}
    status: Literal["completed", "failed", "missing"] = "missing"
    error: str | None = None
    terminal_events = 0
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            status = "failed"
            error = "Codex emitted malformed JSONL"
            continue
        if not isinstance(event, dict):
            status = "failed"
            error = "Codex emitted a non-object JSONL event"
            continue
        if event.get("type") == "thread.started":
            thread_id = str(event.get("thread_id") or "") or None
        item = event.get("item")
        if item is None:
            item = {}
        elif not isinstance(item, dict):
            status = "failed"
            error = "Codex emitted an invalid item event"
            continue
        if (
            event.get("type") == "item.completed"
            and item.get("type") == "agent_message"
        ):
            messages.append(str(item.get("text") or ""))
        if event.get("type") in {"turn.failed", "error"}:
            terminal_events += 1
            if terminal_events != 1:
                status = "failed"
                error = "Codex emitted multiple terminal events"
                continue
            status = "failed"
            error = _error_message(event)
            continue
        if event.get("type") == "turn.completed" and status != "failed":
            terminal_events += 1
            if terminal_events != 1:
                status = "failed"
                error = "Codex emitted multiple terminal events"
                continue
            raw_usage = event.get("usage")
            if not isinstance(raw_usage, dict):
                status = "failed"
                error = "Codex completed without usage"
                continue
            if not {"input_tokens", "output_tokens"}.issubset(raw_usage):
                status = "failed"
                error = "Codex completed without required usage fields"
                continue
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in raw_usage.values()
            ):
                status = "failed"
                error = "Codex completed with invalid usage"
                continue
            usage = dict(raw_usage)
            status = "completed"
    return CodexTerminalState(
        thread_id=thread_id,
        final_message=messages[-1] if messages else "",
        usage=usage,
        status=status,
        error=error,
    )


def _state_dir() -> Path:
    configured = os.environ.get("CODEX_ADAPTER_STATE_DIR")
    if not configured:
        raise RuntimeError("CODEX_ADAPTER_STATE_DIR is required")
    state_dir = Path(configured)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _session_record_path(state_dir: Path, upstream_session_id: str) -> Path:
    digest = sha256(upstream_session_id.encode("utf-8")).hexdigest()
    return state_dir / "sessions" / f"{digest}.json"


def _load_session_thread(state_dir: Path, upstream_session_id: str) -> str | None:
    path = _session_record_path(state_dir, upstream_session_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("upstream_session_id") != upstream_session_id
        or not isinstance(payload.get("thread_id"), str)
        or not payload["thread_id"]
    ):
        raise RuntimeError("invalid Codex session record")
    return payload["thread_id"]


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
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


def _save_session_thread(
    state_dir: Path, upstream_session_id: str, thread_id: str
) -> None:
    path = _session_record_path(state_dir, upstream_session_id)
    _atomic_json_write(
        path,
        {
            "upstream_session_id": upstream_session_id,
            "thread_id": thread_id,
        },
    )


def _invocation_terminal_status(
    returncode: int, terminal: CodexTerminalState
) -> Literal["completed", "failed", "ambiguous"]:
    if (
        terminal.status == "missing"
        or returncode != 0
        and terminal.status == "completed"
    ):
        return "ambiguous"
    if terminal.status == "failed":
        if terminal.error and terminal.error.startswith("Codex emitted"):
            return "ambiguous"
        return "failed"
    return "completed"


def _save_invocation_record(
    state_dir: Path, request: AgentRequest, run: CodexRun
) -> None:
    _atomic_json_write(
        state_dir / "invocations" / f"{uuid4()}.json",
        {
            "schema_version": INVOCATION_RECORD_SCHEMA_VERSION,
            "upstream_session_id": request.upstream_session_id,
            "codex_thread_id": run.thread_id,
            "resume": request.resume,
            "source_model": request.source_model,
            "target_model": request.target_model,
            "reasoning_effort": request.reasoning_effort,
            "return_code": run.returncode,
            "terminal_status": run.terminal_status,
            "usage": run.usage,
            "estimated_cost_usd": estimated_luna_cost(run.usage),
            "cost_status": (
                "standard_tier_upper_estimate_from_observed_usage"
                if run.usage
                else "unknown"
            ),
            "duration_ms": run.duration_ms,
        },
    )


def _codex_command(
    request: AgentRequest, codex: str, thread_id: str | None
) -> list[str]:
    common = [
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "-m",
        request.target_model,
        "-c",
        f'model_reasoning_effort="{request.reasoning_effort}"',
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-c",
        "features.standalone_web_search=false",
    ]
    if request.resume:
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


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _run_codex(
    command: list[str], cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )

    def stop_child(signum: int, _frame: Any) -> None:
        _terminate_process_group(proc)
        raise RuntimeError(f"Codex adapter interrupted by signal {signum}")

    previous_handlers = {
        signum: signal.signal(signum, stop_child)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        stdout, stderr = proc.communicate()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def invoke_codex(request: AgentRequest) -> CodexRun:
    state_dir = _state_dir()
    started = time.monotonic()
    try:
        if request.requested_budget_usd is not None:
            raise ValueError(
                "--max-budget-usd is unsupported because this adapter cannot "
                "enforce a USD cap; omit max_token_cost for Codex agentic engines"
            )
        codex = os.environ.get("CODEX_CLI") or shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLI was not found")
        resumed_thread = (
            _load_session_thread(state_dir, request.upstream_session_id)
            if request.resume
            else None
        )
        command = _codex_command(request, codex, resumed_thread)
        child_env = scrubbed_env()
        api_key = os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            child_env["CODEX_API_KEY"] = api_key
        proc = _run_codex(command, request.cwd, child_env)
        terminal = parse_codex_output(proc.stdout)
        duration_ms = round((time.monotonic() - started) * 1000)
        if proc.returncode == 0:
            validation_error: str | None = None
            if terminal.status != "completed":
                validation_error = (
                    terminal.error or "Codex exited without a completed turn"
                )
            elif not terminal.final_message.strip():
                validation_error = "Codex completed without a final agent message"
            elif not request.resume and terminal.thread_id is None:
                validation_error = "Codex completed without a thread.started event"
            if validation_error is not None:
                run = CodexRun(
                    returncode=1,
                    thread_id=terminal.thread_id or resumed_thread,
                    final_message=terminal.final_message,
                    usage=terminal.usage,
                    stderr=f"{proc.stderr}{validation_error}\n",
                    duration_ms=duration_ms,
                    terminal_status=_invocation_terminal_status(
                        proc.returncode, terminal
                    ),
                )
                _save_invocation_record(state_dir, request, run)
                return run
            if not request.resume:
                _save_session_thread(
                    state_dir, request.upstream_session_id, terminal.thread_id
                )
        run = CodexRun(
            returncode=proc.returncode,
            thread_id=terminal.thread_id or resumed_thread,
            final_message=terminal.final_message,
            usage=terminal.usage,
            stderr=proc.stderr,
            duration_ms=duration_ms,
            terminal_status=_invocation_terminal_status(proc.returncode, terminal),
        )
        _save_invocation_record(state_dir, request, run)
        return run
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        failed_run = CodexRun(
            returncode=2,
            thread_id=None,
            final_message="",
            usage={},
            stderr="",
            duration_ms=round((time.monotonic() - started) * 1000),
            terminal_status="failed",
        )
        _save_invocation_record(state_dir, request, failed_run)
        raise


def estimated_luna_cost(usage: dict[str, int]) -> float:
    input_tokens = usage.get("input_tokens", 0)
    cached_tokens = min(input_tokens, usage.get("cached_input_tokens", 0))
    output_tokens = usage.get("output_tokens", 0)
    long_context = input_tokens > 272_000
    input_multiplier = 2.0 if long_context else 1.0
    output_multiplier = 1.5 if long_context else 1.0
    return (
        (input_tokens - cached_tokens) * 1.25 * input_multiplier
        + cached_tokens * 0.1 * input_multiplier
        + output_tokens * 6.0 * output_multiplier
    ) / 1_000_000


def result_payload(request: AgentRequest, run: CodexRun) -> dict[str, Any]:
    estimated_cost = estimated_luna_cost(run.usage)
    return {
        "type": "result",
        "subtype": "success" if run.returncode == 0 else "error",
        "is_error": run.returncode != 0,
        "session_id": request.upstream_session_id,
        "total_cost_usd": estimated_cost,
        "duration_ms": run.duration_ms,
        "num_turns": 1,
        "usage": run.usage,
        "result": run.final_message,
        "adapter_source_model": request.source_model,
        "adapter_target_model": request.target_model,
        "adapter_cost_status": (
            "standard_tier_upper_estimate_from_observed_usage"
            if run.usage
            else "unknown"
        ),
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
