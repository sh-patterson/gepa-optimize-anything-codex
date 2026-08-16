from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import Future, TimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping, Protocol


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
    stop_requested: Callable[[], str | None] | None = None

    def __post_init__(self) -> None:
        if not self.invocation_id or not self.invocation_id.isascii():
            raise ValueError("invocation_id must be non-empty ASCII")
        if any(
            character
            not in "-_0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            for character in self.invocation_id
        ):
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
        self._prepare_lock = threading.Lock()

    @property
    def fallback_reason(self) -> str | None:
        return self._fallback_reason

    def prepare(self) -> BackendProbe:
        if self._probe is not None:
            return self._probe
        with self._prepare_lock:
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
                    reason
                    for reason in (primary_probe.reason, fallback_probe.reason)
                    if reason
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


def create_runtime(
    *,
    evidence_dir: Path,
    codex_home: Path | None = None,
    cli_executable: Path | None = None,
    environment: Mapping[str, str] | None = None,
    allow_cli_fallback: bool = True,
) -> CodexRuntime:
    """Build the supported App Server primary and optional direct CLI fallback."""
    primary = SdkAppServerBackend(codex_home=codex_home, environment=environment)
    fallback = None
    if allow_cli_fallback:
        executable = cli_executable or _find_codex_cli()
        if executable is not None:
            from codex_cli_backend import CodexCliBackend

            fallback = CodexCliBackend(
                executable=executable,
                codex_home=codex_home,
                environment=environment,
            )
    return CodexRuntime(
        evidence=EvidenceStore(evidence_dir.resolve()),
        primary=primary,
        fallback=fallback,
    )


class SdkAppServerBackend:
    """Official openai-codex SDK backend with deny-by-default approvals."""

    kind: ProviderKind = "app_server"

    def __init__(
        self,
        *,
        sdk_module: object | None = None,
        codex_home: Path | None = None,
        environment: Mapping[str, str] | None = None,
        interrupt_grace_seconds: float = 5.0,
        probe_timeout_seconds: float = 30.0,
    ) -> None:
        if interrupt_grace_seconds <= 0:
            raise ValueError("interrupt grace must be positive")
        if probe_timeout_seconds <= 0:
            raise ValueError("probe timeout must be positive")
        self._sdk_module = sdk_module
        self._use_sanitized_client = sdk_module is None
        self._codex_home = (codex_home or Path.home() / ".codex").resolve()
        self._environment = dict(environment or {})
        self._environment["OPENAI_API_KEY"] = ""
        self._environment["CODEX_API_KEY"] = ""
        self._environment["CODEX_HOME"] = str(self._codex_home)
        self._client: Any | None = None
        self._probe: BackendProbe | None = None
        self._interrupt_grace_seconds = interrupt_grace_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._state_lock = threading.RLock()
        self._probe_lock = threading.Lock()
        self._setup_leases = 0

    def probe(self) -> BackendProbe:
        with self._state_lock:
            cached = self._probe
        if cached is not None:
            return cached

        with self._probe_lock:
            with self._state_lock:
                cached = self._probe
            if cached is not None:
                return cached

            observed_client: dict[str, Any] = {}
            timed_out = threading.Event()

            def observe(client: Any) -> None:
                observed_client["client"] = client
                if timed_out.is_set():
                    self._force_close_probe_client(client)

            def start_and_authenticate() -> tuple[Any, str, str]:
                sdk = self._sdk()
                config_overrides = (
                    "sandbox_workspace_write.network_access=false",
                    'web_search="disabled"',
                )
                config = sdk.CodexConfig(
                    experimental_api=False,
                    env=self._environment,
                    config_overrides=config_overrides,
                )
                if self._use_sanitized_client:
                    from codex_sdk_client import create_sanitized_codex

                    client = create_sanitized_codex(
                        sdk, config, client_observer=observe
                    )
                else:
                    client = sdk.Codex(config)
                    observe(client)
                account = client.account(refresh_token=False)
                auth_mode = _auth_mode(account)
                if auth_mode == "none":
                    raise RuntimeError("Codex authentication is required")
                return client, auth_mode, str(sdk.__version__)

            future = _submit_daemon(
                start_and_authenticate, name="gepa-codex-app-server-probe"
            )
            try:
                client, auth_mode, runtime_version = future.result(
                    timeout=self._probe_timeout_seconds
                )
                probe = BackendProbe(
                    provider=self.kind,
                    available=True,
                    runtime_version=runtime_version,
                    auth_mode=auth_mode,
                )
            except TimeoutError:
                timed_out.set()
                client = observed_client.get("client")
                if client is not None:
                    self._force_close_probe_client(client)
                future.add_done_callback(self._close_late_probe_result)
                client = None
                probe = BackendProbe(
                    provider=self.kind,
                    available=False,
                    runtime_version=None,
                    auth_mode=None,
                    reason="App Server probe timed out",
                )
            except Exception as exc:
                client = observed_client.get("client")
                if client is not None:
                    self._force_close_probe_client(client)
                client = None
                probe = BackendProbe(
                    provider=self.kind,
                    available=False,
                    runtime_version=None,
                    auth_mode=None,
                    reason=_safe_reason(f"{type(exc).__name__}: {exc}"),
                )
            with self._state_lock:
                self._client = client
                self._probe = probe
            return probe

    @staticmethod
    def _force_close_probe_client(client: Any) -> None:
        try:
            force_close = getattr(client, "force_close", None)
            if force_close is not None:
                force_close()
            else:
                client.close()
        except Exception:
            pass

    def _close_late_probe_result(self, future: Future[Any]) -> None:
        try:
            client, _, _ = future.result()
        except BaseException:
            return
        self._force_close_probe_client(client)

    def invoke(self, spec: InvocationSpec) -> InvocationResult:
        probe = self.probe()
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
        with self._state_lock:
            if not probe.available or self._probe is not probe or self._client is None:
                raise BackendUnavailable(probe.reason or "App Server unavailable")
            client = self._client

        def start_turn() -> tuple[Any, Any]:
            if spec.resume_thread_id:
                thread = self._dispatch_setup_rpc(
                    client,
                    probe,
                    lambda: client.thread_resume(
                        spec.resume_thread_id, **thread_kwargs
                    ),
                )
            else:
                thread = self._dispatch_setup_rpc(
                    client, probe, lambda: client.thread_start(**thread_kwargs)
                )
            handle = self._dispatch_setup_rpc(
                client,
                probe,
                lambda: thread.turn(
                    spec.prompt,
                    approval_mode=approval_mode,
                    cwd=str(spec.cwd),
                    effort=spec.reasoning_effort,
                    model=spec.model,
                    output_schema=(
                        dict(spec.output_schema) if spec.output_schema else None
                    ),
                    sandbox=sandbox,
                ),
            )
            return thread, handle

        started = time.monotonic()
        setup_future = _submit_daemon(start_turn, name="gepa-codex-turn-setup")
        try:
            thread, handle = setup_future.result(timeout=spec.timeout_seconds)
        except TimeoutError as exc:
            self._contain_unconfirmed_turn()
            raise AmbiguousInvocation(
                "Codex turn setup exceeded the invocation timeout"
            ) from exc
        except Exception as exc:
            with self._state_lock:
                still_current = self._client is client and self._probe is probe
            if not still_current:
                raise AmbiguousInvocation(
                    "Codex turn setup raced with backend containment"
                ) from exc
            raise
        remaining = spec.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            self._contain_unconfirmed_turn()
            raise AmbiguousInvocation(
                "Codex turn setup consumed the invocation timeout"
            )
        result = self._run_with_timeout(
            handle, remaining, stop_requested=spec.stop_requested
        )
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

    def _dispatch_setup_rpc(
        self,
        client: Any,
        probe: BackendProbe,
        call: Callable[[], Any],
    ) -> Any:
        """Reserve one setup RPC before dispatch so poisoning cannot race it."""
        with self._state_lock:
            if self._client is not client or self._probe is not probe:
                raise BackendUnavailable("App Server was contained during turn setup")
            self._setup_leases += 1
        try:
            return call()
        finally:
            with self._state_lock:
                self._setup_leases -= 1

    def _run_with_timeout(
        self,
        handle: Any,
        timeout_seconds: float,
        *,
        stop_requested: Callable[[], str | None] | None = None,
    ) -> Any:
        future = _submit_daemon(handle.run, name="gepa-codex-turn")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                reason = stop_requested() if stop_requested is not None else None
            except Exception as exc:
                try:
                    self._interrupt_and_collect(
                        handle, future, "lifecycle monitor failure"
                    )
                except AmbiguousInvocation:
                    raise AmbiguousInvocation(
                        "Codex lifecycle monitor failed; turn status is ambiguous"
                    ) from exc
                self._contain_unconfirmed_turn()
                raise AmbiguousInvocation(
                    "Codex lifecycle monitor failed; turn status is ambiguous"
                ) from exc
            remaining = deadline - time.monotonic()
            if reason is not None or remaining <= 0:
                return self._interrupt_and_collect(handle, future, reason)
            try:
                return future.result(timeout=min(0.1, remaining))
            except TimeoutError:
                continue

    def _interrupt_and_collect(
        self, handle: Any, future: Future[Any], reason: str | None
    ) -> Any:
        interrupt_future = _submit_daemon(handle.interrupt, name="gepa-codex-interrupt")
        try:
            interrupt_future.result(timeout=self._interrupt_grace_seconds)
        except Exception as exc:
            self._contain_unconfirmed_turn()
            raise AmbiguousInvocation(
                "Codex turn stopped but interruption could not be confirmed"
            ) from exc
        try:
            return future.result(timeout=self._interrupt_grace_seconds)
        except TimeoutError as exc:
            self._contain_unconfirmed_turn()
            detail = reason or "runtime timeout"
            raise AmbiguousInvocation(
                f"Codex turn did not terminate after interrupt ({_safe_reason(detail)})"
            ) from exc

    def _contain_unconfirmed_turn(self) -> None:
        with self._state_lock:
            client = self._client
            self._client = None
            leased_setups = self._setup_leases
            self._probe = BackendProbe(
                provider=self.kind,
                available=False,
                runtime_version=None,
                auth_mode=None,
                reason=(
                    "Codex turn termination was unconfirmed; backend was contained; "
                    f"preexisting_setup_leases={leased_setups}"
                ),
            )
        try:
            if client is not None:
                client.close()
        except Exception:
            # The transport is already untrusted. Preserve the ambiguous status
            # instead of allowing a secondary shutdown error to relabel it.
            pass

    def close(self) -> None:
        with self._state_lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

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


def _submit_daemon(call: Callable[[], Any], *, name: str) -> Future[Any]:
    """Run an SDK blocking call without registering an interpreter-exit join."""
    future: Future[Any] = Future()

    def invoke() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(call())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=invoke, name=name, daemon=True).start()
    return future


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


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:300]


def _write_json_exclusive(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with path.open("x", encoding="utf-8") as file:
        file.write(encoded)


def immutable_usage(usage: TokenUsage) -> Mapping[str, int]:
    return MappingProxyType(asdict(usage))
