"""Provider-neutral GEPA AgentRunner backed by CodexRuntime."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol
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
    stop_requested: Callable[[], str | None] | None


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
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    def run(self, request: AgentRequest) -> CodexAgentRunResult:
        with self._lock:
            thread_id = self._threads.get(request.continuation_id)
            if request.continuation_id in self._inflight:
                raise ValueError(
                    "agent continuation already has an invocation in flight"
                )
            if request.resume and thread_id is None:
                raise ValueError("cannot resume an unknown agent continuation")
            if not request.resume and thread_id is not None:
                raise ValueError("agent continuation has already been started")
            self._inflight.add(request.continuation_id)

        try:
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
                    stop_requested=getattr(request, "stop_requested", None),
                )
            )
        except BaseException:
            with self._lock:
                self._inflight.discard(request.continuation_id)
            raise
        if result.thread_id is None:
            with self._lock:
                self._inflight.discard(request.continuation_id)
            raise RuntimeError("Codex agent invocation returned no thread identity")
        with self._lock:
            existing = self._threads.setdefault(
                request.continuation_id, result.thread_id
            )
            self._inflight.discard(request.continuation_id)
        if existing != result.thread_id:
            raise RuntimeError("Codex agent continuation changed thread identity")
        usage = result.usage
        return CodexAgentRunResult(
            text=result.text,
            thread_id=result.thread_id,
            status=result.status,
            usage={
                "input_tokens": usage.input_tokens if usage is not None else 0,
                "cached_input_tokens": (
                    usage.cached_input_tokens if usage is not None else 0
                ),
                "output_tokens": usage.output_tokens if usage is not None else 0,
                "reasoning_output_tokens": (
                    usage.reasoning_output_tokens if usage is not None else 0
                ),
                "total_tokens": usage.total_tokens if usage is not None else 0,
            },
        )
