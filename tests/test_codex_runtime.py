from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "plugins"
    / "gepa-optimize-anything"
    / "skills"
    / "gepa-optimize-anything-codex"
    / "scripts"
    / "codex_runtime.py"
)
SPEC = importlib.util.spec_from_file_location("codex_runtime_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


@dataclass
class FakeBreakdown:
    input_tokens: int = 2
    cached_input_tokens: int = 0
    output_tokens: int = 3
    reasoning_output_tokens: int = 1
    total_tokens: int = 5


class FakeResult:
    id = "turn-1"
    status = SimpleNamespace(value="completed")
    final_response = "BLUE"
    duration_ms = 12
    usage = SimpleNamespace(total=FakeBreakdown())


class FakeHandle:
    def __init__(self, result: object | None = None) -> None:
        self.result = result or FakeResult()
        self.interrupted = False

    def run(self) -> object:
        return self.result

    def interrupt(self) -> None:
        self.interrupted = True


class FakeThread:
    id = "thread-1"

    def __init__(self, client: "FakeClient") -> None:
        self.client = client

    def turn(self, prompt: str, **kwargs: object) -> FakeHandle:
        self.client.turn_call = (prompt, kwargs)
        return self.client.handle


class ChatgptAccount:
    pass


class FakeClient:
    def __init__(self, _config: object) -> None:
        self.thread_call: tuple[str, object] | None = None
        self.turn_call: tuple[str, dict[str, object]] | None = None
        self.handle = FakeHandle()
        self.closed = False

    def account(self, *, refresh_token: bool) -> object:
        assert refresh_token is False
        return SimpleNamespace(account=SimpleNamespace(root=ChatgptAccount()))

    def thread_start(self, **kwargs: object) -> FakeThread:
        self.thread_call = ("start", kwargs)
        return FakeThread(self)

    def thread_resume(self, thread_id: str, **kwargs: object) -> FakeThread:
        self.thread_call = (thread_id, kwargs)
        return FakeThread(self)

    def close(self) -> None:
        self.closed = True


class FakeApprovalMode:
    deny_all = object()


class FakeSandbox:
    read_only = object()
    workspace_write = object()


class FakeSdk:
    __version__ = "test-sdk"
    ApprovalMode = FakeApprovalMode
    Sandbox = FakeSandbox
    CodexConfig = SimpleNamespace

    def __init__(self) -> None:
        self.client: FakeClient | None = None
        self.config: object | None = None

    def Codex(self, config: object) -> FakeClient:
        self.config = config
        self.client = FakeClient(config)
        return self.client


def _spec(tmp_path: Path, **overrides: object) -> object:
    values = {
        "invocation_id": "invocation-1",
        "prompt": "Return BLUE",
        "cwd": tmp_path.resolve(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "high",
        "timeout_seconds": 1.0,
        "sandbox": "workspace-write",
    }
    values.update(overrides)
    return runtime.InvocationSpec(**values)


def test_sdk_backend_reuses_auth_and_denies_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "must-not-cross-boundary")
    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(
        sdk_module=sdk,
        environment={
            "PATH": "runtime-path",
            "OPENAI_API_KEY": "must-not-cross-boundary",
            "UNRELATED_SECRET": "must-not-cross-boundary",
        },
    )

    probe = backend.probe()
    result = backend.invoke(_spec(tmp_path))

    assert probe.available is True
    assert probe.auth_mode == "chatgpt"
    assert sdk.config is not None
    assert sdk.config.experimental_api is False
    assert sdk.config.env["CODEX_HOME"] == str((Path.home() / ".codex").resolve())
    assert sdk.config.env["OPENAI_API_KEY"] == ""
    assert sdk.config.env["CODEX_API_KEY"] == ""
    assert sdk.config.env["PATH"] == "runtime-path"
    assert sdk.config.env["UNRELATED_SECRET"] == "must-not-cross-boundary"
    assert "sandbox_workspace_write.network_access=false" in sdk.config.config_overrides
    assert result.status == "completed"
    assert result.text == "BLUE"
    assert result.usage.total_tokens == 5
    assert sdk.client is not None
    assert sdk.client.thread_call is not None
    assert sdk.client.thread_call[1]["approval_mode"] is FakeApprovalMode.deny_all
    assert sdk.client.thread_call[1]["sandbox"] is FakeSandbox.workspace_write
    assert sdk.client.turn_call is not None
    assert sdk.client.turn_call[1]["approval_mode"] is FakeApprovalMode.deny_all
    assert sdk.client.turn_call[1]["sandbox"] is FakeSandbox.workspace_write


def test_sdk_backend_bounds_probe_and_allows_fallback(tmp_path: Path) -> None:
    release = threading.Event()

    class BlockingClient(FakeClient):
        def account(self, *, refresh_token: bool) -> object:
            release.wait(1)
            return super().account(refresh_token=refresh_token)

    class BlockingSdk(FakeSdk):
        def Codex(self, config: object) -> FakeClient:
            self.config = config
            self.client = BlockingClient(config)
            return self.client

    sdk = BlockingSdk()
    backend = runtime.SdkAppServerBackend(
        sdk_module=sdk, probe_timeout_seconds=0.02
    )
    fallback_probe = runtime.BackendProbe(
        provider="codex_cli",
        available=True,
        runtime_version="test-cli",
        auth_mode="chatgpt",
    )
    fallback = SimpleNamespace(probe=lambda: fallback_probe)
    coordinator = runtime.CodexRuntime(
        evidence=runtime.EvidenceStore((tmp_path / "evidence").resolve()),
        primary=backend,
        fallback=fallback,
    )
    started = time.monotonic()
    probe = coordinator.prepare()
    elapsed = time.monotonic() - started

    try:
        assert probe is fallback_probe
        assert coordinator.fallback_reason == "App Server probe timed out"
        assert elapsed < 0.5
        assert sdk.client is not None
        assert sdk.client.closed is True
    finally:
        release.set()


def test_sdk_backend_interrupts_timeout(tmp_path: Path) -> None:
    release = threading.Event()

    class InterruptHandle(FakeHandle):
        def run(self) -> object:
            release.wait(1)
            result = FakeResult()
            result.status = SimpleNamespace(value="interrupted")
            result.final_response = None
            result.usage = None
            return result

        def interrupt(self) -> None:
            self.interrupted = True
            release.set()

    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk, interrupt_grace_seconds=0.2)
    backend.probe()
    assert sdk.client is not None
    sdk.client.handle = InterruptHandle()

    result = backend.invoke(_spec(tmp_path, timeout_seconds=0.01))

    assert sdk.client.handle.interrupted is True
    assert result.status == "interrupted"


def test_sdk_backend_marks_interrupt_failure_ambiguous_and_contains_client(
    tmp_path: Path,
) -> None:
    class UninterruptibleHandle(FakeHandle):
        def run(self) -> object:
            threading.Event().wait(1)
            return FakeResult()

        def interrupt(self) -> None:
            raise RuntimeError("transport lost")

    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk)
    backend.probe()
    assert sdk.client is not None
    client = sdk.client
    client.handle = UninterruptibleHandle()

    with pytest.raises(runtime.AmbiguousInvocation, match="could not be confirmed"):
        backend.invoke(_spec(tmp_path, timeout_seconds=0.05))

    assert client.closed is True
    assert backend.probe().available is False


def test_sdk_backend_bounds_a_blocked_interrupt_rpc(tmp_path: Path) -> None:
    release = threading.Event()

    class BlockingInterruptHandle(FakeHandle):
        def run(self) -> object:
            release.wait(1)
            return FakeResult()

        def interrupt(self) -> None:
            release.wait(1)

    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk, interrupt_grace_seconds=0.02)
    backend.probe()
    assert sdk.client is not None
    client = sdk.client
    client.handle = BlockingInterruptHandle()

    with pytest.raises(runtime.AmbiguousInvocation, match="could not be confirmed"):
        backend.invoke(_spec(tmp_path, timeout_seconds=0.05))

    assert client.closed is True
    assert backend.probe().available is False
    blocked_workers = [
        thread
        for thread in threading.enumerate()
        if thread.name in {"gepa-codex-turn", "gepa-codex-interrupt"}
    ]
    assert blocked_workers
    assert all(thread.daemon for thread in blocked_workers)
    release.set()


def test_sdk_backend_bounds_blocked_turn_setup(tmp_path: Path) -> None:
    release = threading.Event()
    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk)
    backend.probe()
    assert sdk.client is not None
    client = sdk.client
    original_start = client.thread_start

    def blocked_start(**kwargs: object) -> FakeThread:
        release.wait(1)
        return original_start(**kwargs)

    client.thread_start = blocked_start

    with pytest.raises(runtime.AmbiguousInvocation, match="setup exceeded"):
        backend.invoke(_spec(tmp_path, timeout_seconds=0.01))

    assert client.closed is True
    poisoned = backend.probe()
    assert poisoned.available is False
    assert "preexisting_setup_leases=1" in (poisoned.reason or "")
    workers = [
        thread
        for thread in threading.enumerate()
        if thread.name == "gepa-codex-turn-setup"
    ]
    assert workers and all(thread.daemon for thread in workers)
    release.set()


def test_sdk_backend_contains_turn_when_lifecycle_monitor_fails(
    tmp_path: Path,
) -> None:
    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk)
    backend.probe()
    assert sdk.client is not None
    client = sdk.client

    def broken_monitor() -> str | None:
        raise RuntimeError("monitor failed")

    with pytest.raises(runtime.AmbiguousInvocation, match="monitor failed"):
        backend.invoke(_spec(tmp_path, stop_requested=broken_monitor))

    assert client.closed is True
    assert backend.probe().available is False


def test_sdk_containment_preserves_ambiguous_status_if_close_fails(
    tmp_path: Path,
) -> None:
    sdk = FakeSdk()
    backend = runtime.SdkAppServerBackend(sdk_module=sdk)
    backend.probe()
    assert sdk.client is not None
    sdk.client.close = lambda: (_ for _ in ()).throw(BrokenPipeError("closed"))

    def broken_monitor() -> str | None:
        raise RuntimeError("monitor failed")

    with pytest.raises(runtime.AmbiguousInvocation, match="monitor failed"):
        backend.invoke(_spec(tmp_path, stop_requested=broken_monitor))

    assert backend.probe().available is False


def test_runtime_falls_back_only_during_prepare_and_records_reason(
    tmp_path: Path,
) -> None:
    class Backend:
        def __init__(self, kind: str, available: bool) -> None:
            self.kind = kind
            self.available = available
            self.calls = 0

        def probe(self) -> object:
            return runtime.BackendProbe(
                self.kind,
                self.available,
                "1.0" if self.available else None,
                "chatgpt" if self.available else None,
                None if self.available else "primary probe failed",
            )

        def invoke(self, spec: object) -> object:
            self.calls += 1
            return runtime.InvocationResult(
                invocation_id=spec.invocation_id,
                provider=self.kind,
                text="BLUE",
                status="completed",
                model=spec.model,
                reasoning_effort=spec.reasoning_effort,
                sandbox=spec.sandbox,
                thread_id="thread-1",
                turn_id="turn-1",
                usage=runtime.TokenUsage(2, 0, 3, 1, 5),
                duration_ms=5,
                runtime_version="1.0",
                auth_mode="chatgpt",
            )

        def close(self) -> None:
            pass

    primary = Backend("app_server", False)
    fallback = Backend("codex_cli", True)
    evidence = runtime.EvidenceStore((tmp_path / "evidence").resolve())
    coordinator = runtime.CodexRuntime(
        evidence=evidence, primary=primary, fallback=fallback
    )

    result = coordinator.invoke(_spec(tmp_path))

    assert primary.calls == 0
    assert fallback.calls == 1
    assert result.fallback_reason == "primary probe failed"
    receipt = json.loads(
        (evidence.invocations / "invocation-1.json").read_text(encoding="utf-8")
    )
    assert receipt["provider"] == "codex_cli"
    assert receipt["fallback_reason"] == "primary probe failed"


def test_evidence_rejects_duplicate_invocation_id(tmp_path: Path) -> None:
    evidence = runtime.EvidenceStore((tmp_path / "evidence").resolve())
    spec = _spec(tmp_path)

    evidence.claim(spec)

    with pytest.raises(FileExistsError):
        evidence.claim(spec)


def test_runtime_prepare_is_single_flight(tmp_path: Path) -> None:
    class Backend:
        kind = "app_server"

        def __init__(self) -> None:
            self.probes = 0

        def probe(self) -> object:
            self.probes += 1
            threading.Event().wait(0.02)
            return runtime.BackendProbe(self.kind, True, "1.0", "chatgpt")

        def invoke(self, spec: object) -> object:
            raise AssertionError(spec)

        def close(self) -> None:
            pass

    backend = Backend()
    coordinator = runtime.CodexRuntime(
        evidence=runtime.EvidenceStore((tmp_path / "evidence").resolve()),
        primary=backend,
    )
    threads = [threading.Thread(target=coordinator.prepare) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert backend.probes == 1


def test_runtime_records_failure_when_backend_returns_wrong_invocation_id(
    tmp_path: Path,
) -> None:
    class WrongIdBackend:
        kind = "app_server"

        def probe(self) -> object:
            return runtime.BackendProbe(self.kind, True, "1.0", "chatgpt")

        def invoke(self, spec: object) -> object:
            return runtime.InvocationResult(
                invocation_id="wrong-id",
                provider=self.kind,
                text="BLUE",
                status="completed",
                model=spec.model,
                reasoning_effort=spec.reasoning_effort,
                sandbox=spec.sandbox,
                thread_id="thread-1",
                turn_id="turn-1",
                usage=runtime.TokenUsage(2, 0, 3, 1, 5),
                duration_ms=5,
                runtime_version="1.0",
                auth_mode="chatgpt",
            )

        def close(self) -> None:
            pass

    evidence = runtime.EvidenceStore((tmp_path / "evidence").resolve())
    coordinator = runtime.CodexRuntime(
        evidence=evidence, primary=WrongIdBackend(), fallback=None
    )

    with pytest.raises(RuntimeError, match="wrong invocation id"):
        coordinator.invoke(_spec(tmp_path))

    receipt = json.loads(
        (evidence.invocations / "invocation-1.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "failed"
    assert "wrong invocation id" in receipt["error"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"invocation_id": "../escape"},
        {"prompt": "  "},
        {"timeout_seconds": 0},
        {"sandbox": "full-access"},
        {"cwd": Path("relative")},
    ],
)
def test_invocation_boundary_rejects_invalid_input(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, **overrides)
