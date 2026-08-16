"""Provider-neutral GEPA AgentRunner backed by CodexRuntime."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from codex_runtime import CodexRuntime, InvocationSpec


class AgentRequest(Protocol):
    continuation_id: str
    resume: bool
    prompt: str
    cwd: Path
    model: str
    reasoning_effort: str | None
    sandbox: Literal["read-only", "workspace-write"]
    timeout_seconds: float


@dataclass(frozen=True)
class CodexAgentRunResult:
    text: str
    thread_id: str
    status: Literal["completed", "failed", "interrupted", "ambiguous"]
    usage: dict[str, int]
    cost_usd: None = None


class CodexAgentRunner:
    """Maps GEPA continuation identities to provider-scoped Codex threads."""

    def __init__(self, runtime: CodexRuntime) -> None:
        self.runtime = runtime
        self._threads: dict[str, str] = {}
        self._lock = threading.Lock()

    def run(self, request: AgentRequest) -> CodexAgentRunResult:
        with self._lock:
            thread_id = self._threads.get(request.continuation_id)
        if request.resume and thread_id is None:
            raise ValueError("cannot resume an unknown agent continuation")
        if not request.resume and thread_id is not None:
            raise ValueError("agent continuation has already been started")

        result = self.runtime.invoke(
            InvocationSpec(
                invocation_id=f"agent-{uuid4().hex}",
                prompt=request.prompt,
                cwd=request.cwd,
                model=request.model,
                reasoning_effort=request.reasoning_effort or "high",
                sandbox=request.sandbox,
                timeout_seconds=request.timeout_seconds,
                resume_thread_id=thread_id,
            )
        )
        if result.thread_id is None:
            raise RuntimeError("Codex agent invocation returned no thread identity")
        with self._lock:
            existing = self._threads.setdefault(
                request.continuation_id, result.thread_id
            )
        if existing != result.thread_id:
            raise RuntimeError("Codex agent continuation changed thread identity")
        return CodexAgentRunResult(
            text=result.text,
            thread_id=result.thread_id,
            status=result.status,
            usage={
                "input_tokens": result.usage.input_tokens,
                "cached_input_tokens": result.usage.cached_input_tokens,
                "output_tokens": result.usage.output_tokens,
                "reasoning_output_tokens": result.usage.reasoning_output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )
