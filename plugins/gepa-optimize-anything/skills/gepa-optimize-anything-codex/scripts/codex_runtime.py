from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol


ProviderKind = Literal["app_server", "codex_cli"]
SandboxMode = Literal["read-only", "workspace-write"]
TerminalStatus = Literal["completed", "failed", "interrupted", "ambiguous"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        values = asdict(self).values()
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("token usage values must be non-negative integers")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")


@dataclass(frozen=True, slots=True)
class InvocationSpec:
    invocation_id: str
    prompt: str
    cwd: Path
    model: str
    reasoning_effort: str
    timeout_seconds: float
    sandbox: SandboxMode
    output_schema: Mapping[str, object] | None = None
    resume_thread_id: str | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id or not self.invocation_id.isascii():
            raise ValueError("invocation_id must be non-empty ASCII")
        if any(character not in "-_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for character in self.invocation_id):
            raise ValueError("invocation_id contains unsafe characters")
        if not self.prompt.strip():
            raise ValueError("prompt must be non-empty")
        if not self.cwd.is_absolute():
            raise ValueError("cwd must be absolute")
        if not self.model.strip() or not self.reasoning_effort.strip():
            raise ValueError("model and reasoning_effort must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.sandbox not in ("read-only", "workspace-write"):
            raise ValueError("unsupported sandbox mode")


@dataclass(frozen=True, slots=True)
class BackendProbe:
    provider: ProviderKind
    available: bool
    runtime_version: str | None
    auth_mode: str | None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InvocationResult:
    invocation_id: str
    provider: ProviderKind
    text: str
    status: TerminalStatus
    model: str
    reasoning_effort: str
    sandbox: SandboxMode
    thread_id: str | None
    turn_id: str | None
    usage: TokenUsage | None
    duration_ms: int
    runtime_version: str
    auth_mode: str
    cost_status: Literal["unknown"] = "unknown"
    fallback_reason: str | None = None


class RuntimeBackend(Protocol):
    kind: ProviderKind

    def probe(self) -> BackendProbe: ...

    def invoke(self, spec: InvocationSpec) -> InvocationResult: ...

    def close(self) -> None: ...


class BackendUnavailable(RuntimeError):
    pass


class AmbiguousInvocation(RuntimeError):
    pass


class EvidenceStore:
    """Own one run directory and exactly one terminal record per invocation."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("evidence root must be absolute")
        self.root = root.resolve()
        self.claims = self.root / "claims"
        self.invocations = self.root / "invocations"
        self.claims.mkdir(parents=True, exist_ok=True)
        self.invocations.mkdir(parents=True, exist_ok=True)

    def claim(self, spec: InvocationSpec) -> Path:
        path = self.claims / f"{spec.invocation_id}.json"
        payload = {
            "schema_version": 1,
            "invocation_id": spec.invocation_id,
            "model": spec.model,
            "reasoning_effort": spec.reasoning_effort,
            "sandbox": spec.sandbox,
            "cwd": str(spec.cwd),
        }
        _write_json_exclusive(path, payload)
        return path

    def finalize(self, result: InvocationResult) -> Path:
        path = self.invocations / f"{result.invocation_id}.json"
        payload = asdict(result)
        payload["schema_version"] = 1
        _write_json_exclusive(path, payload)
        return path

    def finalize_failure(
        self,
        spec: InvocationSpec,
        *,
        provider: ProviderKind,
        status: Literal["failed", "ambiguous"],
        runtime_version: str,
        auth_mode: str,
        duration_ms: int,
        reason: str,
        fallback_reason: str | None,
    ) -> Path:
        path = self.invocations / f"{spec.invocation_id}.json"
        payload = {
            "schema_version": 1,
            "invocation_id": spec.invocation_id,
            "provider": provider,
            "status": status,
            "model": spec.model,
            "reasoning_effort": spec.reasoning_effort,
            "sandbox": spec.sandbox,
            "thread_id": None,
            "turn_id": None,
            "usage": None,
            "duration_ms": duration_ms,
            "runtime_version": runtime_version,
            "auth_mode": auth_mode,
            "cost_status": "unknown",
            "fallback_reason": fallback_reason,
            "error": _safe_reason(reason),
        }
        _write_json_exclusive(path, payload)
        return path


class CodexRuntime:
    """Select one backend before execution and own canonical evidence."""

    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        primary: RuntimeBackend,
        fallback: RuntimeBackend | None = None,
    ) -> None:
        self.evidence = evidence
        self.primary = primary
        self.fallback = fallback
        self._backend: RuntimeBackend | None = None
        self._probe: BackendProbe | None = None
        self._fallback_reason: str | None = None

    def prepare(self) -> BackendProbe:
        if self._probe is not None:
            return self._probe
        primary_probe = self.primary.probe()
        if primary_probe.available:
            self._backend = self.primary
            self._probe = primary_probe
            return primary_probe
        if self.fallback is None:
            raise BackendUnavailable(primary_probe.reason or "primary unavailable")
        fallback_probe = self.fallback.probe()
        if not fallback_probe.available:
            reasons = "; ".join(
                reason for reason in (primary_probe.reason, fallback_probe.reason) if reason
            )
            raise BackendUnavailable(reasons or "no Codex runtime is available")
        self._backend = self.fallback
        self._probe = fallback_probe
        self._fallback_reason = primary_probe.reason or "primary unavailable"
        return fallback_probe

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        probe = self.prepare()
        assert self._backend is not None
        self.evidence.claim(spec)
        started = time.monotonic()
        try:
            result = self._backend.invoke(spec)
            if result.invocation_id != spec.invocation_id:
                raise RuntimeError("backend returned the wrong invocation id")
        except AmbiguousInvocation as exc:
            self.evidence.finalize_failure(
                spec,
                provider=self._backend.kind,
                status="ambiguous",
                runtime_version=probe.runtime_version or "unknown",
                auth_mode=probe.auth_mode or "unknown",
                duration_ms=round((time.monotonic() - started) * 1000),
                reason=str(exc),
                fallback_reason=self._fallback_reason,
            )
            raise
        except Exception as exc:
            self.evidence.finalize_failure(
                spec,
                provider=self._backend.kind,
                status="failed",
                runtime_version=probe.runtime_version or "unknown",
                auth_mode=probe.auth_mode or "unknown",
                duration_ms=round((time.monotonic() - started) * 1000),
                reason=f"{type(exc).__name__}: {exc}",
                fallback_reason=self._fallback_reason,
            )
            raise
        if self._fallback_reason:
            result = InvocationResult(
                **{**asdict(result), "fallback_reason": self._fallback_reason}
            )
        self.evidence.finalize(result)
        return result

    def close(self) -> None:
        seen: set[int] = set()
        for backend in (self.primary, self.fallback):
            if backend is not None and id(backend) not in seen:
                seen.add(id(backend))
                backend.close()

    def __enter__(self) -> CodexRuntime:
        self.prepare()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class SdkAppServerBackend:
    """Official openai-codex SDK backend with deny-by-default approvals."""

    kind: ProviderKind = "app_server"

    def __init__(
        self,
        *,
        sdk_module: object | None = None,
        codex_home: Path | None = None,
        interrupt_grace_seconds: float = 5.0,
    ) -> None:
        if interrupt_grace_seconds <= 0:
            raise ValueError("interrupt grace must be positive")
        self._sdk_module = sdk_module
        self._codex_home = (codex_home or Path.home() / ".codex").resolve()
        self._client: Any | None = None
        self._probe: BackendProbe | None = None
        self._interrupt_grace_seconds = interrupt_grace_seconds

    def probe(self) -> BackendProbe:
        if self._probe is not None:
            return self._probe
        try:
            sdk = self._sdk()
            config = sdk.CodexConfig(
                experimental_api=False,
                env={"CODEX_HOME": str(self._codex_home)},
                config_overrides=(
                    "sandbox_workspace_write.network_access=false",
                    'web_search="disabled"',
                ),
            )
            self._client = sdk.Codex(config)
            account = self._client.account(refresh_token=False)
            auth_mode = _auth_mode(account)
            if auth_mode == "none":
                raise RuntimeError("Codex authentication is required")
            self._probe = BackendProbe(
                provider=self.kind,
                available=True,
                runtime_version=str(sdk.__version__),
                auth_mode=auth_mode,
            )
        except Exception as exc:
            self.close()
            self._probe = BackendProbe(
                provider=self.kind,
                available=False,
                runtime_version=None,
                auth_mode=None,
                reason=_safe_reason(f"{type(exc).__name__}: {exc}"),
            )
        return self._probe

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        probe = self.probe()
        if not probe.available or self._client is None:
            raise BackendUnavailable(probe.reason or "App Server unavailable")
        sdk = self._sdk()
        approval_mode = sdk.ApprovalMode.deny_all
        sandbox = {
            "read-only": sdk.Sandbox.read_only,
            "workspace-write": sdk.Sandbox.workspace_write,
        }[spec.sandbox]
        thread_kwargs = {
            "approval_mode": approval_mode,
            "cwd": str(spec.cwd),
            "model": spec.model,
            "sandbox": sandbox,
            "service_name": "gepa_optimize_anything_codex",
        }
        if spec.resume_thread_id:
            thread = self._client.thread_resume(spec.resume_thread_id, **thread_kwargs)
        else:
            thread = self._client.thread_start(**thread_kwargs)
        handle = thread.turn(
            spec.prompt,
            approval_mode=approval_mode,
            cwd=str(spec.cwd),
            effort=spec.reasoning_effort,
            model=spec.model,
            output_schema=dict(spec.output_schema) if spec.output_schema else None,
            sandbox=sandbox,
        )
        started = time.monotonic()
        result = self._run_with_timeout(handle, spec.timeout_seconds)
        duration_ms = result.duration_ms
        if duration_ms is None:
            duration_ms = round((time.monotonic() - started) * 1000)
        status = _status_value(result.status)
        usage = _usage(result.usage) if result.usage is not None else None
        text = result.final_response or ""
        if status == "completed" and (not text.strip() or usage is None):
            raise RuntimeError("completed Codex turn is missing text or usage")
        return InvocationResult(
            invocation_id=spec.invocation_id,
            provider=self.kind,
            text=text,
            status=status,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            sandbox=spec.sandbox,
            thread_id=thread.id,
            turn_id=result.id,
            usage=usage,
            duration_ms=duration_ms,
            runtime_version=probe.runtime_version or "unknown",
            auth_mode=probe.auth_mode or "unknown",
        )

    def _run_with_timeout(self, handle: Any, timeout_seconds: float) -> Any:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gepa-codex-turn")
        future: Future[Any] = executor.submit(handle.run)
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError:
            handle.interrupt()
            try:
                return future.result(timeout=self._interrupt_grace_seconds)
            except TimeoutError as exc:
                raise AmbiguousInvocation(
                    "Codex turn did not terminate after interrupt"
                ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _sdk(self) -> Any:
        if self._sdk_module is None:
            import openai_codex

            self._sdk_module = openai_codex
        return self._sdk_module


def _status_value(status: object) -> TerminalStatus:
    value = getattr(status, "value", status)
    mapping = {
        "completed": "completed",
        "failed": "failed",
        "interrupted": "interrupted",
    }
    if value not in mapping:
        return "ambiguous"
    return mapping[value]  # type: ignore[return-value]


def _usage(raw: object) -> TokenUsage:
    total = getattr(raw, "total")
    return TokenUsage(
        input_tokens=total.input_tokens,
        cached_input_tokens=total.cached_input_tokens,
        output_tokens=total.output_tokens,
        reasoning_output_tokens=total.reasoning_output_tokens,
        total_tokens=total.total_tokens,
    )


def _auth_mode(account_response: object) -> str:
    account = getattr(account_response, "account", None)
    if account is None:
        return "none"
    root = getattr(account, "root", account)
    name = type(root).__name__.casefold()
    if "chatgpt" in name:
        return "chatgpt"
    if "apikey" in name or "api_key" in name:
        return "api_key"
    if "bedrock" in name:
        return "amazon_bedrock"
    return "managed"


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:300]


def _write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with path.open("x", encoding="utf-8") as file:
        file.write(encoded)


def immutable_usage(usage: TokenUsage) -> Mapping[str, int]:
    return MappingProxyType(asdict(usage))
