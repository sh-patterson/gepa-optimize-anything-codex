from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from codex_runtime import (
    AmbiguousInvocation,
    BackendProbe,
    BackendUnavailable,
    InvocationResult,
    InvocationSpec,
    ProviderKind,
    TokenUsage,
)


_SECRET_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_API_KEY",
        "OPENAI_API_KEY",
    }
)


@dataclass(frozen=True, slots=True)
class CliTerminal:
    thread_id: str | None
    text: str
    status: Literal["completed", "failed", "missing", "invalid"]
    usage: TokenUsage | None
    error: str | None


class CodexCliBackend:
    """Direct Codex CLI fallback with no Claude compatibility surface."""

    kind: ProviderKind = "codex_cli"

    def __init__(
        self,
        *,
        executable: Path,
        codex_home: Path | None = None,
        environment: Mapping[str, str] | None = None,
        termination_grace_seconds: float = 5.0,
    ) -> None:
        if termination_grace_seconds <= 0:
            raise ValueError("termination grace must be positive")
        self.executable = executable.resolve()
        self.codex_home = (codex_home or Path.home() / ".codex").resolve()
        self.environment = _child_environment(self.codex_home, environment)
        self.termination_grace_seconds = termination_grace_seconds
        self._probe: BackendProbe | None = None

    def probe(self) -> BackendProbe:
        if self._probe is not None:
            return self._probe
        try:
            if not self.executable.is_file():
                raise RuntimeError(f"Codex CLI not found: {self.executable}")
            version = subprocess.run(
                [str(self.executable), "--version"],
                cwd=self.codex_home,
                env=self.environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if version.returncode != 0 or not version.stdout.strip():
                raise RuntimeError("Codex CLI version probe failed")
            login = subprocess.run(
                [str(self.executable), "login", "status"],
                cwd=self.codex_home,
                env=self.environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            auth_mode = _login_mode(login)
            if auth_mode == "none":
                raise RuntimeError("Codex authentication is required")
            self._probe = BackendProbe(
                provider=self.kind,
                available=True,
                runtime_version=version.stdout.strip(),
                auth_mode=auth_mode,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
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
        if not probe.available:
            raise BackendUnavailable(probe.reason or "Codex CLI unavailable")
        schema_path = _write_output_schema(spec)
        command = build_command(spec, self.executable, schema_path)
        started = time.monotonic()
        try:
            completed = self._run(command, spec)
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)
        duration_ms = round((time.monotonic() - started) * 1000)
        terminal = parse_jsonl(completed.stdout)
        if terminal.status in {"missing", "invalid"}:
            raise AmbiguousInvocation(terminal.error or "Codex terminal event is missing")
        if completed.returncode != 0 and terminal.status == "completed":
            raise AmbiguousInvocation("Codex completed but the process exited non-zero")
        if terminal.status == "completed" and (
            not terminal.text.strip() or terminal.usage is None
        ):
            raise AmbiguousInvocation("Codex completion is missing text or usage")
        thread_id = terminal.thread_id or spec.resume_thread_id
        if terminal.status == "completed" and not thread_id:
            raise AmbiguousInvocation("Codex completion is missing a thread id")
        return InvocationResult(
            invocation_id=spec.invocation_id,
            provider=self.kind,
            text=terminal.text,
            status=terminal.status,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            sandbox=spec.sandbox,
            thread_id=thread_id,
            turn_id=None,
            usage=terminal.usage,
            duration_ms=duration_ms,
            runtime_version=probe.runtime_version or "unknown",
            auth_mode=probe.auth_mode or "unknown",
        )

    def _run(
        self, command: list[str], spec: InvocationSpec
    ) -> subprocess.CompletedProcess[str]:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=spec.cwd,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        try:
            stdout, stderr = process.communicate(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process(process, self.termination_grace_seconds)
            raise AmbiguousInvocation("Codex CLI timed out and was terminated") from exc
        return subprocess.CompletedProcess(
            command, process.returncode, stdout=stdout, stderr=stderr
        )

    def close(self) -> None:
        return None


def build_command(
    spec: InvocationSpec, executable: Path, schema_path: Path | None
) -> list[str]:
    common = [
        "--json",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--model",
        spec.model,
        "--config",
        f'model_reasoning_effort="{spec.reasoning_effort}"',
        "--config",
        f'sandbox_mode="{spec.sandbox}"',
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        'web_search="disabled"',
    ]
    if schema_path is not None:
        common.extend(("--output-schema", str(schema_path)))
    if spec.resume_thread_id:
        return [
            str(executable),
            "exec",
            "resume",
            *common,
            spec.resume_thread_id,
            spec.prompt,
        ]
    return [
        str(executable),
        "exec",
        *common,
        "--sandbox",
        spec.sandbox,
        "--cwd",
        str(spec.cwd),
        spec.prompt,
    ]


def parse_jsonl(raw: str) -> CliTerminal:
    thread_id: str | None = None
    messages: list[str] = []
    terminal_events = 0
    status: Literal["completed", "failed", "missing", "invalid"] = "missing"
    usage: TokenUsage | None = None
    error: str | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return CliTerminal(thread_id, "", "invalid", None, "malformed JSONL")
        if not isinstance(event, dict):
            return CliTerminal(thread_id, "", "invalid", None, "non-object JSONL")
        event_type = event.get("type")
        if event_type == "thread.started":
            raw_thread = event.get("thread_id")
            thread_id = raw_thread if isinstance(raw_thread, str) and raw_thread else None
        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
        if event_type in {"turn.completed", "turn.failed", "error"}:
            terminal_events += 1
            if terminal_events > 1:
                return CliTerminal(
                    thread_id, "", "invalid", None, "multiple terminal events"
                )
            if event_type == "turn.completed":
                try:
                    usage = _event_usage(event.get("usage"))
                except ValueError as exc:
                    return CliTerminal(thread_id, "", "invalid", None, str(exc))
                status = "completed"
            else:
                status = "failed"
                error = _safe_reason(str(event.get("error") or event.get("message") or "failed"))
    return CliTerminal(thread_id, messages[-1] if messages else "", status, usage, error)


def _event_usage(raw: object) -> TokenUsage:
    if not isinstance(raw, dict):
        raise ValueError("completed turn has no usage")
    input_tokens = _non_negative_int(raw, "input_tokens")
    output_tokens = _non_negative_int(raw, "output_tokens")
    cached = _optional_non_negative_int(raw, "cached_input_tokens")
    reasoning = _optional_non_negative_int(raw, "reasoning_output_tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        total_tokens=input_tokens + output_tokens,
    )


def _non_negative_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _optional_non_negative_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def _child_environment(
    codex_home: Path, configured: Mapping[str, str] | None
) -> dict[str, str]:
    source = os.environ if configured is None else configured
    environment = {
        name: value for name, value in source.items() if name not in _SECRET_NAMES
    }
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _login_mode(completed: subprocess.CompletedProcess[str]) -> str:
    text = f"{completed.stdout}\n{completed.stderr}".casefold()
    if completed.returncode != 0 or "not logged in" in text:
        return "none"
    if "chatgpt" in text:
        return "chatgpt"
    if "api key" in text or "apikey" in text:
        return "api_key"
    return "managed" if "logged in" in text else "none"


def _write_output_schema(spec: InvocationSpec) -> Path | None:
    if spec.output_schema is None:
        return None
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".gepa-codex-schema-{spec.invocation_id}-",
        suffix=".json",
        dir=spec.cwd,
        text=True,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(dict(spec.output_schema), file, sort_keys=True)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _terminate_process(process: subprocess.Popen[str], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _safe_reason(reason: str) -> str:
    return reason.replace("\r", " ").replace("\n", " ")[:300]
