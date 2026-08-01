from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import uuid4


TARGET_MODEL = "gpt-5.6-luna"
TARGET_REASONING_EFFORT = "high"
SandboxMode = Literal["workspace-write"]


@dataclass(frozen=True, slots=True)
class CodexLMConfig:
    executable: Path
    model: str
    reasoning_effort: str
    sandbox_mode: SandboxMode
    state_dir: Path
    session_dir: Path
    timeout_seconds: float
    retry_ceiling: int = 0

    def __post_init__(self) -> None:
        if self.model != TARGET_MODEL:
            raise ValueError(f"unsupported Codex model: {self.model}")
        if self.reasoning_effort != TARGET_REASONING_EFFORT:
            raise ValueError(f"unsupported reasoning effort: {self.reasoning_effort}")
        if self.sandbox_mode != "workspace-write":
            raise ValueError("unsupported sandbox mode")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retry_ceiling < 0:
            raise ValueError("retry_ceiling must be non-negative")
        state = self.state_dir.resolve()
        sessions = self.session_dir.resolve()
        if state == sessions or state in sessions.parents or sessions in state.parents:
            raise ValueError("state_dir and session_dir must be separate")


@dataclass(frozen=True, slots=True)
class CodexLMResult:
    text: str
    status: Literal["completed"]
    usage: Mapping[str, int]
    estimated_cost_usd: float
    upstream_session_id: str
    codex_thread_id: str
    session_mapping_path: Path
    raw_receipt_path: Path
    duration_ms: int
    model: str
    reasoning_effort: str


class CodexLM:
    """The supported GEPA callable for the installed Codex skill."""

    def __init__(
        self,
        config: CodexLMConfig,
        *,
        cwd: Path,
        environment: Mapping[str, str],
        temperature: float | None = None,
        **kwargs: Any,
    ) -> None:
        if temperature not in (None, 1.0) or kwargs:
            raise ValueError("CodexLM does not support LiteLLM sampling options")
        if any(environment.get(name) for name in _SECRET_NAMES):
            raise ValueError("CodexLM child environment must not contain API keys")
        self.config = config
        self.cwd = Path(cwd)
        self.environment = dict(environment)
        self._claim_roots()
        self.last_result: CodexLMResult | None = None
        self.total_cost = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_usage: dict[str, int] = {}
        self.invocation_count = 0

    def __call__(self, prompt: str) -> str:
        result = self.invoke(prompt)
        self.last_result = result
        self.total_tokens_in += result.usage["input_tokens"]
        self.total_tokens_out += result.usage["output_tokens"]
        for name, value in result.usage.items():
            self.total_usage[name] = self.total_usage.get(name, 0) + value
        self.total_cost += result.estimated_cost_usd
        self.invocation_count += 1
        return result.text

    def invoke(self, prompt: str) -> CodexLMResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("CodexLM requires a non-empty string prompt")
        session_id = uuid4().hex
        command = [
            os.fspath(self.config.executable),
            "--print",
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--disallowedTools=WebFetch,WebSearch",
            "--model",
            self.config.model,
            "--effort",
            self.config.reasoning_effort,
            "--session-id",
            session_id,
            prompt,
        ]
        started = time.monotonic()
        completed = self._run(command)
        duration_ms = round((time.monotonic() - started) * 1000)
        receipt = self._write_receipt(command, completed, duration_ms)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex adapter returned invalid JSON; receipt={receipt}") from exc
        text, usage, estimated_cost, thread_id = _validated_payload(
            payload, completed.returncode, session_id, self.config
        )
        mapping = self.config.session_dir / "mappings" / f"{session_id}.json"
        _write_json(mapping, {"upstream_session_id": session_id, "codex_thread_id": thread_id})
        return CodexLMResult(
            text=text,
            status="completed",
            usage=MappingProxyType(usage),
            estimated_cost_usd=estimated_cost,
            upstream_session_id=session_id,
            codex_thread_id=thread_id,
            session_mapping_path=mapping,
            raw_receipt_path=receipt,
            duration_ms=duration_ms,
            model=self.config.model,
            reasoning_effort=self.config.reasoning_effort,
        )

    def _claim_roots(self) -> None:
        for root in (self.config.state_dir, self.config.session_dir):
            root.mkdir(parents=True, exist_ok=True)
        marker = self.config.state_dir / ".codex-lm-owner"
        try:
            with marker.open("x", encoding="utf-8") as file:
                file.write(uuid4().hex)
        except FileExistsError as exc:
            raise RuntimeError("CodexLM state_dir has already been used") from exc
        (self.config.session_dir / "mappings").mkdir(exist_ok=True)
        (self.config.session_dir / "receipts").mkdir(exist_ok=True)

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        attempts = 0
        while True:
            try:
                return subprocess.run(
                    command, cwd=self.cwd, env=self.environment, capture_output=True,
                    text=True, check=False, timeout=self.config.timeout_seconds,
                )
            except OSError:
                if attempts >= self.config.retry_ceiling:
                    raise RuntimeError("Codex process could not start") from None
                attempts += 1
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Codex process timed out") from exc

    def _write_receipt(
        self, command: list[str], completed: subprocess.CompletedProcess[str], duration_ms: int
    ) -> Path:
        path = self.config.session_dir / "receipts" / f"{uuid4().hex}.json"
        _write_json(path, {"command": command[:-1], "returncode": completed.returncode,
                           "stdout": completed.stdout, "stderr": completed.stderr,
                           "duration_ms": duration_ms, "model": self.config.model,
                           "reasoning_effort": self.config.reasoning_effort})
        return path


_SECRET_NAMES = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"})


def _validated_payload(
    payload: object,
    returncode: int,
    session_id: str,
    config: CodexLMConfig,
) -> tuple[str, dict[str, int], float, str]:
    if returncode != 0 or not isinstance(payload, dict) or payload.get("is_error") is not False:
        raise RuntimeError("Codex adapter invocation failed")
    if payload.get("session_id") != session_id or payload.get("adapter_target_model") != config.model:
        raise RuntimeError("Codex adapter session or model mapping is inconsistent")
    text, usage, thread_id = payload.get("result"), payload.get("usage"), payload.get("codex_thread_id")
    if not isinstance(text, str) or not text.strip() or not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("Codex adapter completion is incomplete")
    if not isinstance(usage, dict) or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
        raise RuntimeError("Codex adapter returned invalid usage")
    if not isinstance(usage.get("input_tokens"), int) or not isinstance(usage.get("output_tokens"), int) or usage["input_tokens"] + usage["output_tokens"] <= 0:
        raise RuntimeError("Codex adapter returned no usage")
    estimated_cost = payload.get("total_cost_usd")
    if (
        isinstance(estimated_cost, bool)
        or not isinstance(estimated_cost, (int, float))
        or estimated_cost <= 0
    ):
        raise RuntimeError("Codex adapter returned no cost estimate")
    return text, dict(usage), float(estimated_cost), thread_id


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
