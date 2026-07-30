from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4


TARGET_MODEL = "gpt-5.6-luna"


class CodexLM:
    """GEPA language-model callable backed by the staged Codex adapter."""

    def __init__(
        self,
        model: str,
        *,
        launcher: Path,
        cwd: Path,
        environment: dict[str, str],
        reasoning_effort: str = "high",
        temperature: float | None = None,
        **kwargs: Any,
    ) -> None:
        if model != TARGET_MODEL:
            raise ValueError(f"unsupported Codex model: {model}")
        if temperature not in (None, 1.0) or kwargs:
            raise ValueError("CodexLM does not support LiteLLM sampling options")
        self.model = model
        self.launcher = launcher
        self.cwd = cwd
        self.environment = dict(environment)
        self.reasoning_effort = reasoning_effort
        self.total_cost = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0
        self.total_usage: dict[str, int] = {}
        self.invocation_count = 0

    def __call__(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("CodexLM requires a non-empty string prompt")
        session_id = uuid4().hex
        completed = subprocess.run(
            [
                os.fspath(self.launcher),
                "--print",
                "--output-format",
                "json",
                "--permission-mode",
                "bypassPermissions",
                "--disallowedTools=WebFetch,WebSearch",
                "--model",
                self.model,
                "--effort",
                self.reasoning_effort,
                "--session-id",
                session_id,
                prompt,
            ],
            cwd=self.cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex adapter returned invalid JSON") from exc
        if (
            completed.returncode != 0
            or not isinstance(payload, dict)
            or payload.get("is_error") is not False
        ):
            raise RuntimeError("Codex adapter invocation failed")
        if payload.get("session_id") != session_id:
            raise RuntimeError("Codex adapter session mapping is inconsistent")
        if payload.get("adapter_target_model") != self.model:
            raise RuntimeError("Codex adapter selected an unexpected model")
        result = payload.get("result")
        usage = payload.get("usage")
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("Codex adapter returned an empty result")
        if not isinstance(usage, dict):
            raise RuntimeError("Codex adapter returned no usage")
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in usage.values()
            )
            or isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or input_tokens + output_tokens <= 0
        ):
            raise RuntimeError("Codex adapter returned invalid usage")
        estimated_cost = payload.get("total_cost_usd")
        if (
            isinstance(estimated_cost, bool)
            or not isinstance(estimated_cost, (int, float))
            or estimated_cost <= 0
        ):
            raise RuntimeError("Codex adapter returned no cost estimate")
        self.total_tokens_in += input_tokens
        self.total_tokens_out += output_tokens
        for name, value in usage.items():
            self.total_usage[name] = self.total_usage.get(name, 0) + value
        self.total_cost += float(estimated_cost)
        self.invocation_count += 1
        return result
