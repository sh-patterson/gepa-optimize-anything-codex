"""GEPA's reflective engine with the installed Codex runtime as its LM."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from gepa.optimize_anything import OptimizeAnythingConfig, optimize_anything

from codex_lm import CodexLM, CodexLMConfig, TARGET_MODEL, TARGET_REASONING_EFFORT
from sandbox_runtime import runtime_environment, runtime_paths, stage_runtime


_SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)


@dataclass(frozen=True, slots=True)
class CodexGepaRun:
    result: Any
    reflection_lm: CodexLM
    state_root: Path


def _codex_config(
    config: OptimizeAnythingConfig, lm: CodexLM | None
) -> OptimizeAnythingConfig:
    if config.engine != "gepa":
        raise ValueError("Codex reflection runtime requires engine='gepa'")
    if config.max_evals is None or config.max_evals <= 0:
        raise ValueError("Codex reflection runtime requires a positive max_evals")
    if config.max_token_cost is not None:
        raise ValueError("Codex token cost is an estimate, not an enforceable USD cap")

    engine_config = dict(config.engine_config)
    reflection = dict(engine_config.get("reflection") or {})
    if reflection.get("reflection_lm") is not None:
        raise ValueError("Codex reflection runtime does not accept another reflection_lm")
    if reflection.get("reflection_strategy") is not None:
        raise ValueError("Codex reflection runtime does not accept reflection_strategy")
    if reflection.get("custom_candidate_proposer") is not None:
        raise ValueError("Codex reflection runtime does not accept custom_candidate_proposer")
    if reflection.get("reflection_lm_kwargs"):
        raise ValueError("Codex reflection runtime does not accept LiteLLM options")
    reflection.pop("reflection_lm_kwargs", None)
    if lm is not None:
        reflection["reflection_lm"] = lm
    engine_config["reflection"] = reflection

    refiner = engine_config.get("refiner")
    if isinstance(refiner, dict) and refiner.get("refiner_lm") is not None:
        raise ValueError("Codex reflection runtime does not accept a provider refiner_lm")
    return replace(config, engine_config=engine_config)


def _new_codex_lm(max_reflection_calls: int, timeout_seconds: float) -> tuple[CodexLM, Path]:
    paths = stage_runtime(runtime_paths())
    state_root = paths.runs_root / f"gepa-reflection-{uuid4().hex}"
    state_dir = state_root / "adapter"
    session_dir = state_root / "driver-sessions"
    environment = runtime_environment(paths, state_dir)
    for name in _SECRET_NAMES:
        environment.pop(name, None)
    environment.update(
        {
            "CODEX_ADAPTER_AUTH_MODE": "chatgpt_login",
            "CODEX_ADAPTER_MAX_INVOCATIONS": str(max_reflection_calls),
            "CODEX_ADAPTER_PRE_SUBMISSION_RETRIES": "0",
        }
    )
    login = subprocess.run(
        [str(paths.codex), "login", "status"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if login.returncode != 0:
        raise RuntimeError("isolated Codex runtime needs a ChatGPT login")
    lm = CodexLM(
        CodexLMConfig(
            executable=paths.launcher,
            model=TARGET_MODEL,
            reasoning_effort=TARGET_REASONING_EFFORT,
            sandbox_mode="workspace-write",
            state_dir=state_dir,
            session_dir=session_dir,
            timeout_seconds=timeout_seconds,
            retry_ceiling=0,
        ),
        cwd=Path.cwd(),
        environment=environment,
    )
    return lm, state_root


def optimize_with_codex(
    seed_candidate: Any = None,
    *,
    config: OptimizeAnythingConfig | None = None,
    max_reflection_calls: int = 4,
    timeout_seconds: float = 600,
    **kwargs: Any,
) -> CodexGepaRun:
    """Run GEPA with Codex reflection, bounded starts, and no provider fallback."""
    if (
        isinstance(max_reflection_calls, bool)
        or not isinstance(max_reflection_calls, int)
        or max_reflection_calls < 1
    ):
        raise ValueError("max_reflection_calls must be a positive integer")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    requested = config or OptimizeAnythingConfig(engine="gepa", max_evals=10)
    # Reject incompatible choices before staging or making any model call.
    _codex_config(requested, lm=None)
    lm, state_root = _new_codex_lm(max_reflection_calls, timeout_seconds)
    actual = _codex_config(requested, lm)
    result = optimize_anything(seed_candidate, config=actual, **kwargs)
    return CodexGepaRun(result=result, reflection_lm=lm, state_root=state_root)
