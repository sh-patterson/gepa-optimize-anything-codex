"""GEPA language-model callable backed by the Codex-native runtime."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import uuid4

from codex_cli_backend import CodexCliBackend
from codex_runtime import (
    CodexRuntime,
    EvidenceStore,
    InvocationResult,
    InvocationSpec,
    SdkAppServerBackend,
)

TARGET_MODEL = "gpt-5.6-luna"
TARGET_REASONING_EFFORT = "high"
SandboxMode = Literal["read-only", "workspace-write"]


@dataclass(frozen=True, slots=True)
class CodexLMConfig:
    model: str
    reasoning_effort: str
    sandbox_mode: SandboxMode
    evidence_dir: Path
    timeout_seconds: float
    codex_home: Path | None = None
    cli_executable: Path | None = None
    allow_cli_fallback: bool = True

    def __post_init__(self) -> None:
        if self.model != TARGET_MODEL:
            raise ValueError(f"unsupported Codex model: {self.model}")
        if self.reasoning_effort != TARGET_REASONING_EFFORT:
            raise ValueError(f"unsupported reasoning effort: {self.reasoning_effort}")
        if self.sandbox_mode not in ("read-only", "workspace-write"):
            raise ValueError("unsupported sandbox mode")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not self.evidence_dir.is_absolute():
            raise ValueError("evidence_dir must be absolute")
        if self.codex_home is not None and not self.codex_home.is_absolute():
            raise ValueError("codex_home must be absolute")
        if self.cli_executable is not None and not self.cli_executable.is_absolute():
            raise ValueError("cli_executable must be absolute")


@dataclass(frozen=True, slots=True)
class CodexLMResult:
    text: str
    status: Literal["completed"]
    usage: Mapping[str, int]
    estimated_cost_usd: None
    cost_status: Literal["unknown"]
    provider: Literal["app_server", "codex_cli"]
    codex_thread_id: str
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
        environment: Mapping[str, str] | None = None,
        runtime: CodexRuntime | None = None,
        temperature: float | None = None,
        **kwargs: Any,
    ) -> None:
        if temperature not in (None, 1.0) or kwargs:
            raise ValueError("CodexLM does not support LiteLLM sampling options")
        self.config = config
        self.cwd = Path(cwd).resolve()
        if not self.cwd.is_dir():
            raise ValueError("CodexLM cwd must be an existing directory")
        self.environment = dict(environment or {})
        self._claim_evidence_root()
        self.runtime = runtime or _build_runtime(config, self.environment)
        self.last_result: CodexLMResult | None = None
        self.total_cost: None = None
        self.cost_status: Literal["unknown"] = "unknown"
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
        self.invocation_count += 1
        return result.text

    def invoke(self, prompt: str) -> CodexLMResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("CodexLM requires a non-empty string prompt")
        invocation_id = f"lm-{uuid4().hex}"
        result = self.runtime.invoke(
            InvocationSpec(
                invocation_id=invocation_id,
                prompt=prompt,
                cwd=self.cwd,
                model=self.config.model,
                reasoning_effort=self.config.reasoning_effort,
                sandbox=self.config.sandbox_mode,
                timeout_seconds=self.config.timeout_seconds,
            )
        )
        if result.status != "completed" or not result.thread_id:
            raise RuntimeError("Codex runtime did not return a completed LM turn")
        usage = _usage_mapping(result)
        receipt = self.config.evidence_dir / "invocations" / f"{invocation_id}.json"
        if not receipt.is_file():
            raise RuntimeError("Codex runtime did not finalize invocation evidence")
        return CodexLMResult(
            text=result.text,
            status="completed",
            usage=MappingProxyType(usage),
            estimated_cost_usd=None,
            cost_status="unknown",
            provider=result.provider,
            codex_thread_id=result.thread_id,
            raw_receipt_path=receipt,
            duration_ms=result.duration_ms,
            model=result.model,
            reasoning_effort=result.reasoning_effort,
        )

    def close(self) -> None:
        self.runtime.close()

    def _claim_evidence_root(self) -> None:
        self.config.evidence_dir.mkdir(parents=True, exist_ok=True)
        marker = self.config.evidence_dir / ".codex-lm-owner"
        try:
            with marker.open("x", encoding="utf-8") as file:
                file.write(uuid4().hex)
        except FileExistsError as exc:
            raise RuntimeError("CodexLM evidence_dir has already been used") from exc


def _build_runtime(
    config: CodexLMConfig, environment: Mapping[str, str]
) -> CodexRuntime:
    evidence = EvidenceStore(config.evidence_dir)
    primary = SdkAppServerBackend(codex_home=config.codex_home)
    fallback = None
    if config.allow_cli_fallback:
        executable = config.cli_executable or _find_codex_cli()
        if executable is not None:
            fallback = CodexCliBackend(
                executable=executable,
                codex_home=config.codex_home,
                environment=environment,
            )
    return CodexRuntime(evidence=evidence, primary=primary, fallback=fallback)


def _find_codex_cli() -> Path | None:
    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered).resolve()
    try:
        import codex_cli_bin

        package = Path(codex_cli_bin.__file__).resolve().parent
        name = "codex.exe" if os.name == "nt" else "codex"
        candidate = package / "bin" / name
        return candidate if candidate.is_file() else None
    except ImportError:
        return None


def _usage_mapping(result: InvocationResult) -> dict[str, int]:
    usage = {
        "input_tokens": result.usage.input_tokens,
        "cached_input_tokens": result.usage.cached_input_tokens,
        "output_tokens": result.usage.output_tokens,
        "reasoning_output_tokens": result.usage.reasoning_output_tokens,
        "total_tokens": result.usage.total_tokens,
    }
    if usage["input_tokens"] + usage["output_tokens"] <= 0:
        raise RuntimeError("Codex runtime returned no LM usage")
    return usage
